"""Change capture for cross-machine sync — active only on a migrated DB.

Every local write to a replicated table must (a) get a global write timestamp
(HLC) + author (node_id) stamped on the row, and (b) leave a trail in
`sync_oplog` so peers can pull it. Rather than touch dozens of scattered write
sites, this is done with SQLite triggers generated per replicated table.

The triggers compute the HLC in pure SQL off the `sync_state` singleton, so
they need no Python. They are suppressed while a per-connection TEMP marker
table exists (created by `applying_guard`) so that merging a peer's changes does
NOT get re-captured as a fresh local write — that guard is the correctness crux
that keeps origin/hlc of a relayed row intact. The marker is connection-local
(a temp table, invisible to other connections and unaffected by their commits),
so a concurrent local write on another connection is always captured, and an
inner commit inside the guarded path cannot expose suppression to anyone else.

Triggers exist only once `sync_state.sync_schema_version >= SYNC_SCHEMA_VERSION`
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
# Suppression is a per-connection TEMP table, probed via pragma_table_list — the
# only temp-catalog probe usable inside a trigger (a trigger may not reference
# the `temp` schema directly, and unqualified sqlite_temp_master binds to main).
# It never errors on connections lacking the table (reads 0) and is invisible to
# other connections regardless of their commits.
_APPLYING_TABLE = "_tk_sync_applying"
_NOT_APPLYING = (
    f"(SELECT count(*) FROM pragma_table_list "
    f"WHERE schema='temp' AND name='{_APPLYING_TABLE}')=0"
)


def is_migrated(conn: sqlite3.Connection) -> bool:
    """True once the re-id migration ran (sync feature enabled).

    Gated on sync_state.sync_schema_version — NOT PRAGMA user_version, which main
    owns as its schema-migration counter. Absent table/row/value = not migrated.
    """
    try:
        row = conn.execute(
            "SELECT sync_schema_version FROM sync_state WHERE id=1"
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row) and row[0] is not None and int(row[0]) >= SYNC_SCHEMA_VERSION


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
        # during apply. `OLD.origin_node IS NOT NULL` skips the AFTER-INSERT
        # trigger's own stamping UPDATE (which flips origin_node NULL->set): that
        # UPDATE fires this trigger too (recursive_triggers OFF only blocks a
        # trigger re-firing ITSELF, not one trigger firing another), so without
        # the guard every insert would be logged twice. This trigger's own
        # re-stamp UPDATE does not recurse (self-firing is blocked).
        conn.executescript(
            f"CREATE TRIGGER IF NOT EXISTS {t}__sync_au AFTER UPDATE ON {t} "
            f"WHEN {_NOT_APPLYING} AND OLD.origin_node IS NOT NULL BEGIN "
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
    """Within this block, capture triggers are suppressed ON THIS CONNECTION.

    Suppression is a per-connection TEMP table, not a shared `sync_state` row:
    other connections never see it and their commits never affect it, so a
    concurrent local write elsewhere is still captured and an inner commit in
    the guarded path (e.g. rebuild_derived's backfills) cannot leak suppression.
    The temp table persists across commits on this connection until dropped."""
    already_guarded = bool(conn.execute(
        "SELECT count(*) FROM pragma_table_list "
        "WHERE schema='temp' AND name=?",
        (_APPLYING_TABLE,),
    ).fetchone()[0])
    if not already_guarded:
        conn.execute(f"CREATE TEMP TABLE {_APPLYING_TABLE} (x)")
    try:
        yield
    finally:
        if not already_guarded:
            conn.execute(f"DROP TABLE IF EXISTS {_APPLYING_TABLE}")
