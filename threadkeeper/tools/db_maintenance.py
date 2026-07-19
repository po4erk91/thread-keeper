"""One-shot DB maintenance: reclaim disk after the schema-v2 FTS dedup.

Dropping v1's dialog_fts shadow copy freed ~465MB of pages INSIDE the file;
the file itself shrinks only on VACUUM. VACUUM takes an exclusive lock and
is permitted to renumber dialog_messages' implicit rowids (not guaranteed
stable; preserved on the builds we tested) — which would desync the
external-content dialog_fts index — so this is deliberately an explicit,
operator-run tool (never an automatic pass) and the FTS rebuild after
VACUUM is mandatory (defensive), not optional."""
from __future__ import annotations

import sqlite3
import time

from .._mcp import write_tool
from ..config import DB_PATH
from ..db import get_db, vec_available
from ..helpers import single_flight_lock
from ..identity import _ensure_session


def _embedding_dedup_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Count BLOB rows/bytes that already have an equivalent vec0 row."""
    from ..embeddings import _notes_mapped

    if _notes_mapped(conn):
        note_join = (
            "JOIN notes_vec_map m ON m.gid=n.id "
            "JOIN notes_vec v ON v.rowid=m.rowid"
        )
    else:
        note_join = "JOIN notes_vec v ON v.id=n.id"
    note = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(length(n.embedding)),0) "
        f"FROM notes n {note_join} WHERE n.embedding IS NOT NULL"
    ).fetchone()
    dialog = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(length(d.embedding)),0) "
        "FROM dialog_messages d "
        "JOIN dialog_vec_map m ON m.uuid=d.uuid "
        "JOIN dialog_vec v ON v.rowid=m.rowid "
        "WHERE d.embedding IS NOT NULL"
    ).fetchone()
    blob = conn.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM notes WHERE embedding IS NOT NULL), "
        "(SELECT COUNT(*) FROM dialog_messages WHERE embedding IS NOT NULL)"
    ).fetchone()
    return {
        "notes_eligible": int(note[0]),
        "notes_bytes": int(note[1]),
        "notes_uncovered": int(blob[0]) - int(note[0]),
        "dialog_eligible": int(dialog[0]),
        "dialog_bytes": int(dialog[1]),
        "dialog_uncovered": int(blob[1]) - int(dialog[0]),
    }


def _format_embedding_dedup(stats: dict[str, int], *, dry_run: bool) -> str:
    total_rows = stats["notes_eligible"] + stats["dialog_eligible"]
    total_bytes = stats["notes_bytes"] + stats["dialog_bytes"]
    uncovered = stats["notes_uncovered"] + stats["dialog_uncovered"]
    return (
        f"{'dry_run' if dry_run else 'ok'} embedding_dedup "
        f"rows={total_rows} bytes={total_bytes} "
        f"notes={stats['notes_eligible']} dialog={stats['dialog_eligible']} "
        f"uncovered={uncovered}"
    )


@write_tool(idempotent=True)
def db_deduplicate_embeddings(dry_run: bool = True) -> str:
    """Remove base-table embedding BLOBs already represented in sqlite-vec.

    The operation is coverage-gated: rows without a confirmed vec0 mirror keep
    their BLOB fallback. Defaults to a report-only dry run. Run ``db_compact``
    afterwards to return the newly freed pages to the filesystem.
    """
    with single_flight_lock("db-embedding-dedup") as locked:
        if not locked:
            return "db_deduplicate_embeddings already running"
        conn = get_db()
        try:
            _ensure_session(conn)
            if not vec_available():
                return "embedding_dedup skipped: sqlite-vec unavailable"
            before = _embedding_dedup_stats(conn)
            if dry_run:
                return _format_embedding_dedup(before, dry_run=True)
            from ..embeddings import _notes_mapped
            from ..sync.capture import applying_guard

            with applying_guard(conn):
                if _notes_mapped(conn):
                    conn.execute(
                        "UPDATE notes SET embedding=NULL "
                        "WHERE embedding IS NOT NULL AND EXISTS ("
                        "SELECT 1 FROM notes_vec_map m "
                        "JOIN notes_vec v ON v.rowid=m.rowid "
                        "WHERE m.gid=notes.id)"
                    )
                else:
                    conn.execute(
                        "UPDATE notes SET embedding=NULL "
                        "WHERE embedding IS NOT NULL AND EXISTS ("
                        "SELECT 1 FROM notes_vec v WHERE v.id=notes.id)"
                    )
                conn.execute(
                    "UPDATE dialog_messages SET embedding=NULL "
                    "WHERE embedding IS NOT NULL AND EXISTS ("
                    "SELECT 1 FROM dialog_vec_map m "
                    "JOIN dialog_vec v ON v.rowid=m.rowid "
                    "WHERE m.uuid=dialog_messages.uuid)"
                )
                conn.commit()
            return _format_embedding_dedup(before, dry_run=False)
        finally:
            conn.close()


@write_tool(idempotent=True)
def db_compact() -> str:
    """Shrink the DB file: VACUUM + mandatory dialog_fts rebuild.

    Run in a quiet window — VACUUM needs an exclusive lock and copies the
    whole file (minutes on a multi-GB DB); concurrent FTS searches during
    the vacuum→rebuild gap may map to wrong rows until the rebuild commits.
    Fails soft (with a retry hint) when the DB is busy."""
    with single_flight_lock("db-compact") as locked:
        if not locked:
            return "db_compact already running (single-flight lock held)"
        conn = get_db()
        try:
            _ensure_session(conn)
            before = DB_PATH.stat().st_size
            t0 = time.time()
            conn.commit()  # VACUUM cannot run inside a transaction
            try:
                conn.execute("VACUUM")
            except sqlite3.OperationalError as e:
                return (
                    f"vacuum skipped: {e} — DB busy; retry in a quiet window "
                    f"(no rowids were changed, index is still consistent)"
                )
            # MANDATORY: VACUUM may have renumbered dialog_messages'
            # implicit rowids (SQLite's contract permits it; preserved on
            # the builds we tested); the external-content index maps by
            # rowid and could now be stale — rebuild is defensive.
            conn.execute("INSERT INTO dialog_fts(dialog_fts) VALUES('rebuild')")
            conn.commit()
            after = DB_PATH.stat().st_size
            return (
                f"ok vacuum+fts_rebuild {time.time() - t0:.1f}s "
                f"size {before / 1e6:.0f}MB -> {after / 1e6:.0f}MB "
                f"(freed {(before - after) / 1e6:.0f}MB)"
            )
        finally:
            conn.close()
