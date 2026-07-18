"""Targeted privacy erasure for one conversation/session/topic selector."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import time
from typing import Iterable, TypeVar


SELECTOR_TYPES = {"auto", "session_id", "cid", "thread_id", "uuid"}
_BATCH = 400
T = TypeVar("T")


@dataclass
class ReviewRef:
    kind: str
    name: str
    detail: str = ""


@dataclass
class ForgetPlan:
    selector: str
    selector_type: str
    dialog_uuids: list[str] = field(default_factory=list)
    # notes/verbatim/dialectic_* are re-id'd INTEGER->TEXT by the sync migration,
    # so their ids are strings post-migration (and plain str(int) pre-migration,
    # which SQLite matches against an INTEGER column via affinity). Node-local
    # tables (signals/events/extract_candidates) stay integer-keyed below.
    note_ids: list[str] = field(default_factory=list)
    thread_ids: list[str] = field(default_factory=list)
    verbatim_ids: list[str] = field(default_factory=list)
    observation_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)
    extract_candidate_ids: list[int] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    task_spool_paths: list[Path] = field(default_factory=list)
    signal_ids: list[int] = field(default_factory=list)
    event_ids: list[int] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    cursor_ids: list[str] = field(default_factory=list)
    presence_ids: list[str] = field(default_factory=list)
    lesson_refs: list[ReviewRef] = field(default_factory=list)
    skill_refs: list[ReviewRef] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def _chunks(items: Iterable[T], n: int = _BATCH) -> Iterable[list[T]]:
    chunk: list[T] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= n:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _placeholders(n: int) -> str:
    return ",".join("?" for _ in range(n))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=? LIMIT 1",
            (name,),
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def _fetch_col(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple = (),
    *,
    cast=int,
) -> list:
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    return [cast(row[0]) for row in rows]


def _fetch_where_in(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    values: Iterable,
    out_column: str,
    *,
    cast=int,
) -> list:
    out = []
    vals = list(dict.fromkeys(values))
    if not vals:
        return out
    if not _table_exists(conn, table):
        return out
    for batch in _chunks(vals):
        ph = _placeholders(len(batch))
        out.extend(
            _fetch_col(
                conn,
                f"SELECT {out_column} FROM {table} WHERE {column} IN ({ph})",
                tuple(batch),
                cast=cast,
            )
        )
    return out


def _delete_where_in(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    values: Iterable,
) -> int:
    total = 0
    vals = list(dict.fromkeys(values))
    if not vals or not _table_exists(conn, table):
        return 0
    for batch in _chunks(vals):
        ph = _placeholders(len(batch))
        cur = conn.execute(
            f"DELETE FROM {table} WHERE {column} IN ({ph})",
            tuple(batch),
        )
        total += int(cur.rowcount or 0)
    return total


def _auto_selector_type(conn: sqlite3.Connection, selector: str) -> str:
    if conn.execute(
        "SELECT 1 FROM threads WHERE id=? LIMIT 1",
        (selector,),
    ).fetchone():
        return "thread_id"
    if conn.execute(
        "SELECT 1 FROM dialog_messages WHERE uuid=? LIMIT 1",
        (selector,),
    ).fetchone():
        return "uuid"
    return "session_id"


def _resolve_selector_type(
    conn: sqlite3.Connection,
    selector: str,
    selector_type: str,
) -> str:
    st = (selector_type or "auto").strip().lower()
    if st not in SELECTOR_TYPES:
        raise ValueError(
            f"selector_type must be one of {', '.join(sorted(SELECTOR_TYPES))}"
        )
    if st == "cid":
        return "session_id"
    if st == "auto":
        return _auto_selector_type(conn, selector)
    return st


def _dialog_uuids(
    conn: sqlite3.Connection,
    selector: str,
    selector_type: str,
) -> list[str]:
    if selector_type == "uuid":
        return _fetch_col(
            conn,
            "SELECT uuid FROM dialog_messages WHERE uuid=?",
            (selector,),
            cast=str,
        )
    if selector_type == "session_id":
        return _fetch_col(
            conn,
            "SELECT uuid FROM dialog_messages WHERE session_id=?",
            (selector,),
            cast=str,
        )
    return []


def _notes(
    conn: sqlite3.Connection,
    selector: str,
    selector_type: str,
) -> tuple[list[str], list[str]]:
    if selector_type == "thread_id":
        return (
            _fetch_col(
                conn,
                "SELECT id FROM notes WHERE thread_id=?",
                (selector,),
                cast=str,
            ),
            [selector],
        )
    if selector_type == "session_id":
        return (
            _fetch_col(
                conn,
                "SELECT id FROM notes WHERE session_id=?",
                (selector,),
                cast=str,
            ),
            [],
        )
    return [], []


def _verbatim_ids(
    conn: sqlite3.Connection,
    selector: str,
    selector_type: str,
) -> list[str]:
    if selector_type == "thread_id":
        return _fetch_col(
            conn,
            "SELECT id FROM verbatim WHERE thread_id=?",
            (selector,),
            cast=str,
        )
    if selector_type == "session_id":
        return _fetch_col(
            conn,
            "SELECT id FROM verbatim WHERE session_id=?",
            (selector,),
            cast=str,
        )
    return []


def _task_ids(
    conn: sqlite3.Connection,
    selector: str,
    selector_type: str,
) -> list[str]:
    if selector_type != "session_id":
        return []
    return _fetch_col(
        conn,
        "SELECT id FROM tasks WHERE parent_cid=? OR spawned_cid=?",
        (selector, selector),
        cast=str,
    )


def _task_spool_paths(task_ids: list[str]) -> list[Path]:
    if not task_ids:
        return []
    from .config import TASK_LOG_DIR

    try:
        from .task_spool import ensure_task_spool_dir

        root = ensure_task_spool_dir(TASK_LOG_DIR)
    except (OSError, PermissionError):
        return []
    out: list[Path] = []
    for task_id in task_ids:
        for name in (
            f"{task_id}.log",
            f"{task_id}.stdin.txt",
            f"{task_id}.command",
            f"slim-mcp-{task_id}.json",
            f"gh-safe-{task_id}",
        ):
            path = root / name
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            out.append(path)
    return out


def _source_tokens(plan: ForgetPlan) -> set[str]:
    tokens = {plan.selector, f"session:{plan.selector}", f"cid:{plan.selector}"}
    tokens.update(f"dialog:{uuid}" for uuid in plan.dialog_uuids)
    tokens.update(plan.dialog_uuids)
    tokens.update(f"verbatim:{vid}" for vid in plan.verbatim_ids)
    tokens.update(f"thread:{tid}" for tid in plan.thread_ids)
    tokens.update(plan.thread_ids)
    tokens.update(f"task:{tid}" for tid in plan.task_ids)
    tokens.update(plan.task_ids)
    return {t for t in tokens if t}


def _dialectic_targets(conn: sqlite3.Connection, plan: ForgetPlan) -> None:
    if not _table_exists(conn, "dialectic_observations"):
        return
    obs_ids = set(
        _fetch_col(
            conn,
            "SELECT id FROM dialectic_observations WHERE source_cid=?",
            (plan.selector,),
            cast=str,
        )
    )
    obs_ids.update(
        _fetch_where_in(
            conn,
            "dialectic_observations",
            "dialog_uuid",
            plan.dialog_uuids,
            "id",
            cast=str,
        )
    )
    plan.observation_ids = sorted(obs_ids)

    if not _table_exists(conn, "dialectic_evidence"):
        return
    source_tokens = _source_tokens(plan)
    evidence_rows: list[sqlite3.Row] = []
    for batch in _chunks(sorted(source_tokens)):
        ph = _placeholders(len(batch))
        evidence_rows.extend(
            conn.execute(
                "SELECT id, claim_id FROM dialectic_evidence "
                f"WHERE source IN ({ph})",
                tuple(batch),
            ).fetchall()
        )
    evidence_ids = {str(r["id"]) for r in evidence_rows}
    affected_claim_ids = {str(r["claim_id"]) for r in evidence_rows}

    claim_ids = set()
    if _table_exists(conn, "user_dialectic"):
        if plan.selector_type == "session_id":
            claim_ids.update(
                _fetch_col(
                    conn,
                    "SELECT id FROM user_dialectic WHERE created_by_cid=?",
                    (plan.selector,),
                    cast=str,
                )
            )
        for claim_id in affected_claim_ids:
            all_ids = _fetch_col(
                conn,
                "SELECT id FROM dialectic_evidence WHERE claim_id=?",
                (claim_id,),
                cast=str,
            )
            if all_ids and set(all_ids).issubset(evidence_ids):
                claim_ids.add(claim_id)

    if claim_ids:
        evidence_ids.update(
            _fetch_where_in(
                conn,
                "dialectic_evidence",
                "claim_id",
                claim_ids,
                "id",
                cast=str,
            )
        )
    plan.evidence_ids = sorted(evidence_ids)
    plan.claim_ids = sorted(claim_ids)


def _extract_candidate_ids(conn: sqlite3.Connection, plan: ForgetPlan) -> list[int]:
    ids = set()
    if not _table_exists(conn, "extract_candidates"):
        return []
    if plan.selector_type == "session_id":
        ids.update(
            _fetch_col(
                conn,
                "SELECT id FROM extract_candidates WHERE source_cid=?",
                (plan.selector,),
                cast=int,
            )
        )
    ids.update(
        _fetch_where_in(
            conn,
            "extract_candidates",
            "source_uuid",
            plan.dialog_uuids,
            "id",
            cast=int,
        )
    )
    return sorted(ids)


def _signal_ids(conn: sqlite3.Connection, plan: ForgetPlan) -> list[int]:
    if plan.selector_type != "session_id" or not _table_exists(conn, "signals"):
        return []
    return _fetch_col(
        conn,
        "SELECT id FROM signals WHERE from_cid=? OR to_cid=?",
        (plan.selector, plan.selector),
        cast=int,
    )


def _session_sidecar_ids(conn: sqlite3.Connection, plan: ForgetPlan) -> None:
    if plan.selector_type != "session_id":
        return
    if _table_exists(conn, "events"):
        plan.event_ids = _fetch_col(
            conn,
            "SELECT id FROM events WHERE session_id=?",
            (plan.selector,),
            cast=int,
        )
    if _table_exists(conn, "sessions"):
        plan.session_ids = _fetch_col(
            conn,
            "SELECT id FROM sessions WHERE id=?",
            (plan.selector,),
            cast=str,
        )
    if _table_exists(conn, "cursors"):
        plan.cursor_ids = _fetch_col(
            conn,
            "SELECT session_id FROM cursors WHERE session_id=?",
            (plan.selector,),
            cast=str,
        )
    if _table_exists(conn, "presence"):
        plan.presence_ids = _fetch_col(
            conn,
            "SELECT session_id FROM presence WHERE session_id=?",
            (plan.selector,),
            cast=str,
        )


def _scan_lessons(plan: ForgetPlan) -> list[ReviewRef]:
    tokens = _source_tokens(plan)
    if not tokens:
        return []
    try:
        from .lessons import iter_lessons
    except Exception:
        return []
    refs: list[ReviewRef] = []
    for item in iter_lessons():
        source = str(item.get("source") or "")
        body = str(item.get("body") or "")
        if source in tokens or any(token in body for token in tokens):
            refs.append(
                ReviewRef(
                    kind="lesson",
                    name=str(item.get("slug") or ""),
                    detail=f"source={source}" if source else "",
                )
            )
    return refs


def _skill_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()

    def add(root: Path | None) -> None:
        if root is None:
            return
        try:
            expanded = root.expanduser()
            key = expanded.resolve()
        except OSError:
            return
        if key in seen:
            return
        seen.add(key)
        roots.append(expanded)

    try:
        from .adapters import ADAPTERS
        from .config import CLAUDE_SKILLS_DIR, DB_PATH

        add(CLAUDE_SKILLS_DIR)
        for adapter in ADAPTERS:
            add(adapter.skills_dir())
        raw = os.environ.get("THREADKEEPER_EXTRA_SKILLS_DIRS", "").strip()
        if raw:
            for item in raw.split(os.pathsep):
                if item.strip():
                    add(Path(item.strip()))
        agents_root = Path("~/.agents/skills").expanduser()
        if agents_root.exists():
            add(agents_root)
        add(DB_PATH.parent / "skills")
    except Exception:
        return roots
    return roots


def _scan_skills(plan: ForgetPlan) -> list[ReviewRef]:
    tokens = _source_tokens(plan)
    if not tokens:
        return []
    refs: list[ReviewRef] = []
    seen: set[Path] = set()
    for root in _skill_roots():
        if not root.exists() or not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for child in children:
            md = child / "SKILL.md"
            if not md.is_file():
                continue
            try:
                key = md.resolve()
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            try:
                body = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(token in body for token in tokens):
                refs.append(
                    ReviewRef(
                        kind="skill",
                        name=child.name,
                        detail=str(md),
                    )
                )
    return refs


def _count_existing_paths(paths: list[Path]) -> int:
    n = 0
    for path in paths:
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        n += 1
    return n


def _count_vec_rows(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    values: Iterable,
) -> int:
    return len(_fetch_where_in(conn, table, column, values, column, cast=int))


def _count_dialog_vec(conn: sqlite3.Connection, uuids: list[str]) -> tuple[int, int]:
    rowids = _fetch_where_in(
        conn,
        "dialog_vec_map",
        "uuid",
        uuids,
        "rowid",
        cast=int,
    )
    vec = _count_vec_rows(conn, "dialog_vec", "rowid", rowids)
    return vec, len(rowids)


def _count_docsize(conn: sqlite3.Connection, table: str, ids: list[int]) -> int:
    if not ids or not _table_exists(conn, table):
        return 0
    return _count_vec_rows(conn, table, "id", ids)


def _note_rowids(conn: sqlite3.Connection, note_ids: list[str]) -> list[int]:
    """Resolve note ids (TEXT gids post-migration, ints pre-) to notes.rowid,
    the integer key used by notes_fts (content_rowid) and — pre-migration —
    notes_vec."""
    return _fetch_where_in(conn, "notes", "id", note_ids, "rowid", cast=int)


def _count_notes_vec(conn: sqlite3.Connection, note_ids: list[str]) -> tuple[int, int]:
    """(notes_vec rows, notes_vec_map rows) for the given notes. Post-migration
    notes_vec is keyed by an integer rowid resolved through notes_vec_map.gid;
    pre-migration it is keyed directly by the integer note id."""
    from .embeddings import _notes_mapped
    if _notes_mapped(conn):
        rowids = _fetch_where_in(
            conn, "notes_vec_map", "gid", note_ids, "rowid", cast=int,
        )
        return _count_vec_rows(conn, "notes_vec", "rowid", rowids), len(rowids)
    return _count_vec_rows(conn, "notes_vec", "id", note_ids), 0


def _count_notes_fts(conn: sqlite3.Connection, note_ids: list[str]) -> int:
    """notes_fts_docsize rows for the given notes, counted by notes.rowid (the
    FTS content_rowid) — NOT the TEXT note id."""
    return _count_docsize(conn, "notes_fts_docsize", _note_rowids(conn, note_ids))


def _populate_counts(conn: sqlite3.Connection, plan: ForgetPlan) -> None:
    dialog_vec, dialog_vec_map = _count_dialog_vec(conn, plan.dialog_uuids)
    _notes_vec_count, _notes_vec_map_count = _count_notes_vec(conn, plan.note_ids)
    counts = {
        "dialog_messages": len(plan.dialog_uuids),
        "dialog_fts": _count_docsize(
            conn,
            "dialog_fts_docsize",
            _fetch_where_in(
                conn,
                "dialog_messages",
                "uuid",
                plan.dialog_uuids,
                "rowid",
                cast=int,
            ),
        ),
        "dialog_vec": dialog_vec,
        "dialog_vec_map": dialog_vec_map,
        "notes": len(plan.note_ids),
        "notes_fts": _count_notes_fts(conn, plan.note_ids),
        "notes_vec": _notes_vec_count,
        "notes_vec_map": _notes_vec_map_count,
        "threads": len(plan.thread_ids) if plan.selector_type == "thread_id" else 0,
        "verbatim": len(plan.verbatim_ids),
        "dialectic_observations": len(plan.observation_ids),
        "dialectic_evidence": len(plan.evidence_ids),
        "user_dialectic": len(plan.claim_ids),
        "extract_candidates": len(plan.extract_candidate_ids),
        "tasks": len(plan.task_ids),
        "task_spool_paths": _count_existing_paths(plan.task_spool_paths),
        "signals": len(plan.signal_ids),
        "events": len(plan.event_ids),
        "sessions": len(plan.session_ids),
        "cursors": len(plan.cursor_ids),
        "presence": len(plan.presence_ids),
    }
    plan.counts = counts


def build_forget_plan(
    conn: sqlite3.Connection,
    selector: str,
    selector_type: str = "auto",
) -> ForgetPlan:
    clean_selector = (selector or "").strip()
    if not clean_selector:
        raise ValueError("selector is required")
    resolved = _resolve_selector_type(conn, clean_selector, selector_type)
    plan = ForgetPlan(selector=clean_selector, selector_type=resolved)
    plan.dialog_uuids = _dialog_uuids(conn, clean_selector, resolved)
    plan.note_ids, plan.thread_ids = _notes(conn, clean_selector, resolved)
    plan.verbatim_ids = _verbatim_ids(conn, clean_selector, resolved)
    plan.task_ids = _task_ids(conn, clean_selector, resolved)
    plan.task_spool_paths = _task_spool_paths(plan.task_ids)
    _dialectic_targets(conn, plan)
    plan.extract_candidate_ids = _extract_candidate_ids(conn, plan)
    plan.signal_ids = _signal_ids(conn, plan)
    _session_sidecar_ids(conn, plan)
    plan.lesson_refs = _scan_lessons(plan)
    plan.skill_refs = _scan_skills(plan)
    _populate_counts(conn, plan)
    return plan


def _unlink_spool_path(path: Path) -> bool:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode):
        path.unlink()
        return True
    if stat.S_ISREG(st.st_mode):
        path.unlink()
        return True
    if stat.S_ISDIR(st.st_mode) and path.name.startswith("gh-safe-"):
        shutil.rmtree(path)
        return True
    return False


def _delete_dialog_sidecars(
    conn: sqlite3.Connection,
    uuids: list[str],
) -> tuple[int, int]:
    if not uuids or not _table_exists(conn, "dialog_vec_map"):
        return 0, 0
    rowids = _fetch_where_in(
        conn,
        "dialog_vec_map",
        "uuid",
        uuids,
        "rowid",
        cast=int,
    )
    vec_deleted = _delete_where_in(conn, "dialog_vec", "rowid", rowids)
    map_deleted = _delete_where_in(conn, "dialog_vec_map", "uuid", uuids)
    return vec_deleted, map_deleted


def _delete_note_sidecars(
    conn: sqlite3.Connection,
    note_ids: list[str],
) -> tuple[int, int]:
    """Delete a note's vec sidecars. Returns (notes_vec, notes_vec_map). Post-
    migration notes_vec is keyed by an integer rowid via notes_vec_map.gid;
    pre-migration it is keyed directly by the integer note id (no map)."""
    from .embeddings import _notes_mapped
    if not _notes_mapped(conn):
        return _delete_where_in(conn, "notes_vec", "id", note_ids), 0
    rowids = _fetch_where_in(
        conn, "notes_vec_map", "gid", note_ids, "rowid", cast=int,
    )
    vec_deleted = _delete_where_in(conn, "notes_vec", "rowid", rowids)
    map_deleted = _delete_where_in(conn, "notes_vec_map", "gid", note_ids)
    return vec_deleted, map_deleted


def _recompute_claims(conn: sqlite3.Connection, claim_ids: set[str]) -> None:
    if not claim_ids:
        return
    try:
        from .tools.dialectic import _recompute_confidence, _recompute_tier
    except Exception:
        _recompute_confidence = None
        _recompute_tier = None
    now_t = int(time.time())
    for claim_id in sorted(claim_ids):
        if not conn.execute(
            "SELECT 1 FROM user_dialectic WHERE id=?",
            (claim_id,),
        ).fetchone():
            continue
        row = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN kind='support' THEN 1 ELSE 0 END) AS support, "
            "  SUM(CASE WHEN kind='contradict' THEN 1 ELSE 0 END) AS contradict, "
            "  MAX(created_at) AS last_at "
            "FROM dialectic_evidence WHERE claim_id=?",
            (claim_id,),
        ).fetchone()
        conn.execute(
            "UPDATE user_dialectic SET support_count=?, contradict_count=?, "
            "last_evidence_at=? WHERE id=?",
            (
                int(row["support"] or 0),
                int(row["contradict"] or 0),
                row["last_at"],
                claim_id,
            ),
        )
        if _recompute_confidence is not None:
            _recompute_confidence(conn, claim_id)
        if _recompute_tier is not None:
            _recompute_tier(conn, claim_id, now_t)


def _apply_forget(conn: sqlite3.Connection, plan: ForgetPlan) -> dict[str, int]:
    deleted: dict[str, int] = {}
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        deleted["task_spool_paths"] = sum(
            1 for path in plan.task_spool_paths if _unlink_spool_path(path)
        )
        deleted["dialog_fts"] = int(plan.counts.get("dialog_fts") or 0)
        deleted["notes_fts"] = int(plan.counts.get("notes_fts") or 0)
        dialog_vec, dialog_vec_map = _delete_dialog_sidecars(
            conn,
            plan.dialog_uuids,
        )
        deleted["dialog_vec"] = dialog_vec
        deleted["dialog_vec_map"] = dialog_vec_map
        notes_vec, notes_vec_map = _delete_note_sidecars(conn, plan.note_ids)
        deleted["notes_vec"] = notes_vec
        deleted["notes_vec_map"] = notes_vec_map
        deleted["dialectic_observations"] = _delete_where_in(
            conn,
            "dialectic_observations",
            "id",
            plan.observation_ids,
        )
        affected_claims = set(
            _fetch_where_in(
                conn,
                "dialectic_evidence",
                "id",
                plan.evidence_ids,
                "claim_id",
                cast=str,
            )
        )
        deleted["dialectic_evidence"] = _delete_where_in(
            conn,
            "dialectic_evidence",
            "id",
            plan.evidence_ids,
        )
        deleted["user_dialectic"] = _delete_where_in(
            conn,
            "user_dialectic",
            "id",
            plan.claim_ids,
        )
        affected_claims.difference_update(plan.claim_ids)
        _recompute_claims(conn, affected_claims)
        deleted["extract_candidates"] = _delete_where_in(
            conn,
            "extract_candidates",
            "id",
            plan.extract_candidate_ids,
        )
        deleted["verbatim"] = _delete_where_in(
            conn,
            "verbatim",
            "id",
            plan.verbatim_ids,
        )
        deleted["notes"] = _delete_where_in(conn, "notes", "id", plan.note_ids)
        deleted["threads"] = (
            _delete_where_in(conn, "threads", "id", plan.thread_ids)
            if plan.selector_type == "thread_id"
            else 0
        )
        deleted["dialog_messages"] = _delete_where_in(
            conn,
            "dialog_messages",
            "uuid",
            plan.dialog_uuids,
        )
        deleted["tasks"] = _delete_where_in(conn, "tasks", "id", plan.task_ids)
        deleted["signals"] = _delete_where_in(
            conn,
            "signals",
            "id",
            plan.signal_ids,
        )
        deleted["events"] = _delete_where_in(
            conn,
            "events",
            "id",
            plan.event_ids,
        )
        deleted["cursors"] = _delete_where_in(
            conn,
            "cursors",
            "session_id",
            plan.cursor_ids,
        )
        deleted["presence"] = _delete_where_in(
            conn,
            "presence",
            "session_id",
            plan.presence_ids,
        )
        deleted["sessions"] = _delete_where_in(
            conn,
            "sessions",
            "id",
            plan.session_ids,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return deleted


def _orphan_counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {
        "dialog_fts_orphans": 0,
        "dialog_vec_orphans": 0,
        "dialog_vec_map_orphans": 0,
        "notes_fts_orphans": 0,
        "notes_vec_orphans": 0,
        "notes_vec_map_orphans": 0,
    }
    try:
        if _table_exists(conn, "dialog_fts_docsize"):
            out["dialog_fts_orphans"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM dialog_fts_docsize ds "
                    "LEFT JOIN dialog_messages d ON d.rowid=ds.id "
                    "WHERE d.rowid IS NULL"
                ).fetchone()[0]
            )
    except sqlite3.OperationalError:
        pass
    try:
        if _table_exists(conn, "dialog_vec_map"):
            out["dialog_vec_map_orphans"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM dialog_vec_map m "
                    "LEFT JOIN dialog_messages d ON d.uuid=m.uuid "
                    "WHERE d.uuid IS NULL"
                ).fetchone()[0]
            )
    except sqlite3.OperationalError:
        pass
    try:
        if _table_exists(conn, "dialog_vec") and _table_exists(
            conn,
            "dialog_vec_map",
        ):
            out["dialog_vec_orphans"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM dialog_vec v "
                    "LEFT JOIN dialog_vec_map m ON m.rowid=v.rowid "
                    "WHERE m.rowid IS NULL"
                ).fetchone()[0]
            )
    except sqlite3.OperationalError:
        pass
    try:
        if _table_exists(conn, "notes_fts_docsize"):
            out["notes_fts_orphans"] = int(
                conn.execute(
                    # docsize.id is the FTS content_rowid == notes.rowid, NOT the
                    # (post-migration TEXT) notes.id.
                    "SELECT COUNT(*) FROM notes_fts_docsize ds "
                    "LEFT JOIN notes n ON n.rowid=ds.id "
                    "WHERE n.rowid IS NULL"
                ).fetchone()[0]
            )
    except sqlite3.OperationalError:
        pass
    try:
        from .embeddings import _notes_mapped
        if _table_exists(conn, "notes_vec"):
            if _notes_mapped(conn):
                # notes_vec.rowid -> notes_vec_map.rowid -> gid -> notes.id
                out["notes_vec_orphans"] = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM notes_vec v "
                        "LEFT JOIN notes_vec_map m ON m.rowid=v.rowid "
                        "LEFT JOIN notes n ON n.id=m.gid "
                        "WHERE n.id IS NULL"
                    ).fetchone()[0]
                )
                out["notes_vec_map_orphans"] = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM notes_vec_map m "
                        "LEFT JOIN notes n ON n.id=m.gid "
                        "WHERE n.id IS NULL"
                    ).fetchone()[0]
                )
            else:
                out["notes_vec_orphans"] = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM notes_vec v "
                        "LEFT JOIN notes n ON n.id=v.id "
                        "WHERE n.id IS NULL"
                    ).fetchone()[0]
                )
    except sqlite3.OperationalError:
        pass
    return out


def _format_refs(refs: list[ReviewRef], limit: int = 12) -> list[str]:
    lines = []
    for ref in refs[:limit]:
        detail = f" {ref.detail}" if ref.detail else ""
        lines.append(f"  {ref.kind} {ref.name}{detail}")
    if len(refs) > limit:
        lines.append(f"  ... {len(refs) - limit} more")
    return lines


def format_forget_report(
    plan: ForgetPlan,
    *,
    dry_run: bool,
    deleted: dict[str, int] | None = None,
    orphans: dict[str, int] | None = None,
) -> str:
    mode = "dry_run" if dry_run else "applied"
    verb = "would_delete" if dry_run else "deleted"
    counts = plan.counts if dry_run else (deleted or {})
    out = [
        f"forget selector_type={plan.selector_type} mode={mode}",
        f"{verb}:",
    ]
    for name in sorted(counts):
        if int(counts[name] or 0):
            out.append(f"  {name}={int(counts[name])}")
    if len(out) == 2:
        out.append("  none=0")
    review_n = len(plan.lesson_refs) + len(plan.skill_refs)
    out.append(
        f"review_required lessons={len(plan.lesson_refs)} "
        f"skills={len(plan.skill_refs)}"
    )
    out.extend(_format_refs(plan.lesson_refs))
    out.extend(_format_refs(plan.skill_refs))
    if orphans:
        out.append(
            "residuals "
            + " ".join(f"{k}={v}" for k, v in sorted(orphans.items()))
        )
    if dry_run:
        out.append("pass dry_run=False or run tk-forget --apply to delete")
    elif review_n:
        out.append("review listed lessons/skills before keeping or editing them")
    return "\n".join(out)


def forget_selector(
    selector: str,
    *,
    selector_type: str = "auto",
    dry_run: bool = True,
    conn: sqlite3.Connection | None = None,
) -> str:
    own_conn = conn is None
    if conn is None:
        from .db import get_db

        conn = get_db()
    try:
        plan = build_forget_plan(conn, selector, selector_type)
        if dry_run:
            return format_forget_report(
                plan,
                dry_run=True,
                orphans=_orphan_counts(conn),
            )
        deleted = _apply_forget(conn, plan)
        return format_forget_report(
            plan,
            dry_run=False,
            deleted=deleted,
            orphans=_orphan_counts(conn),
        )
    finally:
        if own_conn:
            conn.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tk-forget",
        description=(
            "Dry-run or apply targeted erasure for one thread-keeper "
            "session/cid/thread/dialog UUID."
        ),
    )
    parser.add_argument("selector", help="session id, cid, thread id, or dialog uuid")
    parser.add_argument(
        "--selector-type",
        choices=sorted(SELECTOR_TYPES),
        default="auto",
        help="how to interpret selector (default: auto)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete rows; omitted means dry-run only",
    )
    parser.add_argument("--db", help="override THREADKEEPER_DB")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.db:
        os.environ["THREADKEEPER_DB"] = args.db
    try:
        print(
            forget_selector(
                args.selector,
                selector_type=args.selector_type,
                dry_run=not args.apply,
            )
        )
    except Exception as exc:
        print(f"ERR forget_failed={exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
