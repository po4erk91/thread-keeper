"""Change capture for cross-machine sync — active only on a migrated DB.

Every local write to a replicated table must (a) get a global write timestamp
(HLC) + author (node_id) stamped on the row, and (b) leave a trail in
`sync_oplog` so peers can pull it. Rather than touch dozens of scattered write
sites, this is done with SQLite triggers generated per replicated table.

The triggers compute the HLC in pure SQL off the `sync_state` singleton, so
they need no Python. They are suppressed while `sync_state.applying=1` (set by
the protocol's apply path via `applying_guard`) so that merging a peer's
changes does NOT get re-captured as a fresh local write — that guard is the
correctness crux that keeps origin/hlc of a relayed row intact.

Triggers exist only once `PRAGMA user_version >= SYNC_SCHEMA_VERSION`
(i.e. after `tk-sync-migrate`). Pre-migration installs are untouched.
"""
from __future__ import annotations

import contextlib
import sqlite3

from . import SYNC_SCHEMA_VERSION

# Primary-key column per replicated table (the row's global id == its gid).
_PK = {
    "threads": "id", "notes": "id", "verbatim": "id", "evolve": "id",
    "probe_results": "id", "dialectic_evidence": "id",
    "dialectic_observations": "id", "edges": "id", "concepts": "id",
    "distill": "id", "user_dialectic": "id", "probes": "id",
    "core_memory": "key", "style": "key", "skill_usage": "name",
    "reliability": "category", "dialog_messages": "uuid",
}
# Composite-key table: gid is the two key parts joined.
_COMPOSITE = {"distill_votes": ("distill_id", "voter_cid")}

_NOW_MS = "(CAST(strftime('%s','now') AS INTEGER)*1000)"
_ADVANCE = (
    "UPDATE sync_state SET "
    f"hlc_counter = CASE WHEN hlc_phys_ms >= {_NOW_MS} "
    "THEN hlc_counter+1 ELSE 0 END, "
    f"hlc_phys_ms = MAX(hlc_phys_ms, {_NOW_MS}) WHERE id=1;"
)
_HLC = ("(SELECT printf('%015d:%06d:%s',hlc_phys_ms,hlc_counter,node_id) "
        "FROM sync_state WHERE id=1)")
_NODE = "(SELECT node_id FROM sync_state WHERE id=1)"
_NOT_APPLYING = "(SELECT applying FROM sync_state WHERE id=1)=0"


def is_migrated(conn: sqlite3.Connection) -> bool:
    """True once the re-id migration ran (sync feature enabled)."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0]) >= SYNC_SCHEMA_VERSION


def _gid_expr(table: str, ref: str) -> str:
    if table in _COMPOSITE:
        a, b = _COMPOSITE[table]
        return f"{ref}.{a}||':'||{ref}.{b}"
    return f"{ref}.{_PK[table]}"


def _oplog_insert(table: str, ref: str, op: str) -> str:
    return (
        "INSERT INTO sync_oplog(tbl,gid,op,hlc,origin_node) "
        f"SELECT '{table}', {_gid_expr(table, ref)}, '{op}', {_HLC}, {_NODE};"
    )


def install_triggers(conn: sqlite3.Connection) -> None:
    """Create capture triggers for every replicated table. Idempotent
    (CREATE TRIGGER IF NOT EXISTS). Only call on a migrated DB."""
    tables = list(_PK) + list(_COMPOSITE)
    for t in tables:
        # INSERT: only local-origin rows (origin_node still NULL); a remote row
        # applied by the protocol arrives with origin_node already set.
        conn.executescript(
            f"CREATE TRIGGER IF NOT EXISTS {t}__sync_ai AFTER INSERT ON {t} "
            f"WHEN {_NOT_APPLYING} AND NEW.origin_node IS NULL BEGIN "
            f"{_ADVANCE} "
            f"UPDATE {t} SET origin_node={_NODE}, hlc={_HLC} WHERE rowid=NEW.rowid; "
            f"{_oplog_insert(t, 'NEW', 'put')} END;"
        )
        # UPDATE: a local edit is a new write (re-stamp + oplog). Suppressed
        # during apply. Does not recurse: recursive_triggers defaults OFF.
        conn.executescript(
            f"CREATE TRIGGER IF NOT EXISTS {t}__sync_au AFTER UPDATE ON {t} "
            f"WHEN {_NOT_APPLYING} BEGIN "
            f"{_ADVANCE} "
            f"UPDATE {t} SET origin_node={_NODE}, hlc={_HLC} WHERE rowid=NEW.rowid; "
            f"{_oplog_insert(t, 'NEW', 'put')} END;"
        )
        # DELETE: leave a tombstone in the oplog so the delete propagates and a
        # peer that still has the row does not resurrect it.
        conn.executescript(
            f"CREATE TRIGGER IF NOT EXISTS {t}__sync_ad AFTER DELETE ON {t} "
            f"WHEN {_NOT_APPLYING} BEGIN "
            f"{_ADVANCE} "
            f"{_oplog_insert(t, 'OLD', 'del')} END;"
        )
    conn.commit()


@contextlib.contextmanager
def applying_guard(conn: sqlite3.Connection):
    """Within this block, capture triggers are suppressed. Used by the apply
    path so merging a peer's rows keeps their original origin/hlc and does not
    echo back into the oplog."""
    conn.execute("UPDATE sync_state SET applying=1 WHERE id=1")
    try:
        yield
    finally:
        conn.execute("UPDATE sync_state SET applying=0 WHERE id=1")
