"""Retention for terminal Curator and Evolve workflow artifacts."""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import time
from pathlib import Path
from typing import Iterable

from .config import (
    CURATOR_REPORTS_DIR,
    DB_PATH,
    WORKFLOW_RETENTION_COUNT,
    WORKFLOW_RETENTION_DAYS,
)


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


def record_research_consumed(
    conn: sqlite3.Connection,
    *,
    research_path: Path,
    research_body: str,
    now: int | None = None,
) -> None:
    """Record a handoff body only after an audit child consumes it.

    The handoff's authorization and status remain owned by ``evolve_daemon``.
    Retention data is additive so it cannot change that security boundary.
    """
    now = int(time.time()) if now is None else int(now)
    target = str(research_path.resolve(strict=False))
    conn.execute(
        "UPDATE evolve_research_handoffs SET retention_at=?, retention_body=?, "
        "retention_summary=?, retention_compacted_at=NULL, "
        "retention_path_deleted=0 WHERE target_path=? AND status='accepted'",
        (now, research_body, _summary(research_body), target),
    )
    conn.commit()


def _eligible_ids(
    rows: Iterable[sqlite3.Row],
    *,
    now: int,
    days: float,
    count: int,
) -> set[str]:
    """Return terminal artifacts outside every enabled retention protection."""
    day_cutoff = now - int(float(days) * 86400) if float(days or 0) > 0 else None
    keep_count = max(0, int(count or 0))
    if day_cutoff is None and keep_count == 0:
        return set()
    eligible: set[str] = set()
    for position, row in enumerate(rows):
        old_enough = (
            day_cutoff is None or _safe_int(row["terminal_at"]) < day_cutoff
        )
        beyond_count = keep_count == 0 or position >= keep_count
        if old_enough and beyond_count:
            eligible.add(str(row["pass_id"]))
    return eligible


def _curator_candidates(
    conn: sqlite3.Connection,
    *,
    now: int,
    days: float,
    count: int,
) -> list[sqlite3.Row]:
    """Return complete, applied Curator passes old enough to compact."""
    rows = conn.execute(
        "SELECT p.*, COALESCE(p.endorsed_at, p.completed_at) AS terminal_at "
        "FROM curator_passes p WHERE p.endorsed_at IS NOT NULL "
        "AND p.abandoned_at IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM curator_batches b WHERE b.pass_id=p.pass_id "
        "AND (b.state<>'complete' OR b.apply_state<>'applied')) "
        "ORDER BY terminal_at DESC, p.pass_id DESC"
    ).fetchall()
    eligible = _eligible_ids(rows, now=now, days=days, count=count)
    return [row for row in rows if row["pass_id"] in eligible]


def _research_candidates(
    conn: sqlite3.Connection,
    *,
    now: int,
    days: float,
    count: int,
) -> list[sqlite3.Row]:
    """Return consumed Evolve handoffs old enough to compact."""
    rows = conn.execute(
        "SELECT *, retention_at AS terminal_at FROM evolve_research_handoffs "
        "WHERE retention_at IS NOT NULL ORDER BY retention_at DESC, pass_id DESC"
    ).fetchall()
    eligible = _eligible_ids(rows, now=now, days=days, count=count)
    return [row for row in rows if row["pass_id"] in eligible]


def _contained_path(path: str | Path, root: Path) -> tuple[Path, str]:
    """Validate configured-root containment and reject symlink components."""
    root = root.expanduser().absolute()
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = root / target
    target = target.absolute()
    try:
        target.relative_to(root)
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
    for part in target.relative_to(root).parts:
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


def _path_size_no_follow(path: str | Path, root: Path) -> tuple[int, str]:
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
                child_mode = os.lstat(child).st_mode
            except OSError:
                return total, "walk_failed"
            if stat.S_ISLNK(child_mode):
                return total, "symlink"
            if stat.S_ISREG(child_mode):
                total += os.lstat(child).st_size
    return total, ""


def _journal_delete(
    conn: sqlite3.Connection,
    *,
    path: str | Path,
    root: Path,
    now: int,
) -> tuple[bool, int, str]:
    """Delete one owned path with a durable pending/done journal."""
    target, error = _contained_path(path, root)
    if error:
        return False, 0, error
    size, error = _path_size_no_follow(target, root)
    if error not in ("", "missing"):
        return False, 0, error
    conn.execute(
        "INSERT INTO workflow_artifact_deletions "
        "(path, root_path, state, bytes, created_at, updated_at) "
        "VALUES (?, ?, 'pending', ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
        "state='pending', bytes=excluded.bytes, error=NULL, "
        "updated_at=excluded.updated_at",
        (str(target), str(root.expanduser().absolute()), size, now, now),
    )
    conn.commit()
    if error == "missing":
        conn.execute(
            "UPDATE workflow_artifact_deletions SET state='deleted', "
            "error=NULL, updated_at=? WHERE path=?",
            (now, str(target)),
        )
        conn.commit()
        return True, 0, ""
    try:
        mode = os.lstat(target).st_mode
        if stat.S_ISREG(mode):
            os.unlink(target)
        elif stat.S_ISDIR(mode):
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


def _artifact_paths_for_pass(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> list[tuple[Path, Path]]:
    paths: list[tuple[Path, Path]] = []
    for column in ("audit_manifest_path", "snapshot_path"):
        path = row[column] or ""
        if path:
            paths.append((Path(path), CURATOR_REPORTS_DIR))
    total = int(row["expected_batches"])
    batches = conn.execute(
        "SELECT batch_index, report_name, research_state FROM curator_batches "
        "WHERE pass_id=? ORDER BY batch_index",
        (row["pass_id"],),
    ).fetchall()
    for batch in batches:
        paths.append((CURATOR_REPORTS_DIR / batch["report_name"], CURATOR_REPORTS_DIR))
        # The verifier's report quotes memory text too, so it ages out with
        # the rest of the pass. Only batches that ran a verifier have one.
        verify_report = CURATOR_REPORTS_DIR / (
            "VERIFY-" + batch["report_name"].removeprefix("REPORT-")
        )
        if verify_report.exists():
            paths.append((verify_report, CURATOR_REPORTS_DIR))
        if batch["research_state"] == "complete":
            name = (
                f"RESEARCH-{row['pass_id']}-batch-{batch['batch_index']:03d}-"
                f"of-{total:03d}.json"
            )
            paths.append((CURATOR_REPORTS_DIR / name, CURATOR_REPORTS_DIR))
    return paths


def _deleted_in_journal(conn: sqlite3.Connection, path: Path, root: Path) -> bool:
    target, error = _contained_path(path, root)
    if error:
        return False
    row = conn.execute(
        "SELECT state FROM workflow_artifact_deletions WHERE path=?",
        (str(target),),
    ).fetchone()
    return bool(row and row["state"] == "deleted")


def _preview(
    conn: sqlite3.Connection,
    *,
    now: int,
    days: float,
    count: int,
) -> dict[str, int]:
    passes = _curator_candidates(conn, now=now, days=days, count=count)
    research = _research_candidates(conn, now=now, days=days, count=count)
    files = 0
    bytes_total = 0
    refused = 0
    for row in passes:
        for path, root in _artifact_paths_for_pass(conn, row):
            size, error = _path_size_no_follow(path, root)
            if error in ("", "missing"):
                files += 1
                bytes_total += size
            else:
                refused += 1
    research_root = DB_PATH.parent / "evolve-research"
    for row in research:
        size, error = _path_size_no_follow(row["target_path"], research_root)
        if error in ("", "missing"):
            files += 1
            bytes_total += size
        else:
            refused += 1
    oldest = conn.execute(
        "SELECT MIN(COALESCE(endorsed_at, completed_at)) FROM curator_passes "
        "WHERE endorsed_at IS NOT NULL AND abandoned_at IS NULL"
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


def _compact_pass(conn: sqlite3.Connection, pass_id: str, now: int) -> int:
    rows = conn.execute(
        "SELECT batch_index, batch_text FROM curator_batches WHERE pass_id=? "
        "AND workflow_compacted_at IS NULL",
        (pass_id,),
    ).fetchall()
    for row in rows:
        body = row["batch_text"] or ""
        conn.execute(
            "UPDATE curator_batches SET batch_text='', workflow_batch_sha256=?, "
            "workflow_batch_summary=?, workflow_compacted_at=? "
            "WHERE pass_id=? AND batch_index=?",
            (_sha256(body), _summary(body), now, pass_id, row["batch_index"]),
        )
    conn.execute(
        "INSERT INTO workflow_retention_passes (pass_id, compacted_at) "
        "VALUES (?, ?) ON CONFLICT(pass_id) DO NOTHING",
        (pass_id, now),
    )
    conn.commit()
    return len(rows)


def _pass_was_compacted(conn: sqlite3.Connection, pass_id: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM workflow_retention_passes WHERE pass_id=?", (pass_id,)
    ).fetchone())


def _pass_ready_to_delete(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> bool:
    remaining = conn.execute(
        "SELECT COUNT(*) FROM curator_batches WHERE pass_id=? "
        "AND workflow_compacted_at IS NULL",
        (row["pass_id"],),
    ).fetchone()[0]
    if remaining:
        return False
    return all(
        _deleted_in_journal(conn, path, root)
        for path, root in _artifact_paths_for_pass(conn, row)
    )


def _format_stats(mode: str, stats: dict[str, int]) -> str:
    return (
        f"{mode} workflow_retention rows={stats['candidate_rows']} "
        f"passes={stats['candidate_passes']} research={stats['candidate_research']} "
        f"files={stats['candidate_files']} bytes={stats['candidate_bytes']} "
        f"refused_files={stats['refused_files']} "
        f"oldest_terminal_pass={stats['oldest_terminal_pass']}"
    )


def workflow_retention_preview(conn: sqlite3.Connection | None = None) -> str:
    own_conn = conn is None
    if conn is None:
        from .db import get_db

        conn = get_db()
    try:
        return _format_stats(
            "dry_run",
            _preview(
                conn,
                now=int(time.time()),
                days=WORKFLOW_RETENTION_DAYS,
                count=WORKFLOW_RETENTION_COUNT,
            ),
        )
    finally:
        if own_conn:
            conn.close()


def run_workflow_retention(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    now: int | None = None,
) -> str:
    """Compact then remove eligible terminal workflow output."""
    now = int(time.time()) if now is None else int(now)
    stats = _preview(
        conn,
        now=now,
        days=WORKFLOW_RETENTION_DAYS,
        count=WORKFLOW_RETENTION_COUNT,
    )
    if dry_run:
        return _format_stats("dry_run", stats)
    compacted = 0
    deleted_rows = 0
    file_errors = 0
    for row in _curator_candidates(
        conn,
        now=now,
        days=WORKFLOW_RETENTION_DAYS,
        count=WORKFLOW_RETENTION_COUNT,
    ):
        was_compacted = _pass_was_compacted(conn, row["pass_id"])
        compacted += _compact_pass(conn, row["pass_id"], now)
        for path, root in _artifact_paths_for_pass(conn, row):
            ok, _size, _error = _journal_delete(
                conn, path=path, root=root, now=now,
            )
            if not ok:
                file_errors += 1
        if was_compacted and _pass_ready_to_delete(conn, row):
            conn.execute(
                "DELETE FROM curator_batches WHERE pass_id=?", (row["pass_id"],)
            )
            conn.execute(
                "DELETE FROM curator_passes WHERE pass_id=?", (row["pass_id"],)
            )
            conn.execute(
                "DELETE FROM workflow_retention_passes WHERE pass_id=?",
                (row["pass_id"],),
            )
            conn.commit()
            deleted_rows += 1
    research_root = DB_PATH.parent / "evolve-research"
    for row in _research_candidates(
        conn,
        now=now,
        days=WORKFLOW_RETENTION_DAYS,
        count=WORKFLOW_RETENTION_COUNT,
    ):
        was_compacted = row["retention_compacted_at"] is not None
        if not was_compacted:
            body = row["retention_body"] or ""
            conn.execute(
                "UPDATE evolve_research_handoffs SET retention_body='', "
                "retention_summary=?, retention_compacted_at=? WHERE pass_id=?",
                (_summary(body), now, row["pass_id"]),
            )
            conn.commit()
            compacted += 1
        ok, _size, _error = _journal_delete(
            conn,
            path=row["target_path"],
            root=research_root,
            now=now,
        )
        if not ok:
            file_errors += 1
            continue
        conn.execute(
            "UPDATE evolve_research_handoffs SET retention_path_deleted=1 "
            "WHERE pass_id=?",
            (row["pass_id"],),
        )
        conn.commit()
        if was_compacted:
            conn.execute(
                "DELETE FROM evolve_research_handoffs WHERE pass_id=?",
                (row["pass_id"],),
            )
            conn.commit()
            deleted_rows += 1
    outcome = _format_stats("ok", stats) + (
        f" compacted={compacted} deleted_rows={deleted_rows} "
        f"file_errors={file_errors}"
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
        conn,
        now=int(time.time()),
        days=WORKFLOW_RETENTION_DAYS,
        count=WORKFLOW_RETENTION_COUNT,
    )
    row = conn.execute(
        "SELECT last_at, last_outcome FROM workflow_retention_state "
        "WHERE name='workflow'"
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
    if not selector:
        return 0
    try:
        batches = conn.execute(
            "SELECT COUNT(*) FROM curator_batches WHERE instr(batch_text, ?) > 0",
            (selector,),
        ).fetchone()[0]
        research = conn.execute(
            "SELECT COUNT(*) FROM evolve_research_handoffs "
            "WHERE instr(retention_body, ?) > 0",
            (selector,),
        ).fetchone()[0]
    except sqlite3.Error:
        return 0
    return _safe_int(batches) + _safe_int(research)


def selectively_erase_bodies(conn: sqlite3.Connection, selector: str) -> int:
    """Immediately compact matching frozen workflow bodies."""
    if not selector:
        return 0
    now = int(time.time())
    try:
        batches = conn.execute(
            "SELECT pass_id, batch_index, batch_text FROM curator_batches "
            "WHERE instr(batch_text, ?) > 0",
            (selector,),
        ).fetchall()
        for batch in batches:
            conn.execute(
                "UPDATE curator_batches SET batch_text='', "
                "workflow_batch_sha256=?, "
                "workflow_batch_summary='[selectively erased]', "
                "workflow_compacted_at=? WHERE pass_id=? AND batch_index=?",
                (
                    _sha256(batch["batch_text"] or ""),
                    now,
                    batch["pass_id"],
                    batch["batch_index"],
                ),
            )
        research = conn.execute(
            "UPDATE evolve_research_handoffs SET retention_body='', "
            "retention_summary='[selectively erased]', retention_compacted_at=? "
            "WHERE instr(retention_body, ?) > 0",
            (now, selector),
        ).rowcount
    except sqlite3.Error:
        return 0
    return len(batches) + _safe_int(research)
