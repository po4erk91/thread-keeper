"""Scheduled updater for installed Skill.md directories.

The updater has two jobs:

- keep mirrored CLI skill roots in sync by importing the newest local copy into
  the primary root and mirroring it everywhere else;
- update source-tracked GitHub skills when their upstream directory changed.

It deliberately updates SKILL.md directories, not arbitrary files. Skills with
local edits after the last source-tracked update are skipped instead of being
overwritten.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from urllib import request
import zipfile

from . import identity
from .config import (
    BACKGROUND_DAEMONS_ALLOWED,
    CLAUDE_SKILLS_DIR,
    DB_PATH,
    SKILL_UPDATE_ALLOW_UNTRACKED_OVERWRITE,
    SKILL_UPDATE_INFER_SOURCES,
    SKILL_UPDATE_INTERVAL_S,
    SKILL_UPDATE_SOURCES,
    SKILL_UPDATE_TIMEOUT_S,
)
from .db import get_db
from .helpers import daemon_sleep, single_flight_lock

logger = logging.getLogger(__name__)

SOURCE_FILE = ".threadkeeper-skill-source.json"
EVENT_KIND = "skill_update_pass"
_started = False


@dataclass(frozen=True)
class GithubSource:
    repo: str
    ref: str
    path: str


@dataclass(frozen=True)
class RemoteSkill:
    name: str
    source: GithubSource
    path: Path
    tree_sha256: str


@dataclass
class SkillCopy:
    root: Path
    path: Path
    name: str
    tree_sha256: str
    mtime: int
    primary: bool


def _record_skill_update_pass(summary: str) -> None:
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, ?, '', ?, ?)",
            (
                identity._session_id or "",
                EVENT_KIND,
                summary[:300],
                int(time.time()),
            ),
        )
        conn.commit()
    except Exception:
        logger.debug("skill_updater: failed to record pass", exc_info=True)


def _last_skill_update_ts() -> int:
    try:
        row = get_db().execute(
            "SELECT created_at FROM events WHERE kind=? "
            "ORDER BY created_at DESC LIMIT 1",
            (EVENT_KIND,),
        ).fetchone()
    except Exception:
        return 0
    if not row:
        return 0
    try:
        return int(row["created_at"] or 0)
    except (TypeError, ValueError):
        return 0


def _due(now: int | None = None) -> tuple[bool, int]:
    if SKILL_UPDATE_INTERVAL_S <= 0:
        return False, 0
    now = int(now or time.time())
    last = _last_skill_update_ts()
    age = now - last if last else int(SKILL_UPDATE_INTERVAL_S)
    return last == 0 or age >= SKILL_UPDATE_INTERVAL_S, max(0, age)


def _update_lock():
    return single_flight_lock("skill-update")


def _short(text: str, limit: int = 120) -> str:
    return " ".join((text or "").split())[:limit]


def _is_skill_dir(path: Path) -> bool:
    return path.is_dir() and (path / "SKILL.md").is_file()


def _skip_rel(rel: Path) -> bool:
    parts = rel.parts
    if not parts:
        return False
    if parts[0] in {SOURCE_FILE, ".DS_Store", ".git", "__pycache__"}:
        return True
    return any(part in {".git", "__pycache__"} for part in parts)


def _tree_sha256(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if _skip_rel(rel) or path.is_dir():
            continue
        if not path.is_file():
            continue
        h.update(rel.as_posix().encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(path.read_bytes())
        except OSError:
            continue
        h.update(b"\0")
    return h.hexdigest()


def _tree_mtime(root: Path) -> int:
    latest = 0
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if _skip_rel(rel):
            continue
        try:
            latest = max(latest, int(path.stat().st_mtime))
        except OSError:
            continue
    return latest


def _skill_name(path: Path) -> str:
    md = path / "SKILL.md"
    try:
        body = md.read_text(encoding="utf-8")
    except OSError:
        return path.name
    try:
        from .tools.skills import _parse_frontmatter

        fields, err = _parse_frontmatter(body)
        if not err and fields and fields.get("name"):
            return str(fields["name"])
    except Exception:
        return path.name
    return path.name


def _skill_roots() -> list[Path]:
    from .tools.skills import _skill_roots as configured_skill_roots

    roots: list[Path] = []
    seen: set[Path] = set()
    for root in configured_skill_roots():
        try:
            key = root.expanduser().resolve()
        except OSError:
            key = root.expanduser()
        if key in seen:
            continue
        seen.add(key)
        roots.append(root.expanduser())
    return roots


def _iter_skill_copies() -> list[SkillCopy]:
    copies: list[SkillCopy] = []
    primary_root = CLAUDE_SKILLS_DIR.expanduser().resolve()
    for root in _skill_roots():
        if not root.exists():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if child.name.startswith("."):
                continue
            if not _is_skill_dir(child):
                continue
            try:
                is_primary = child.parent.resolve() == primary_root
            except OSError:
                is_primary = False
            copies.append(
                SkillCopy(
                    root=root,
                    path=child,
                    name=_skill_name(child),
                    tree_sha256=_tree_sha256(child),
                    mtime=_tree_mtime(child),
                    primary=is_primary,
                )
            )
    return copies


def _validate_skill_dir(path: Path, expected_name: str | None = None) -> str | None:
    md = path / "SKILL.md"
    if not md.is_file():
        return "missing_SKILL.md"
    try:
        body = md.read_text(encoding="utf-8")
    except OSError as e:
        return f"read_failed: {e}"
    try:
        from .tools.skills import _validate_skill_md

        name = expected_name or _skill_name(path)
        return _validate_skill_md(body, name)
    except Exception as e:  # noqa: BLE001
        return f"validate_failed: {e}"


def _replace_skill_tree(src: Path, dst: Path) -> None:
    if src.resolve() == dst.resolve():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        if dst.is_symlink():
            dst.unlink()
        else:
            shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=False)


def _backup_skill_dir(skill_dir: Path) -> None:
    if not skill_dir.exists():
        return
    stamp = int(time.time())
    backup_root = DB_PATH.parent / "skill-update-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    dst = backup_root / f"{skill_dir.name}-{stamp}"
    idx = 1
    while dst.exists():
        dst = backup_root / f"{skill_dir.name}-{stamp}-{idx}"
        idx += 1
    shutil.copytree(skill_dir, dst, symlinks=True)


def _mirror_skill(name: str) -> None:
    from .tools.skills import _mirror_skill_dir

    _mirror_skill_dir(name)


def _sync_local_roots() -> dict[str, int]:
    counts = {
        "local_updates": 0,
        "mirrored": 0,
        "conflicts": 0,
        "local_checked": 0,
    }
    grouped: dict[str, list[SkillCopy]] = {}
    for copy in _iter_skill_copies():
        grouped.setdefault(copy.name, []).append(copy)

    primary_root = CLAUDE_SKILLS_DIR.expanduser()
    for name, copies in grouped.items():
        counts["local_checked"] += 1
        primary = next((c for c in copies if c.primary), None)
        newest_mtime = max(c.mtime for c in copies)
        newest = [c for c in copies if c.mtime == newest_mtime]
        newest_hashes = {c.tree_sha256 for c in newest}
        if len(newest_hashes) != 1:
            counts["conflicts"] += 1
            continue

        distinct_hashes = {c.tree_sha256 for c in copies}
        if primary and len(distinct_hashes) <= 1:
            continue

        winner = newest[0]
        if primary and primary.tree_sha256 == winner.tree_sha256:
            _mirror_skill(name)
            counts["mirrored"] += 1
            continue

        if primary and primary.path.is_symlink():
            counts["conflicts"] += 1
            continue

        dst = primary_root / name
        if err := _validate_skill_dir(winner.path, name):
            logger.debug("skill_updater: skip local %s: %s", name, err)
            counts["conflicts"] += 1
            continue
        _backup_skill_dir(dst)
        _replace_skill_tree(winner.path, dst)
        _mirror_skill(name)
        counts["local_updates"] += 1
    return counts


def _parse_sources(raw: str) -> list[GithubSource]:
    specs: list[GithubSource] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            logger.warning("Ignoring invalid skill update source: %s", item)
            continue
        left, path = item.split(":", 1)
        if "@" in left:
            repo, ref = left.rsplit("@", 1)
        else:
            repo, ref = left, "main"
        repo = repo.strip().strip("/")
        ref = (ref or "main").strip()
        path = path.strip().strip("/")
        if "/" not in repo or not path:
            logger.warning("Ignoring invalid skill update source: %s", item)
            continue
        specs.append(GithubSource(repo=repo, ref=ref, path=path))
    return specs


def _read_source_file(skill_dir: Path) -> GithubSource | None:
    path = skill_dir / SOURCE_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("type") != "github":
        return None
    repo = str(data.get("repo") or "").strip().strip("/")
    ref = str(data.get("ref") or "main").strip()
    src_path = str(data.get("path") or "").strip().strip("/")
    if "/" not in repo or not src_path:
        return None
    return GithubSource(repo=repo, ref=ref, path=src_path)


def _read_source_sha(skill_dir: Path) -> str:
    path = skill_dir / SOURCE_FILE
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str(data.get("tree_sha256") or "")


def _write_source_file(skill_dir: Path, remote: RemoteSkill, *, inferred: bool) -> None:
    data = {
        "type": "github",
        "repo": remote.source.repo,
        "ref": remote.source.ref,
        "path": remote.source.path,
        "tree_sha256": remote.tree_sha256,
        "source_url": (
            f"https://github.com/{remote.source.repo}/tree/"
            f"{remote.source.ref}/{remote.source.path}"
        ),
        "inferred": bool(inferred),
        "updated_at": int(time.time()),
    }
    (skill_dir / SOURCE_FILE).write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _safe_extract_zip(zip_file: zipfile.ZipFile, dest_dir: Path) -> None:
    dest_root = dest_dir.resolve()
    for info in zip_file.infolist():
        extracted = (dest_dir / info.filename).resolve()
        if extracted == dest_root or dest_root in extracted.parents:
            continue
        raise ValueError("archive contains files outside destination")
    zip_file.extractall(dest_dir)


def _download_repo_zip(source: GithubSource, dest_dir: Path) -> Path:
    url = f"https://codeload.github.com/{source.repo}/zip/{source.ref}"
    req = request.Request(url, headers={"User-Agent": "threadkeeper-skill-updater"})
    zip_path = dest_dir / "repo.zip"
    with request.urlopen(req, timeout=SKILL_UPDATE_TIMEOUT_S) as res:
        zip_path.write_bytes(res.read())
    with zipfile.ZipFile(zip_path, "r") as zf:
        _safe_extract_zip(zf, dest_dir)
        top_levels = {name.split("/")[0] for name in zf.namelist() if name}
    if len(top_levels) != 1:
        raise ValueError("unexpected archive layout")
    return dest_dir / next(iter(top_levels))


def _remote_skills_from_source(source: GithubSource, repo_root: Path) -> list[RemoteSkill]:
    base = repo_root / source.path
    candidates: list[Path]
    if _is_skill_dir(base):
        candidates = [base]
    elif base.is_dir():
        candidates = [
            child for child in base.iterdir()
            if child.is_dir() and not child.name.startswith(".") and _is_skill_dir(child)
        ]
    else:
        return []

    out: list[RemoteSkill] = []
    for path in candidates:
        name = _skill_name(path)
        src_path = path.relative_to(repo_root).as_posix()
        exact_source = GithubSource(
            repo=source.repo,
            ref=source.ref,
            path=src_path,
        )
        out.append(
            RemoteSkill(
                name=name,
                source=exact_source,
                path=path,
                tree_sha256=_tree_sha256(path),
            )
        )
    return out


def _build_remote_index(
    sources: list[GithubSource],
    tmp_root: Path,
) -> tuple[dict[tuple[str, str, str], RemoteSkill], dict[str, RemoteSkill], list[str]]:
    by_exact: dict[tuple[str, str, str], RemoteSkill] = {}
    by_name: dict[str, RemoteSkill] = {}
    ambiguous: set[str] = set()
    errors: list[str] = []
    repo_cache: dict[tuple[str, str], Path] = {}

    for source in sources:
        try:
            key = (source.repo, source.ref)
            if key not in repo_cache:
                repo_tmp = tmp_root / (
                    source.repo.replace("/", "_") + "@" + source.ref.replace("/", "_")
                )
                repo_tmp.mkdir(parents=True, exist_ok=True)
                repo_cache[key] = _download_repo_zip(source, repo_tmp)
            for remote in _remote_skills_from_source(source, repo_cache[key]):
                exact = (remote.source.repo, remote.source.ref, remote.source.path)
                by_exact[exact] = remote
                if remote.name in by_name and by_name[remote.name].source != remote.source:
                    ambiguous.add(remote.name)
                else:
                    by_name[remote.name] = remote
        except Exception as e:  # noqa: BLE001
            errors.append(f"{source.repo}:{source.path}: {_short(str(e))}")

    for name in ambiguous:
        by_name.pop(name, None)
    return by_exact, by_name, errors


def _source_specs_for_primary_skills() -> list[GithubSource]:
    specs = _parse_sources(SKILL_UPDATE_SOURCES)
    seen = {(s.repo, s.ref, s.path) for s in specs}
    for skill_dir in _primary_skill_dirs():
        source = _read_source_file(skill_dir)
        if not source:
            continue
        key = (source.repo, source.ref, source.path)
        if key in seen:
            continue
        seen.add(key)
        specs.append(source)
    return specs


def _primary_skill_dirs() -> list[Path]:
    root = CLAUDE_SKILLS_DIR.expanduser()
    if not root.exists():
        return []
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    return [
        child for child in children
        if child.is_dir() and not child.name.startswith(".") and _is_skill_dir(child)
    ]


def _apply_remote_update(
    skill_dir: Path,
    remote: RemoteSkill,
    *,
    inferred: bool,
) -> str:
    name = _skill_name(skill_dir)
    if err := _validate_skill_dir(remote.path, remote.name):
        return f"error_remote_invalid={name}:{_short(err)}"
    local_hash = _tree_sha256(skill_dir)
    if local_hash == remote.tree_sha256:
        _write_source_file(skill_dir, remote, inferred=inferred)
        return "no_update"

    if inferred and not SKILL_UPDATE_ALLOW_UNTRACKED_OVERWRITE:
        return "skipped_untracked_changed"

    expected_hash = _read_source_sha(skill_dir)
    if expected_hash and expected_hash != local_hash:
        return "skipped_local_changes"

    _backup_skill_dir(skill_dir)
    _replace_skill_tree(remote.path, skill_dir)
    _write_source_file(skill_dir, remote, inferred=inferred)
    _mirror_skill(remote.name)
    return "updated"


def _update_source_tracked_skills() -> dict[str, int | list[str]]:
    counts: dict[str, int | list[str]] = {
        "remote_checked": 0,
        "remote_updated": 0,
        "remote_no_update": 0,
        "remote_skipped": 0,
        "remote_errors": [],
    }
    sources = _source_specs_for_primary_skills()
    if not sources:
        return counts
    with tempfile.TemporaryDirectory(prefix="threadkeeper-skill-update-") as td:
        by_exact, by_name, errors = _build_remote_index(sources, Path(td))
        counts["remote_errors"] = errors
        for skill_dir in _primary_skill_dirs():
            source = _read_source_file(skill_dir)
            remote: RemoteSkill | None = None
            inferred = False
            if source:
                remote = by_exact.get((source.repo, source.ref, source.path))
            elif SKILL_UPDATE_INFER_SOURCES:
                remote = by_name.get(_skill_name(skill_dir))
                inferred = remote is not None
            if not remote:
                continue
            counts["remote_checked"] = int(counts["remote_checked"]) + 1
            result = _apply_remote_update(skill_dir, remote, inferred=inferred)
            if result == "updated":
                counts["remote_updated"] = int(counts["remote_updated"]) + 1
            elif result == "no_update":
                counts["remote_no_update"] = int(counts["remote_no_update"]) + 1
            elif result.startswith("error_"):
                errors.append(result)
            else:
                counts["remote_skipped"] = int(counts["remote_skipped"]) + 1
    return counts


def _run_update() -> str:
    local = _sync_local_roots()
    remote = _update_source_tracked_skills()
    remote_errors = remote.get("remote_errors", [])
    error_count = len(remote_errors) if isinstance(remote_errors, list) else 0
    return (
        f"local_updates={local['local_updates']} mirrored={local['mirrored']} "
        f"conflicts={local['conflicts']} local_checked={local['local_checked']} "
        f"remote_updated={remote['remote_updated']} "
        f"remote_no_update={remote['remote_no_update']} "
        f"remote_skipped={remote['remote_skipped']} "
        f"remote_checked={remote['remote_checked']} errors={error_count}"
    )


def run_skill_update_pass(*, force: bool = False) -> str:
    """Run one skill update check/apply pass.

    `force=True` bypasses the interval gate but still respects the process lock.
    """
    if SKILL_UPDATE_INTERVAL_S <= 0 and not force:
        return "disabled"
    if not force:
        is_due, age = _due()
        if not is_due:
            return f"not_due age_s={age}"

    with _update_lock() as locked:
        if not locked:
            return "update_running"
        try:
            result = _run_update()
        except Exception as e:  # noqa: BLE001
            logger.debug("skill_updater: pass failed", exc_info=True)
            result = f"error {type(e).__name__}: {e}"

    _record_skill_update_pass(result)
    return result


def _serve_loop() -> None:
    while True:
        try:
            run_skill_update_pass()
        except Exception:
            logger.debug("skill_updater daemon tick failed", exc_info=True)
        daemon_sleep(SKILL_UPDATE_INTERVAL_S, idle_s=3600.0)


def start_skill_update_daemon() -> None:
    """Start the twice-weekly skill updater in foreground MCP parents."""
    global _started
    if _started:
        return
    if SKILL_UPDATE_INTERVAL_S <= 0:
        return
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(
        target=_serve_loop,
        name="skill_update_daemon",
        daemon=True,
    )
    t.start()
    _started = True
