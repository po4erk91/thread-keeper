"""Durable retention for Curator and Evolve workflow artifacts.

Workflow output differs from ordinary event telemetry: it can contain frozen
lesson/skill bodies and it also owns files outside SQLite.  This module keeps a
small lifecycle ledger for that output.  Rows are only made terminal by the
producer, retention never touches active/retryable work, and file deletion is
journaled before it is attempted so a failed or interrupted unlink cannot make
the database falsely claim success.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import time
from pathlib import Path
from typing import Iterable

from .config import CURATOR_REPORTS_DIR, DB_PATH, WORKFLOW_RETENTION_COUNT, WORKFLOW_RETENTION_DAYS


_SUMMARY_CHARS = 512


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _summary(text: str) -> str:
    return " ".join(text.split())[:_SUMMARY_CHARS]


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def register_curator_pass(
    conn: sqlite3.Connection,
    *,
    pass_id: str,
    batch_count: int,
    created_at: int,
    manifest_path: Path | None = None,
    snapshot_path: Path | None = None,
) -> None:
    """Create the active ownership row before Curator children are launched."""
    conn.execute(
        "INSERT OR REPLACE INTO curator_passes "
        "(pass_id, state, batch_count, created_at, manifest_path, snapshot_path) "
        "VALUES (?, 'active', ?, ?, ?, ?)",
        (
            pass_id,
            max(1, int(batch_count)),
            int(created_at),
            str(manifest_path) if manifest_path else None,
            str(snapshot_path) if snapshot_path else None,
        ),
    )


def register_curator_batch(
    conn: sqlite3.Connection,
    *,
    pass_id: str,
    batch_index: int,
    batch_total: int,
    batch_text: str,
    report_path: Path,
    created_at: int,
) -> None:
    """Persist the frozen batch only while the batch is still live."""
    conn.execute(
        "INSERT OR REPLACE INTO curator_batches "
        "(pass_id, batch_index, batch_total, state, batch_text, batch_sha256, "
        "batch_summary, report_path, created_at) "
        "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?)",
        (
            pass_id,
            int(batch_index),
            int(batch_total),
            batch_text,
            _sha256(batch_text),
            _summary(batch_text),
            str(report_path),
            int(created_at),
        ),
    )


def mark_curator_retryable(conn: sqlite3.Connection, pass_id: str) -> None:
    """Keep a failed dispatch out of retention until an operator resolves it."""
    conn.execute(
        "UPDATE curator_passes SET state='retryable' WHERE pass_id=? "
        "AND state='active'",
        (pass_id,),
    )
    conn.commit()


def record_curator_report(
    conn: sqlite3.Connection,
    *,
    pass_id: str,
    batch_index: int,
    batch_total: int,
    report_path: Path,
    report_text: str,
    terminal: bool,
    now: int | None = None,
) -> None:
    """Attach an authenticated report and terminalize a fully completed pass."""
    now = int(time.time()) if now is None else int(now)
    report_sha = _sha256(report_text)
    conn.execute(
        "UPDATE curator_batches SET report_path=?, report_sha256=?, "
        "state=CASE WHEN ? THEN 'terminal' ELSE state END, "
        "terminal_at=CASE WHEN ? THEN ? ELSE terminal_at END "
        "WHERE pass_id=? AND batch_index=? AND batch_total=?",
        (
            str(report_path), report_sha, int(terminal), int(terminal), now,
            pass_id, int(batch_index), int(batch_total),
        ),
    )
    if terminal:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN state='terminal' THEN 1 ELSE 0 END) AS terminal "
            "FROM curator_batches WHERE pass_id=?",
            (pass_id,),
        ).fetchone()
        total = _safe_int(row["total"] if row else 0)
        finished = _safe_int(row["terminal"] if row else 0)
        if total and total == finished:
            conn.execute(
                "UPDATE curator_passes SET state='terminal', terminal_at=? "
                "WHERE pass_id=? AND batch_count=?",
                (now, pass_id, total),
            )
    conn.commit()


def register_research_started(
    conn: sqlite3.Connection,
    *,
    research_path: Path,
    created_at: int,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO evolve_research_handoffs "
        "(research_path, state, created_at) VALUES (?, 'active', ?)",
        (str(research_path), int(created_at)),
    )
    conn.commit()


def complete_research_handoff(
    conn: sqlite3.Connection,
    *,
    research_path: Path,
    research_body: str,
    now: int | None = None,
) -> None:
    """Mark a digest terminal only after its audit child was dispatched."""
    now = int(time.time()) if now is None else int(now)
    conn.execute(
        "INSERT INTO evolve_research_handoffs "
        "(research_path, state, created_at, terminal_at, research_body, "
        "research_sha256, research_summary) VALUES (?, 'terminal', ?, ?, ?, ?, ?) "
        "ON CONFLICT(research_path) DO UPDATE SET "
        "state='terminal', terminal_at=excluded.terminal_at, "
        "research_body=excluded.research_body, "
        "research_sha256=excluded.research_sha256, "
        "research_summary=excluded.research_summary",
        (
            str(research_path), now, now, research_body, _sha256(research_body),
            _summary(research_body),
        ),
    )
    conn.commit()


def _eligible_ids(
    rows: Iterable[sqlite3.Row], *, now: int, days: float, count: int,
) -> set[str]:
    """Return terminal ids outside every enabled retention protection.

    Age and count are independent protections: with both configured, a row must
    be older than the age window *and* outside the newest count.  Setting either
    knob to zero disables that protection; setting both to zero disables pruning.
    """
    day_cutoff = now - int(float(days) * 86400) if float(days or 0) > 0 else None
    keep_count = max(0, int(count or 0))
    if day_cutoff is None and keep_count == 0:
        return set()
    out: set[str] = set()
    for position, row in enumerate(rows):
        terminal_at = _safe_int(row["terminal_at"])
        old_enough = day_cutoff is None or terminal_at < day_cutoff
        beyond_count = keep_count == 0 or position >= keep_count
        if old_enough and beyond_count:
            out.add(str(row[0]))
    return out


def _is_applied_report(
    conn: sqlite3.Connection,
    path: str,
    applied_at: object = None,
) -> bool:
    if not path:
        return True
    if _safe_int(applied_at) > 0:
        return True
    row = conn.execute(
        "SELECT 1 FROM events WHERE kind='curator_report_applied' "
        "AND target=? LIMIT 1",
        (path,),
    ).fetchone()
    return row is not None


def _curator_candidates(
    conn: sqlite3.Connection, *, now: int, days: float, count: int,
) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM curator_passes WHERE state='terminal' "
        "ORDER BY terminal_at DESC, pass_id DESC"
    ).fetchall()
    candidate_ids = _eligible_ids(rows, now=now, days=days, count=count)
    out: list[sqlite3.Row] = []
    for row in rows:
        if row["pass_id"] not in candidate_ids:
            continue
        batches = conn.execute(
            "SELECT state, report_path, report_applied_at "
            "FROM curator_batches WHERE pass_id=?",
            (row["pass_id"],),
        ).fetchall()
        if not batches or any(batch["state"] != "terminal" for batch in batches):
            continue
        if any(
            not _is_applied_report(
                conn, batch["report_path"] or "", batch["report_applied_at"],
            )
            for batch in batches
        ):
            continue
        out.append(row)
    return out


def _research_candidates(
    conn: sqlite3.Connection, *, now: int, days: float, count: int,
) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM evolve_research_handoffs WHERE state='terminal' "
        "ORDER BY terminal_at DESC, research_path DESC"
    ).fetchall()
    ids = _eligible_ids(rows, now=now, days=days, count=count)
    return [row for row in rows if row["research_path"] in ids]


def _path_size_no_follow(path: str, root: Path) -> tuple[int, str]:
    target, error = _contained_path(path, root)
    if error:
        return 0, error
    try:
        mode = os.lstat(target).st_mode
    except FileNotFoundError:
        return 0, "missing"
    except OSError as exc:
        return 0, f"stat:{exc.__class__.__name__}"
    if stat.S_ISLNK(mode):
        return 0, "symlink"
    if stat.S_ISREG(mode):
        return os.lstat(target).st_size, ""
    if not stat.S_ISDIR(mode):
        return 0, "not_regular"
    total = 0
    for base, dirs, files in os.walk(target, followlinks=False):
        for name in [*dirs, *files]:
            child = Path(base) / name
            try:
                child_stat = os.lstat(child)
            except OSError:
                return total, "walk_failed"
            if stat.S_ISLNK(child_stat.st_mode):
                return total, "symlink"
            if stat.S_ISREG(child_stat.st_mode):
                total += child_stat.st_size
    return total, ""


def _contained_path(path: str, root: Path) -> tuple[Path, str]:
    """Validate containment lexically and reject every symlink component."""
    root = root.expanduser().absolute()
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = root / target
    target = target.absolute()
    try:
        rel = target.relative_to(root)
    except ValueError:
        return target, "outside_root"
    try:
        root_mode = os.lstat(root).st_mode
    except FileNotFoundError:
        return target, "root_missing"
    except OSError as exc:
        return target, f"root_stat:{exc.__class__.__name__}"
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        return target, "unsafe_root"
    current = root
    for part in rel.parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            break
        except OSError as exc:
            return target, f"stat:{exc.__class__.__name__}"
        if stat.S_ISLNK(mode):
            return target, "symlink"
    return target, ""


def _journal_delete(
    conn: sqlite3.Connection, *, path: str, root: Path, now: int,
) -> tuple[bool, int, str]:
    """Delete a configured-owned path using a durable pending/done journal."""
    target, error = _contained_path(path, root)
    if error:
        return False, 0, error
    size, error = _path_size_no_follow(path, root)
    if error not in ("", "missing"):
        return False, 0, error
    conn.execute(
        "INSERT INTO workflow_artifact_deletions "
        "(path, root_path, state, bytes, created_at, updated_at) "
        "VALUES (?, ?, 'pending', ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET updated_at=excluded.updated_at",
        (str(target), str(root.expanduser().absolute()), size, now, now),
    )
    conn.commit()
    if error == "missing":
        conn.execute(
            "UPDATE workflow_artifact_deletions SET state='deleted', error=NULL, "
            "updated_at=? WHERE path=?",
            (now, str(target)),
        )
        conn.commit()
        return True, 0, ""
    try:
        mode = os.lstat(target).st_mode
        if stat.S_ISREG(mode):
            os.unlink(target)
        elif stat.S_ISDIR(mode):
            # The no-follow preflight above rejects any link in the tree.
            shutil.rmtree(target)
        else:
            raise OSError("refusing non-regular workflow artifact")
    except OSError as exc:
        conn.execute(
            "UPDATE workflow_artifact_deletions SET state='failed', error=?, "
            "updated_at=? WHERE path=?",
            (exc.__class__.__name__, now, str(target)),
        )
        conn.commit()
        return False, 0, f"delete:{exc.__class__.__name__}"
    conn.execute(
        "UPDATE workflow_artifact_deletions SET state='deleted', error=NULL, "
        "updated_at=? WHERE path=?",
        (now, str(target)),
    )
    conn.commit()
    return True, size, ""


def _artifact_paths_for_pass(conn: sqlite3.Connection, row: sqlite3.Row) -> list[tuple[str, Path, str, tuple]]:
    items: list[tuple[str, Path, str, tuple]] = []
    for column in ("manifest_path", "snapshot_path"):
        path = row[column] or ""
        if path:
            items.append((path, CURATOR_REPORTS_DIR, "curator_passes", (column, row["pass_id"])))
    batches = conn.execute(
        "SELECT batch_index, report_path FROM curator_batches WHERE pass_id=?",
        (row["pass_id"],),
    ).fetchall()
    for batch in batches:
        path = batch["report_path"] or ""
        if path:
            items.append((path, CURATOR_REPORTS_DIR, "curator_batches", ("report_path", row["pass_id"], batch["batch_index"])))
    return items


def _preview(conn: sqlite3.Connection, *, now: int, days: float, count: int) -> dict[str, int | str]:
    passes = _curator_candidates(conn, now=now, days=days, count=count)
    research = _research_candidates(conn, now=now, days=days, count=count)
    files = 0
    bytes_total = 0
    refused = 0
    for row in passes:
        for path, root, _table, _locator in _artifact_paths_for_pass(conn, row):
            size, error = _path_size_no_follow(path, root)
            if error in ("", "missing"):
                files += 1
                bytes_total += size
            else:
                refused += 1
    for row in research:
        size, error = _path_size_no_follow(row["research_path"], DB_PATH.parent / "evolve-research")
        if error in ("", "missing"):
            files += 1
            bytes_total += size
        else:
            refused += 1
    oldest = conn.execute(
        "SELECT MIN(terminal_at) FROM curator_passes WHERE state='terminal'"
    ).fetchone()[0]
    return {
        "candidate_rows": len(passes) + len(research),
        "candidate_passes": len(passes),
        "candidate_research": len(research),
        "candidate_files": files,
        "candidate_bytes": bytes_total,
        "refused_files": refused,
        "oldest_terminal_pass": _safe_int(oldest),
    }


def workflow_retention_preview(conn: sqlite3.Connection | None = None) -> str:
    own_conn = conn is None
    if conn is None:
        from .db import get_db
        conn = get_db()
    try:
        stats = _preview(
            conn, now=int(time.time()), days=WORKFLOW_RETENTION_DAYS,
            count=WORKFLOW_RETENTION_COUNT,
        )
        return _format_stats("dry_run", stats)
    finally:
        if own_conn:
            conn.close()


def _compact_pass(conn: sqlite3.Connection, pass_id: str, now: int) -> int:
    rows = conn.execute(
        "SELECT batch_text, compacted_at FROM curator_batches WHERE pass_id=?",
        (pass_id,),
    ).fetchall()
    compacted = 0
    for row in rows:
        if row["compacted_at"] is None:
            conn.execute(
                "UPDATE curator_batches SET batch_text='', compacted_at=? "
                "WHERE pass_id=? AND compacted_at IS NULL",
                (now, pass_id),
            )
            compacted += 1
    conn.execute(
        "UPDATE curator_passes SET compacted_at=? WHERE pass_id=? "
        "AND compacted_at IS NULL",
        (now, pass_id),
    )
    conn.commit()
    return compacted


def _clear_artifact_locator(conn: sqlite3.Connection, table: str, locator: tuple) -> None:
    column = locator[0]
    if table == "curator_passes":
        conn.execute(f"UPDATE {table} SET {column}=NULL WHERE pass_id=?", (locator[1],))
    else:
        conn.execute(
            f"UPDATE {table} SET {column}=NULL WHERE pass_id=? AND batch_index=?",
            (locator[1], locator[2]),
        )
    conn.commit()


def _pass_ready_to_delete(conn: sqlite3.Connection, pass_id: str, was_compacted: bool) -> bool:
    if not was_compacted:
        return False
    row = conn.execute(
        "SELECT manifest_path, snapshot_path FROM curator_passes WHERE pass_id=?",
        (pass_id,),
    ).fetchone()
    if row is None or row["manifest_path"] or row["snapshot_path"]:
        return False
    row = conn.execute(
        "SELECT COUNT(*) FROM curator_batches WHERE pass_id=? "
        "AND (report_path IS NOT NULL OR compacted_at IS NULL)",
        (pass_id,),
    ).fetchone()
    return _safe_int(row[0] if row else 1) == 0


def _format_stats(mode: str, stats: dict[str, int | str]) -> str:
    return (
        f"{mode} workflow_retention rows={stats['candidate_rows']} "
        f"passes={stats['candidate_passes']} research={stats['candidate_research']} "
        f"files={stats['candidate_files']} bytes={stats['candidate_bytes']} "
        f"refused_files={stats['refused_files']} "
        f"oldest_terminal_pass={stats['oldest_terminal_pass']}"
    )


def run_workflow_retention(
    conn: sqlite3.Connection, *, dry_run: bool = False, now: int | None = None,
) -> str:
    """Compact then remove eligible terminal workflow output.

    The first successful pass compacts durable bodies and deletes owned files.
    A later retry removes the now-hash-only rows.  This deliberate two-step
    lifecycle leaves an inspectable audit stub between compaction and row
    removal, while active/retryable and unapplied report rows never enter the
    candidate set.
    """
    now = int(time.time()) if now is None else int(now)
    stats = _preview(
        conn, now=now, days=WORKFLOW_RETENTION_DAYS,
        count=WORKFLOW_RETENTION_COUNT,
    )
    if dry_run:
        return _format_stats("dry_run", stats)
    deleted_rows = 0
    compacted = 0
    file_errors = 0
    for row in _curator_candidates(
        conn, now=now, days=WORKFLOW_RETENTION_DAYS, count=WORKFLOW_RETENTION_COUNT,
    ):
        was_compacted = row["compacted_at"] is not None
        compacted += _compact_pass(conn, row["pass_id"], now)
        for path, root, table, locator in _artifact_paths_for_pass(conn, row):
            ok, _bytes, _error = _journal_delete(conn, path=path, root=root, now=now)
            if ok:
                _clear_artifact_locator(conn, table, locator)
            else:
                file_errors += 1
        if _pass_ready_to_delete(conn, row["pass_id"], was_compacted):
            conn.execute("DELETE FROM curator_batches WHERE pass_id=?", (row["pass_id"],))
            conn.execute("DELETE FROM curator_passes WHERE pass_id=?", (row["pass_id"],))
            conn.commit()
            deleted_rows += 1
    for row in _research_candidates(
        conn, now=now, days=WORKFLOW_RETENTION_DAYS, count=WORKFLOW_RETENTION_COUNT,
    ):
        was_compacted = row["compacted_at"] is not None
        if not was_compacted:
            conn.execute(
                "UPDATE evolve_research_handoffs SET research_body='', compacted_at=? "
                "WHERE research_path=? AND compacted_at IS NULL",
                (now, row["research_path"]),
            )
            conn.commit()
            compacted += 1
        ok, _bytes, _error = _journal_delete(
            conn,
            path=row["research_path"],
            root=DB_PATH.parent / "evolve-research",
            now=now,
        )
        if ok:
            conn.execute(
                "UPDATE evolve_research_handoffs SET research_path_deleted=1 "
                "WHERE research_path=?",
                (row["research_path"],),
            )
            conn.commit()
        else:
            file_errors += 1
        if was_compacted and ok:
            conn.execute(
                "DELETE FROM evolve_research_handoffs WHERE research_path=?",
                (row["research_path"],),
            )
            conn.commit()
            deleted_rows += 1
    outcome = _format_stats("ok", stats) + (
        f" compacted={compacted} deleted_rows={deleted_rows} file_errors={file_errors}"
    )
    conn.execute(
        "INSERT INTO workflow_retention_state (name, last_at, last_outcome) "
        "VALUES ('workflow', ?, ?) ON CONFLICT(name) DO UPDATE SET "
        "last_at=excluded.last_at, last_outcome=excluded.last_outcome",
        (now, outcome[:1000]),
    )
    conn.commit()
    return outcome


def workflow_retention_status(conn: sqlite3.Connection) -> str:
    preview = _preview(
        conn, now=int(time.time()), days=WORKFLOW_RETENTION_DAYS,
        count=WORKFLOW_RETENTION_COUNT,
    )
    row = conn.execute(
        "SELECT last_at, last_outcome FROM workflow_retention_state WHERE name='workflow'"
    ).fetchone()
    last_at = _safe_int(row["last_at"] if row else 0)
    last = (row["last_outcome"] if row else "none") or "none"
    return (
        f"workflow_retention candidates={preview['candidate_rows']} "
        f"files={preview['candidate_files']} bytes={preview['candidate_bytes']} "
        f"oldest_terminal_pass={preview['oldest_terminal_pass']} "
        f"last_at={last_at} last={last}"
    )


def selective_erase_count(conn: sqlite3.Connection, selector: str) -> int:
    """Count frozen workflow bodies containing a source selected for erasure."""
    if not selector:
        return 0
    try:
        batches = conn.execute(
            "SELECT COUNT(*) FROM curator_batches WHERE instr(batch_text, ?) > 0",
            (selector,),
        ).fetchone()[0]
        research = conn.execute(
            "SELECT COUNT(*) FROM evolve_research_handoffs "
            "WHERE instr(research_body, ?) > 0",
            (selector,),
        ).fetchone()[0]
    except sqlite3.Error:
        return 0
    return _safe_int(batches) + _safe_int(research)


def selectively_erase_bodies(conn: sqlite3.Connection, selector: str) -> int:
    """Remove matching derived bodies immediately, including active workflow.

    Selective erasure is stronger than normal retention: a frozen prompt or
    handoff that repeats the selector cannot remain merely because its pass is
    still active. Hashes stay as non-reversible audit/dedup evidence.
    """
    if not selector:
        return 0
    now = int(time.time())
    try:
        batches = conn.execute(
            "UPDATE curator_batches SET batch_text='', "
            "batch_summary='[selectively erased]', compacted_at=? "
            "WHERE instr(batch_text, ?) > 0",
            (now, selector),
        ).rowcount
        research = conn.execute(
            "UPDATE evolve_research_handoffs SET research_body='', "
            "research_summary='[selectively erased]', compacted_at=? "
            "WHERE instr(research_body, ?) > 0",
            (now, selector),
        ).rowcount
    except sqlite3.Error:
        return 0
    return _safe_int(batches) + _safe_int(research)
