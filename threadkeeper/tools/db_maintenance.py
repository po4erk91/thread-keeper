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
from ..db import get_db
from ..helpers import single_flight_lock
from ..identity import _ensure_session


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
