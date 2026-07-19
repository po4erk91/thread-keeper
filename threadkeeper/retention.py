"""SQLite retention and compaction hygiene.

The daemon defaults off, and most destructive windows default to 0 (disabled)
so upgrades keep existing data until a user opts in. Completed task-row
retention shares the spawn-task cleanup knob, which defaults to 30 days, and
only runs here when the retention pass itself is enabled or forced. When
enabled, this module prunes high-volume operational tables and can
checkpoint/VACUUM the single-file SQLite store.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

from . import daemon_state, identity
from .config import (
    BACKGROUND_DAEMONS_ALLOWED,
    DIALOG_RETENTION_DAYS,
    EVENTS_RETENTION_DAYS,
    PROBE_RESULT_RETENTION_DAYS,
    RETENTION_INTERVAL_S,
    RETENTION_VACUUM_AFTER_ROWS,
    RETENTION_WAL_CHECKPOINT,
    SIGNAL_RETENTION_DAYS,
    TASK_RETENTION_DAYS,
)
from .db import get_db
from .helpers import daemon_sleep

logger = logging.getLogger(__name__)

_started = False
_BATCH = 1000


def _placeholders(n: int) -> str:
    return ",".join("?" for _ in range(n))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=? LIMIT 1",
            (name,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _age_cutoff(now: int, days: float) -> int | None:
    days = float(days or 0)
    if days <= 0:
        return None
    return now - int(days * 86400)


def _delete_dialog_sidecars(conn: sqlite3.Connection, uuids: list[str]) -> None:
    if not uuids:
        return
    ph = _placeholders(len(uuids))
    # dialog_fts is external-content and trigger-synced (schema v2): the
    # AFTER DELETE trigger on dialog_messages removes the FTS entry when
    # _prune_dialog deletes the row itself. Only vec sidecars need manual
    # cleanup here (vectors have no trigger).

    if not _table_exists(conn, "dialog_vec_map"):
        return
    try:
        rows = conn.execute(
            f"SELECT rowid FROM dialog_vec_map WHERE uuid IN ({ph})",
            uuids,
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    rowids = [int(r[0]) for r in rows]
    if rowids and _table_exists(conn, "dialog_vec"):
        try:
            conn.execute(
                f"DELETE FROM dialog_vec WHERE rowid IN ({_placeholders(len(rowids))})",
                rowids,
            )
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute(f"DELETE FROM dialog_vec_map WHERE uuid IN ({ph})", uuids)
    except sqlite3.OperationalError:
        pass


def _prune_dialog(conn: sqlite3.Connection, cutoff: int | None) -> int:
    if cutoff is None:
        return 0
    deleted = 0
    while True:
        rows = conn.execute(
            "SELECT uuid FROM dialog_messages WHERE created_at < ? "
            "ORDER BY created_at ASC LIMIT ?",
            (cutoff, _BATCH),
        ).fetchall()
        uuids = [r["uuid"] if hasattr(r, "keys") else r[0] for r in rows]
        if not uuids:
            break
        _delete_dialog_sidecars(conn, uuids)
        cur = conn.execute(
            f"DELETE FROM dialog_messages WHERE uuid IN ({_placeholders(len(uuids))})",
            uuids,
        )
        deleted += int(cur.rowcount or 0)
        conn.commit()
    return deleted


def _delete_count(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple = (),
) -> int:
    try:
        cur = conn.execute(sql, params)
    except sqlite3.OperationalError:
        return 0
    return int(cur.rowcount or 0)


def _prune_probe_results(conn: sqlite3.Connection, cutoff: int | None) -> int:
    if cutoff is None:
        return 0
    try:
        rows = conn.execute(
            "SELECT DISTINCT category FROM probe_results WHERE created_at < ?",
            (cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    categories = [r["category"] if hasattr(r, "keys") else r[0] for r in rows]
    deleted = _delete_count(
        conn,
        "DELETE FROM probe_results WHERE created_at < ?",
        (cutoff,),
    )
    if deleted and categories:
        try:
            from .tools.probes import _recompute_reliability

            for category in categories:
                _recompute_reliability(conn, category)
        except Exception:
            logger.debug("retention: probe reliability recompute failed", exc_info=True)
    return deleted


def _checkpoint_wal(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.OperationalError as e:
        return f"err:{e.__class__.__name__}"
    if row is None:
        return "ok"
    return f"ok busy={row[0]} log={row[1]} checkpointed={row[2]}"


# Event kinds whose `target` is a dialog_messages ingest-order rowid watermark
# (issue #69). dialog_messages has a TEXT primary key, so its rowid is
# implicit and SQLite's VACUUM contract permits renumbering it once dialog
# deletions have left gaps — which would silently invalidate these cursors.
_DIALOG_CURSOR_EVENT_KINDS = (
    "shadow_review_pass",
    "dialectic_mine_pass",
    "extract_pass",
)


def _rebase_dialog_cursors_to_created_at(conn: sqlite3.Connection) -> int:
    """Rewrite rowid watermarks as legacy created_at values before a VACUUM.

    A created_at value (>= helpers.LEGACY_TS_FLOOR) survives rowid
    renumbering: the next cursor read runs it through
    `resolve_ingest_watermark`, which translates it back to the matching
    post-VACUUM rowid. Granularity note: a late-ingested row whose created_at
    is below the rebased timestamp is skipped once — the same one-time cost
    as the original #69 legacy migration — which is why this runs only around
    an actual VACUUM instead of storing created_at cursors permanently.
    Returns the number of cursors rebased."""
    from .helpers import LEGACY_TS_FLOOR, resolve_ingest_watermark

    rebased = 0
    for kind in _DIALOG_CURSOR_EVENT_KINDS:
        try:
            row = conn.execute(
                "SELECT target FROM events WHERE kind=? ORDER BY id DESC LIMIT 1",
                (kind,),
            ).fetchone()
        except sqlite3.OperationalError:
            return rebased
        target = (row[0] if row else None) or ""
        try:
            stored = int(target)
        except (TypeError, ValueError):
            continue
        if stored <= 0:
            continue
        rowid = resolve_ingest_watermark(conn, stored)
        if rowid <= 0:
            continue
        try:
            ts_row = conn.execute(
                "SELECT MAX(created_at) FROM dialog_messages WHERE rowid <= ?",
                (rowid,),
            ).fetchone()
        except sqlite3.OperationalError:
            continue
        ts = ts_row[0] if ts_row else None
        if ts is None or int(ts) < LEGACY_TS_FLOOR:
            continue
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?,?,?,?,?)",
            (
                identity._session_id or "",
                kind,
                str(int(ts)),
                f"vacuum_cursor_rebase rowid={rowid}",
                int(time.time()),
            ),
        )
        rebased += 1
    if rebased:
        conn.commit()
    return rebased


def _rebuild_content_fts(conn: sqlite3.Connection) -> str:
    """Rebuild every external-content FTS index keyed by an implicit rowid."""
    rebuilt: list[str] = []
    for fts in ("dialog_fts", "notes_fts"):
        try:
            conn.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
            rebuilt.append(fts)
        except sqlite3.OperationalError:
            continue
    if rebuilt:
        conn.commit()
    return "+".join(rebuilt) or "none"


def _vacuum(conn: sqlite3.Connection) -> str:
    """VACUUM with the implicit-rowid safety protocol.

    dialog_messages and (post re-id migration) notes have TEXT primary keys,
    so VACUUM may renumber their implicit rowids once deletions left gaps.
    Three things depend on those rowids: the shadow/miner/extract ingest
    cursors and the external-content dialog_fts / notes_fts indexes
    (`content_rowid='rowid'`). Protocol: rebase the cursors to created_at
    form first, VACUUM, then rebuild the FTS indexes. Mirrors the manual
    `db_compact` tool."""
    rebased = _rebase_dialog_cursors_to_created_at(conn)
    try:
        conn.commit()  # VACUUM cannot run inside a transaction
        conn.execute("VACUUM")
    except sqlite3.OperationalError as e:
        return f"err:{e.__class__.__name__}"
    rebuilt = _rebuild_content_fts(conn)
    return f"ok rebased_cursors={rebased} fts_rebuild={rebuilt}"


def _record_pass(conn: sqlite3.Connection, summary: str) -> None:
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, 'retention_pass', '', ?, ?)",
            (identity._session_id or "", summary[:300], int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("retention: failed to record pass", exc_info=True)


def run_retention_pass(force: bool = False, *, scheduled: bool = False) -> str:
    """Run one retention/compaction pass.

    Returns a compact status string. Destructive table pruning runs only for
    windows whose day knob is >0. `force=True` bypasses the daemon interval
    switch for tests/manual calls, but does not override individual windows.
    A scheduled tick returns 'not_due' when another server already ran this
    loop within the interval (daemon_state).
    """
    if RETENTION_INTERVAL_S <= 0 and not force:
        return "disabled"

    conn = get_db()
    if not daemon_state.claim_pass(
        "retention", RETENTION_INTERVAL_S, scheduled=scheduled, conn=conn,
    ):
        return "not_due"
    now = int(time.time())
    counts = {
        "dialog": _prune_dialog(conn, _age_cutoff(now, DIALOG_RETENTION_DAYS)),
        "tasks": _delete_count(
            conn,
            "DELETE FROM tasks WHERE ended_at IS NOT NULL AND ended_at < ?",
            (_age_cutoff(now, TASK_RETENTION_DAYS) or -1,),
        )
        if TASK_RETENTION_DAYS > 0
        else 0,
        "signals": _delete_count(
            conn,
            "DELETE FROM signals WHERE created_at < ? "
            "AND (read_at IS NOT NULL OR kind IN ('search_request','search_response'))",
            (_age_cutoff(now, SIGNAL_RETENTION_DAYS) or -1,),
        )
        if SIGNAL_RETENTION_DAYS > 0
        else 0,
        "events": _delete_count(
            conn,
            "DELETE FROM events WHERE created_at < ?",
            (_age_cutoff(now, EVENTS_RETENTION_DAYS) or -1,),
        )
        if EVENTS_RETENTION_DAYS > 0
        else 0,
        "probe_results": _prune_probe_results(
            conn, _age_cutoff(now, PROBE_RESULT_RETENTION_DAYS)
        ),
    }
    total = sum(counts.values())
    conn.commit()

    vacuum = "skip"
    if RETENTION_VACUUM_AFTER_ROWS > 0 and total >= RETENTION_VACUUM_AFTER_ROWS:
        vacuum = _vacuum(conn)

    checkpoint = "skip"
    if RETENTION_WAL_CHECKPOINT:
        checkpoint = _checkpoint_wal(conn)

    summary = (
        "deleted "
        + " ".join(f"{name}={count}" for name, count in counts.items())
        + f" total={total} vacuum={vacuum} wal_checkpoint={checkpoint}"
    )
    _record_pass(conn, summary)
    return summary


def _serve_loop() -> None:
    while True:
        try:
            run_retention_pass(scheduled=True)
        except Exception:
            logger.debug("retention tick failed", exc_info=True)
        daemon_sleep(RETENTION_INTERVAL_S)


def start_retention_daemon() -> None:
    """Idempotent foreground-only retention daemon starter."""
    global _started
    if _started:
        return
    if RETENTION_INTERVAL_S <= 0:
        return
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(target=_serve_loop, name="retention", daemon=True)
    t.start()
    _started = True
