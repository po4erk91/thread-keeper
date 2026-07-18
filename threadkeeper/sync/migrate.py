"""Opt-in re-id migration: give every replicated row a globally-unique TEXT id.

This is the ONE destructive step of cross-machine sync. It converts the
INTEGER-AUTOINCREMENT primary keys on the replicated memory tables to global
ULID TEXT ids (and widens the short 3-hex TEXT ids), rewriting every foreign
reference in lockstep, then rebuilds derived indexes and stamps a baseline
HLC. After it runs, `sync_state.sync_schema_version` is set so the sync
daemon/tools activate; before it runs they stay dormant.

NEVER auto-run. The operator invokes `tk-sync-migrate` (or
`python -m threadkeeper.sync.migrate`) explicitly, after a backup. `--dry-run`
is the default; `--apply` performs the change. Idempotent: a second run on an
already-migrated DB is a no-op.

Design + rationale: docs/sync.md.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

from ..config import DB_PATH
from ..helpers import gen_global_id
from . import SYNC_SCHEMA_VERSION
from . import identity as sync_identity

# Replicated tables whose ids are machine-minted → must become global.
# value = human-readable type prefix carried in front of the ULID.
REID_PREFIX = {
    "threads": "T",
    "notes": "",
    "verbatim": "",
    "evolve": "",
    "probe_results": "",
    "dialectic_evidence": "",
    "dialectic_observations": "",
    "edges": "",
    "concepts": "C",
    "distill": "D",
    "user_dialectic": "UC",
    "probes": "P",
}
# Of those, the ones on INTEGER PRIMARY KEY AUTOINCREMENT need a table-rebuild
# (SQLite cannot ALTER a rowid-alias column to TEXT); the rest are TEXT already
# and just need an UPDATE of the id value.
INT_PK_TABLES = {
    "notes", "verbatim", "evolve", "probe_results",
    "dialectic_evidence", "dialectic_observations", "edges",
}
# edges is a typed polymorphic graph (from_kind/from_id, to_kind/to_id) with no
# declared FK; these kinds address a re-id'd table and must be remapped too.
EDGE_KIND_TABLE = {
    "thread": "threads", "note": "notes",
    "concept": "concepts", "distill": "distill",
}


def _sync_version(conn: sqlite3.Connection) -> int:
    """Sync migration gate, read from sync_state — NOT PRAGMA user_version, which
    main owns as its own schema-migration counter (CURRENT_SCHEMA_VERSION).
    0 = not migrated (absent table/row/value)."""
    try:
        row = conn.execute(
            "SELECT sync_schema_version FROM sync_state WHERE id=1"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def _declared_fk_children(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    """parent_table -> [(child_table, child_col), ...] from PRAGMA foreign_key_list."""
    out: dict[str, list[tuple[str, str]]] = {t: [] for t in REID_PREFIX}
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    ]
    for child in tables:
        for fk in conn.execute(f"PRAGMA foreign_key_list({child})"):
            parent, from_col, to_col = fk[2], fk[3], fk[4]
            if parent in out:
                out[parent].append((child, from_col))
    return out


def _build_maps(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """table -> {old_id(str): new_global_id}."""
    maps: dict[str, dict[str, str]] = {}
    for table, prefix in REID_PREFIX.items():
        rows = conn.execute(f"SELECT id FROM {table}").fetchall()
        maps[table] = {str(r[0]): gen_global_id(prefix) for r in rows}
    return maps


def plan(conn: sqlite3.Connection) -> dict:
    """Read-only summary of what --apply would do."""
    maps = _build_maps(conn)
    children = _declared_fk_children(conn)
    return {
        "sync_schema_version": _sync_version(conn),
        "target_version": SYNC_SCHEMA_VERSION,
        "rows_per_table": {t: len(m) for t, m in maps.items()},
        "fk_children": {t: children[t] for t in REID_PREFIX if children[t]},
        "edge_kinds_remapped": sorted(EDGE_KIND_TABLE),
    }


def _rebuild_int_table(conn: sqlite3.Connection, table: str) -> None:
    """Recreate an INTEGER-PK table with a TEXT PK, copying all rows verbatim
    (ids land as their text form; remapping happens afterwards).

    DROP TABLE also drops the table's indexes and triggers, so its real
    (non-auto) indexes are captured first and recreated verbatim afterwards —
    their columns are unchanged, so the stored DDL still applies. Triggers are
    NOT auto-restored here: the only ones on a rebuilt table are notes_fts's,
    which must be re-keyed onto the integer rowid (see _rebuild_notes_fts)."""
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]
    index_sqls = [
        r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND sql IS NOT NULL", (table,)
        ).fetchall()
    ]
    # Only the rowid-alias PK carries AUTOINCREMENT, so this targets the id col.
    # The DEFAULT lets post-migration INSERTs that omit id still get a global
    # id (128-bit random hex, collision-safe across machines) with zero
    # write-site churn; existing rows are remapped to ULIDs just below.
    new_sql = re.sub(
        r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
        "TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16))))",
        sql,
        count=1,
        flags=re.IGNORECASE,
    )
    new_sql = new_sql.replace(f"TABLE IF NOT EXISTS {table}", f"TABLE {table}__new", 1)
    if "__new" not in new_sql:  # schema stored without IF NOT EXISTS
        new_sql = re.sub(rf"\bTABLE\s+{table}\b", f"TABLE {table}__new", new_sql, count=1)
    conn.execute(new_sql)
    conn.execute(f"INSERT INTO {table}__new SELECT * FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}__new RENAME TO {table}")
    for isql in index_sqls:
        conn.execute(isql)


def _rebuild_notes_fts(conn: sqlite3.Connection) -> None:
    """Re-key notes FTS onto the LOCAL integer rowid.

    `notes.id` becomes a TEXT ULID after the rebuild, but an FTS5 external-
    content `content_rowid` must be an integer. The old notes_fts (keyed on
    `id`) and its INSERT/DELETE triggers (dropped with the rebuilt table) are
    recreated keyed on `notes.rowid` — the same shape main uses for dialog_fts.
    Content is repopulated by the later 'rebuild' in _rebuild_derived. Uses
    single-statement execs (not executescript, which would commit the
    surrounding migration transaction)."""
    conn.execute("DROP TABLE IF EXISTS notes_fts")
    conn.execute(
        "CREATE VIRTUAL TABLE notes_fts USING fts5("
        "content, content='notes', content_rowid='rowid')"
    )
    conn.execute("DROP TRIGGER IF EXISTS notes_fts_ai")
    conn.execute("DROP TRIGGER IF EXISTS notes_fts_ad")
    conn.execute(
        "CREATE TRIGGER notes_fts_ai AFTER INSERT ON notes BEGIN "
        "INSERT INTO notes_fts(rowid, content) VALUES (new.rowid, new.content); END"
    )
    conn.execute(
        "CREATE TRIGGER notes_fts_ad AFTER DELETE ON notes BEGIN "
        "INSERT INTO notes_fts(notes_fts, rowid, content) "
        "VALUES('delete', old.rowid, old.content); END"
    )


def _remap_ids(conn: sqlite3.Connection, table: str, id_map: dict[str, str]) -> None:
    conn.executemany(
        f"UPDATE {table} SET id=? WHERE id=?",
        [(new, old) for old, new in id_map.items()],
    )


def _fix_refs(conn: sqlite3.Connection, maps: dict[str, dict[str, str]],
              children: dict[str, list[tuple[str, str]]]) -> None:
    # Declared foreign keys (parent_id, thread_id, source_thread, distill_id,
    # claim_id, superseded_by, probe_id, ...).
    for parent, refs in children.items():
        id_map = maps[parent]
        if not id_map:
            continue
        for child_table, child_col in refs:
            conn.executemany(
                f"UPDATE {child_table} SET {child_col}=? WHERE {child_col}=?",
                [(new, old) for old, new in id_map.items()],
            )
    # Polymorphic edges (from_kind/from_id, to_kind/to_id).
    for kind, tbl in EDGE_KIND_TABLE.items():
        id_map = maps[tbl]
        if not id_map:
            continue
        conn.executemany(
            "UPDATE edges SET from_id=? WHERE from_kind=? AND from_id=?",
            [(new, kind, old) for old, new in id_map.items()],
        )
        conn.executemany(
            "UPDATE edges SET to_id=? WHERE to_kind=? AND to_id=?",
            [(new, kind, old) for old, new in id_map.items()],
        )


def _stamp_baseline_hlc(conn: sqlite3.Connection) -> None:
    """Give every replicated row a baseline (hlc, origin_node) so the first
    sync has a well-defined last-writer ordering. One hlc per table is enough
    (capture re-stamps on future writes)."""
    from ..db import _SYNC_REPLICATED_TABLES
    node_id = sync_identity.get_node_id(conn)
    for t in _SYNC_REPLICATED_TABLES:
        hlc = sync_identity.hlc_now(conn)
        try:
            conn.execute(
                f"UPDATE {t} SET hlc=?, origin_node=? "
                f"WHERE hlc IS NULL OR origin_node IS NULL",
                (hlc, node_id),
            )
        except sqlite3.OperationalError:
            pass


def _rebuild_derived(conn: sqlite3.Connection) -> None:
    """Derived indexes are keyed off the (now-changed) ids — rebuild locally.
    Runs on a migrated get_db() connection, so notes_vec is already the
    rowid+map schema (get_db self-heals the legacy id-keyed table)."""
    try:
        conn.execute("INSERT INTO notes_fts(notes_fts) VALUES('rebuild')")
    except sqlite3.OperationalError:
        pass
    # Repopulate notes_vec/dialog_vec from the stored embedding BLOBs via the
    # new maps (idempotent; also runs on every background tick).
    try:
        from ..ingest import _backfill_vec_tables
        while _backfill_vec_tables(conn)[0]:
            pass
    except Exception:
        pass


def apply(db_path: Path, do_apply: bool) -> int:
    """Run the migration. Returns process exit code."""
    if not db_path.exists():
        print(f"error: DB not found: {db_path}", file=sys.stderr)
        return 2

    probe = sqlite3.connect(str(db_path))
    try:
        uv = _sync_version(probe)
        summary = plan(probe)
    finally:
        probe.close()

    if uv >= SYNC_SCHEMA_VERSION:
        print(f"already migrated (sync_schema_version={uv} >= {SYNC_SCHEMA_VERSION}); no-op.")
        return 0

    print("re-id migration plan:")
    for t, n in summary["rows_per_table"].items():
        print(f"  {t:<24} {n} rows")
    print(f"  edge kinds remapped: {', '.join(summary['edge_kinds_remapped'])}")
    print(f"  fk children: {sum(len(v) for v in summary['fk_children'].values())} ref columns")

    if not do_apply:
        print("\n--dry-run: no changes written. Re-run with --apply to migrate.")
        return 0

    backup = db_path.with_name(db_path.name + f".bak-{int(time.time())}.sqlite")
    # Consistent single-file snapshot BEFORE any mutation. VACUUM INTO reads the
    # live DB *through a connection*, so it captures committed pages still resident
    # in the -wal — a plain file copy would miss them and produce a torn/stale
    # backup exactly when it matters most (review of PR #201; same class as #95).
    bk = sqlite3.connect(str(db_path))
    try:
        bk.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        bk.execute("VACUUM INTO ?", (str(backup),))
    finally:
        bk.close()
    print(f"\nbackup: {backup}")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        maps = _build_maps(conn)
        children = _declared_fk_children(conn)
        conn.execute("BEGIN IMMEDIATE")
        for table in INT_PK_TABLES:
            _rebuild_int_table(conn, table)
        for table in REID_PREFIX:
            _remap_ids(conn, table, maps[table])
        _fix_refs(conn, maps, children)
        _rebuild_notes_fts(conn)
        conn.execute("COMMIT")
        # Enable the feature BEFORE reopening via get_db so that connection
        # materializes the migrated schema (rowid notes_vec + map + triggers).
        # The gate lives in sync_state — main owns PRAGMA user_version. Ensure the
        # singleton row exists (get_node_id → _ensure_state) before stamping it.
        sync_identity.get_node_id(conn)
        conn.execute(
            "UPDATE sync_state SET sync_schema_version=? WHERE id=1",
            (SYNC_SCHEMA_VERSION,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        print(f"error: migration failed, DB rolled back. Restore from {backup} "
              f"if needed.", file=sys.stderr)
        raise
    conn.close()

    # Reopen through get_db (reasserts declared indexes/triggers on the rebuilt
    # tables + installs sync). Rebuild derived indexes and stamp a baseline HLC
    # under applying_guard so capture triggers don't clobber the explicit stamp.
    # force=True: the gate just flipped, so bootstrap must re-run to rebuild the
    # migrated notes_vec keying and install capture triggers (the earlier
    # bootstrap saw an un-migrated DB and installed neither).
    from ..db import get_db, bootstrap_db
    from . import capture
    bootstrap_db(force=True)
    conn = get_db()
    try:
        with capture.applying_guard(conn):
            _rebuild_derived(conn)
            _stamp_baseline_hlc(conn)
        conn.commit()
    finally:
        conn.close()

    print(f"done. sync_schema_version={SYNC_SCHEMA_VERSION}. Sync is now enabled "
          f"once peers + listen are configured.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="tk-sync-migrate",
        description="One-time re-id migration enabling cross-machine sync. "
                    "Back up ~/.threadkeeper/db.sqlite first.",
    )
    ap.add_argument("--apply", action="store_true",
                    help="perform the migration (default is a dry-run)")
    ap.add_argument("--db", default=None, help="path to db.sqlite (default: config)")
    args = ap.parse_args(argv)
    db_path = Path(args.db).expanduser() if args.db else DB_PATH
    return apply(db_path, do_apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
