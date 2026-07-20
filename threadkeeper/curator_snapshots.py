"""Recoverable snapshots for destructive curator passes.

The autonomous curator runs as a spawned child and, in destructive mode, calls
the normal lesson/skill mutators itself. This module gives the parent a
fail-closed pre-mutation archive and lets those mutators add per-pass
tombstones/telemetry when they are running inside that curator child.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import identity, lessons
from .config import (
    CLAUDE_SKILLS_DIR,
    CURATOR_MAX_DESTRUCTIVE_PER_PASS,
    CURATOR_REPORTS_DIR,
    CURATOR_SNAPSHOT_RETENTION,
    WRITE_ORIGIN,
)
from .identity import _ensure_session


PASS_ID_ENV = "THREADKEEPER_CURATOR_PASS_ID"
SNAPSHOT_DIR_ENV = "THREADKEEPER_CURATOR_SNAPSHOT_DIR"
ACTION_EVENT = "curator_destructive_action"
SNAPSHOT_EVENT = "curator_snapshot"
CAP_EVENT = "curator_destructive_cap"

_SAFE_PASS_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


def snapshots_root(reports_dir: Path | None = None) -> Path:
    return (reports_dir or CURATOR_REPORTS_DIR) / "snapshots"


def current_pass_id() -> str:
    return os.environ.get(PASS_ID_ENV, "").strip()


def current_snapshot_dir() -> Path | None:
    raw = os.environ.get(SNAPSHOT_DIR_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", value.strip())[:160] or "item"


def _validate_pass_id(pass_id: str) -> str:
    pass_id = pass_id.strip()
    if not pass_id or not _SAFE_PASS_RE.match(pass_id):
        raise ValueError("invalid pass_id")
    return pass_id


def _snapshot_dir(pass_id: str,
                  reports_dir: Path | None = None) -> Path:
    return snapshots_root(reports_dir) / _validate_pass_id(pass_id)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _iter_skill_dirs() -> list[tuple[Path, list[Path]]]:
    """Return every configured skills root with direct child skill dirs."""
    try:
        from .tools import skills as skill_tools

        roots = skill_tools._skill_roots()
    except Exception:
        roots = [CLAUDE_SKILLS_DIR]

    out: list[tuple[Path, list[Path]]] = []
    seen_roots: set[Path] = set()
    for root in roots:
        root = root.expanduser()
        key = root.resolve(strict=False)
        if key in seen_roots:
            continue
        seen_roots.add(key)
        dirs: list[Path] = []
        if root.exists():
            for child in sorted(root.iterdir(), key=lambda p: p.name):
                if child.name.startswith("."):
                    continue
                if child.is_dir() and (child / "SKILL.md").is_file():
                    dirs.append(child)
        out.append((root, dirs))
    return out


def _prune_old_snapshots(retention: int,
                         reports_dir: Path | None = None) -> None:
    keep = max(1, int(retention))
    root = snapshots_root(reports_dir)
    if not root.exists():
        return
    dirs = [p for p in root.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: (p.name, p.stat().st_mtime), reverse=True)
    for old in dirs[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def create_curator_snapshot(
    pass_id: str,
    *,
    conn: sqlite3.Connection | None = None,
    reports_dir: Path | None = None,
    retention: int = CURATOR_SNAPSHOT_RETENTION,
) -> Path:
    """Archive lessons.md plus all in-scope skill dirs before mutation."""
    pass_id = _validate_pass_id(pass_id)
    reports_dir = reports_dir or CURATOR_REPORTS_DIR
    snap = _snapshot_dir(pass_id, reports_dir)
    if snap.exists():
        shutil.rmtree(snap)
    snap.mkdir(parents=True, exist_ok=False)

    now = int(time.time())
    lesson_path = lessons.get_path()
    lesson_meta: dict[str, Any] = {
        "path": str(lesson_path),
        "snapshot": "lessons.md",
        "exists": lesson_path.exists(),
        "count": lessons.count_lessons(lesson_path) if lesson_path.exists() else 0,
    }
    if lesson_path.exists():
        shutil.copy2(lesson_path, snap / "lessons.md")

    skill_roots: list[dict[str, Any]] = []
    for idx, (root, dirs) in enumerate(_iter_skill_dirs()):
        root_id = f"root-{idx}"
        root_entry: dict[str, Any] = {
            "id": root_id,
            "path": str(root),
            "skills": [],
        }
        for sdir in dirs:
            rel = Path("skills") / root_id / sdir.name
            shutil.copytree(sdir, snap / rel)
            root_entry["skills"].append({
                "name": sdir.name,
                "snapshot": rel.as_posix(),
            })
        skill_roots.append(root_entry)

    manifest = {
        "pass_id": pass_id,
        "created_at": now,
        "lessons": lesson_meta,
        "skill_roots": skill_roots,
    }
    _write_json(snap / "manifest.json", manifest)
    _prune_old_snapshots(retention, reports_dir)

    if conn is not None:
        _ensure_session(conn)
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                identity._session_id or "",
                SNAPSHOT_EVENT,
                pass_id,
                f"path={snap} lessons={lesson_meta['count']} "
                f"skills={sum(len(r['skills']) for r in skill_roots)}",
                now,
            ),
        )
        conn.commit()
    return snap


def _write_text_tombstone(
    snapshot_dir: Path,
    artifact: str,
    action: str,
    key: str,
    body: str,
) -> str:
    rel = Path("tombstones") / artifact / action / f"{_safe_name(key)}.md"
    path = snapshot_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return rel.as_posix()


def capture_skill_tombstone(action: str, name: str, skill_dir: Path) -> str:
    snap = current_snapshot_dir()
    if not current_pass_id() or snap is None or not skill_dir.exists():
        return ""
    rel = Path("tombstones") / "skills" / action / _safe_name(name)
    dst = snap / rel
    try:
        if dst.exists():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_dir, dst)
    except OSError:
        return ""
    return rel.as_posix()


def record_curator_action(
    conn: sqlite3.Connection,
    *,
    action: str,
    artifact: str,
    key: str,
    body: str = "",
    snapshot_rel: str = "",
) -> str:
    """Record one curator mutation and optional tombstone body.

    No-ops unless the current process is a curator child with a pass id.
    """
    pass_id = current_pass_id()
    if not pass_id:
        return ""
    snap = current_snapshot_dir()
    body_rel = snapshot_rel
    if body and snap is not None:
        try:
            body_rel = _write_text_tombstone(snap, artifact, action, key, body)
        except OSError:
            body_rel = snapshot_rel

    summary = (
        f"action={action} artifact={artifact} key={_safe_name(key)} "
        f"snapshot={body_rel or '-'}"
    )
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, ?, ?, ?, strftime('%s','now'))",
            (identity._session_id or "", ACTION_EVENT, pass_id, summary),
        )
    except sqlite3.OperationalError:
        return body_rel
    return body_rel


def admit_curator_destructive_action(
    conn: sqlite3.Connection,
    *,
    action: str,
    artifact: str,
    key: str,
) -> str | None:
    """Reserve one destructive curator operation for the current pass.

    Every bounded-inventory child spawned for a pass inherits the same pass
    identifier.  The ``BEGIN IMMEDIATE`` reservation serializes those children
    across MCP server processes, so a prompt cannot race several deletes past
    the configured total.  The admission row remains even if a later file
    operation fails; failing closed is preferable to restoring deletion
    authority after an uncertain partial mutation.

    Returns an error reason when the caller must not mutate, otherwise ``None``.
    Foreground and other non-curator writers deliberately bypass this guard.
    """
    if WRITE_ORIGIN != "curator":
        return None

    _ensure_session(conn)
    pass_id = current_pass_id()
    limit = max(0, int(CURATOR_MAX_DESTRUCTIVE_PER_PASS))
    if not pass_id:
        return (
            "curator_destructive_cap_missing_pass_id "
            f"action={action} limit={limit}"
        )

    def record(outcome: str, used: int) -> None:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, ?, ?, ?, strftime('%s','now'))",
            (
                identity._session_id or "",
                CAP_EVENT,
                pass_id,
                (
                    f"outcome={outcome} action={action} artifact={artifact} "
                    f"key={_safe_name(key)} limit={limit} used={used}"
                ),
            ),
        )

    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM events "
            "WHERE kind=? AND target=? AND summary LIKE 'outcome=admitted %'",
            (CAP_EVENT, pass_id),
        ).fetchone()
        used = int(row["n"] if row else 0)
        if used >= limit:
            record("refused", used)
            conn.commit()
            return (
                "curator_destructive_cap_exceeded "
                f"action={action} limit={limit} used={used}"
            )
        record("admitted", used)
        conn.commit()
        return None
    except sqlite3.Error as e:
        if conn.in_transaction:
            conn.rollback()
        # A server-side safety guard must fail closed when accounting cannot
        # be persisted, rather than silently allowing an unbounded delete.
        return f"curator_destructive_cap_unavailable action={action}: {e}"


def _load_manifest(snapshot_dir: Path) -> dict[str, Any]:
    return json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))


def _find_lesson_block(snapshot_lessons: Path, slug: str) -> tuple[str, dict] | None:
    if not snapshot_lessons.exists():
        return None
    text = snapshot_lessons.read_text(encoding="utf-8")
    items = {item["slug"]: item for item in lessons.iter_lessons(snapshot_lessons)}
    for match in lessons._BLOCK_RE.finditer(text):
        if match.group("slug") != slug:
            continue
        block = match.group(0).rstrip() + "\n\n"
        return block, items.get(slug, {})
    return None


def restore_lesson(pass_id: str, slug: str, conn: sqlite3.Connection) -> str:
    pass_id = _validate_pass_id(pass_id)
    slug = lessons._slugify(slug)
    snap = _snapshot_dir(pass_id)
    found = _find_lesson_block(snap / "lessons.md", slug)
    if found is None:
        return f"ERR lesson_not_in_snapshot slug={slug} pass_id={pass_id}"
    block, item = found

    target = lessons.get_path()
    lessons._ensure_file(target)
    text = target.read_text(encoding="utf-8")
    begin = f"<!-- LESSON:BEGIN slug={slug} "
    end = f"<!-- LESSON:END slug={slug} -->"
    if begin in text and end in text:
        head, _, rest = text.partition(begin)
        end_idx = rest.find(end)
        if end_idx < 0:
            return f"ERR current_lesson_malformed slug={slug}"
        tail = rest[end_idx + len(end):]
        text = head.rstrip() + "\n\n" + block + tail.lstrip("\n")
    else:
        text = text.rstrip() + "\n\n" + block
    target.write_text(text.rstrip() + "\n", encoding="utf-8")

    _ensure_session(conn)
    lessons.ensure_lesson_usage(conn, item)
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?, 'curator_restore', ?, ?, strftime('%s','now'))",
        (identity._session_id or "", pass_id, f"artifact=lesson key={slug}"),
    )
    conn.commit()
    return f"ok restored_lesson={slug} from={snap}"


def restore_skill(pass_id: str, name: str, conn: sqlite3.Connection) -> str:
    pass_id = _validate_pass_id(pass_id)
    name = name.strip()
    try:
        from .tools import skills as skill_tools
    except Exception as e:
        return f"ERR skill_restore_unavailable={e}"
    if err := skill_tools._validate_name(name):
        return f"ERR {err}"

    snap = _snapshot_dir(pass_id)
    manifest = _load_manifest(snap)
    src: Path | None = None
    for root in manifest.get("skill_roots", []):
        for skill in root.get("skills", []):
            if skill.get("name") == name:
                src = snap / str(skill.get("snapshot", ""))
                break
        if src is not None:
            break
    if src is None or not src.is_dir():
        return f"ERR skill_not_in_snapshot name={name} pass_id={pass_id}"

    dst = skill_tools._skill_dir(name)
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
    skill_tools._mirror_skill_dir(name)
    skill_tools._record_event(name, "patch")

    _ensure_session(conn)
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?, 'curator_restore', ?, ?, strftime('%s','now'))",
        (identity._session_id or "", pass_id, f"artifact=skill key={name}"),
    )
    conn.commit()
    return f"ok restored_skill={name} from={snap}"
