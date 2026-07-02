"""Opt-in re-id migration: give every replicated row a globally-unique TEXT id.

This is the ONE destructive step of cross-machine sync. It converts the
INTEGER-AUTOINCREMENT primary keys on the replicated memory tables to global
ULID TEXT ids (and widens the short 3-hex TEXT ids), rewriting every foreign
reference in lockstep, then rebuilds derived indexes and stamps a baseline
HLC. After it runs, `PRAGMA user_version` is bumped so the sync daemon/tools
activate; before it runs they stay dormant.

NEVER auto-run. The operator invokes `tk-sync-migrate` (or
`python -m threadkeeper.sync.migrate`) explicitly, after a backup. `--dry-run`
is the default; `--apply` performs the change. Idempotent: a second run on an
already-migrated DB is a no-op.

Design + rationale: docs/sync.md.
"""
from __future__ import annotations

import argparse
import re
import shutil
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


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


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
        "user_version": _user_version(conn),
        "target_version": SYNC_SCHEMA_VERSION,
        "rows_per_table": {t: len(m) for t, m in maps.items()},
        "fk_children": {t: children[t] for t in REID_PREFIX if children[t]},
        "edge_kinds_remapped": sorted(EDGE_KIND_TABLE),
    }


def _rebuild_int_table(conn: sqlite3.Connection, table: str) -> None:
    """Recreate an INTEGER-PK table with a TEXT PK, copying all rows verbatim
    (ids land as their text form; remapping happens afterwards)."""
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]
    # Only the rowid-alias PK carries AUTOINCREMENT, so this targets the id col.
    new_sql = re.sub(
        r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
        "TEXT PRIMARY KEY",
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
    """Derived indexes are keyed off the (now-changed) ids — rebuild locally."""
    try:
        conn.execute("INSERT INTO notes_fts(notes_fts) VALUES('rebuild')")
    except sqlite3.OperationalError:
        pass
    # Drop stale vec rows keyed by the old integer notes.id; the ingest
    # backfill (_backfill_vec_tables) repopulates notes_vec from notes.embedding
    # via the new notes_vec_map on next daemon tick / get_db.
    for stmt in ("DELETE FROM notes_vec", "DELETE FROM notes_vec_map"):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass


def apply(db_path: Path, do_apply: bool) -> int:
    """Run the migration. Returns process exit code."""
    if not db_path.exists():
        print(f"error: DB not found: {db_path}", file=sys.stderr)
        return 2

    probe = sqlite3.connect(str(db_path))
    try:
        uv = _user_version(probe)
        summary = plan(probe)
    finally:
        probe.close()

    if uv >= SYNC_SCHEMA_VERSION:
        print(f"already migrated (user_version={uv} >= {SYNC_SCHEMA_VERSION}); no-op.")
        return 0

    print("re-id migration plan:")
    for t, n in summary["rows_per_table"].items():
        print(f"  {t:<24} {n} rows")
    print(f"  edge kinds remapped: {', '.join(summary['edge_kinds_remapped'])}")
    print(f"  fk children: {sum(len(v) for v in summary['fk_children'].values())} ref columns")

    if not do_apply:
        print("\n--dry-run: no changes written. Re-run with --apply to migrate.")
        return 0

    backup = db_path.with_name(db_path.name + f".bak-{int(time.time())}")
    shutil.copy2(db_path, backup)
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
        conn.execute("COMMIT")
    except Exception:
        conn.rollback()
        conn.close()
        print(f"error: migration failed, DB rolled back. Restore from {backup} "
              f"if needed.", file=sys.stderr)
        raise
    conn.close()

    # Reassert declared indexes/triggers on the rebuilt tables, rebuild derived
    # indexes, stamp baseline HLC, then bump user_version.
    from ..db import get_db
    conn = get_db()
    try:
        _rebuild_derived(conn)
        _stamp_baseline_hlc(conn)
        conn.execute(f"PRAGMA user_version={SYNC_SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()

    print(f"done. user_version={SYNC_SCHEMA_VERSION}. Sync is now enabled once "
          f"peers + listen are configured.")
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
