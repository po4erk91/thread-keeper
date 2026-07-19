"""tk-sync-reset-node: give a CLONED thread-keeper DB a fresh sync identity.

The sync `node_id` lives inside the DB (`sync_state`), so copying a migrated
`db.sqlite` (or the whole `~/.threadkeeper` state dir) onto a second machine
gives both installs the SAME identity. The version vector is `MAX(hlc)` per
`origin_node`, so two independent HLC streams under one origin are
indistinguishable: once a peer sees the higher stream it assumes every lower
timestamp for that origin is already known, and a valid change from the other
machine can be dropped forever. This is data loss, not an LWW collision.

When to reset — the distinction matters:

  * Restoring a backup as a REPLACEMENT for the same writer (the old machine is
    gone) may keep the identity: there is still only one live writer for it.
  * Creating a SECOND, simultaneously-writable replica from a DB/state-dir copy
    MUST reset the clone's identity BEFORE it accepts any local write or sync,
    so the two machines have distinct origins.

The reset keeps the DB's existing HLC high-water mark and changes only the
writer id, so a post-reset local write still dominates anything already
observed (including a future-skewed remote HLC). Historical rows keep their
`origin_node` — that history already lives in the copied DB.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from ..config import DB_PATH
from ..helpers import gen_global_id
from . import identity as sync_identity
from .capture import is_migrated


def _high_water_hlc(conn: sqlite3.Connection) -> str:
    """Highest HLC this DB has observed anywhere — the per-origin version vector
    plus the local clock — as an HLC string ('' if none). Absorbing this before
    changing identity guarantees the next local write out-ranks it."""
    from .protocol import version_vector

    hlcs = [h for h in version_vector(conn).values() if h]
    row = conn.execute(
        "SELECT hlc_phys_ms, hlc_counter, node_id FROM sync_state WHERE id=1"
    ).fetchone()
    if row:
        hlcs.append(sync_identity._fmt(int(row[0]), int(row[1]), row[2]))
    return max(hlcs) if hlcs else ""


def _write_node_id_mirror(db_path: Path, node_id: str) -> None:
    """Atomically overwrite the ~/.threadkeeper/node.id mirror (write-temp +
    os.replace). Unlike get_node_id's write-if-missing, a reset MUST replace a
    stale mirror left from the copied identity."""
    try:
        p = db_path.parent / "node.id"
        tmp = p.with_name("node.id.tmp")
        tmp.write_text(node_id + "\n")
        os.replace(tmp, p)
    except OSError:
        pass


def reset_node(db_path: Path, do_apply: bool) -> int:
    """Assign a fresh node_id to the DB at db_path. Returns a process exit code."""
    if not db_path.exists():
        print(f"error: DB not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    # Autocommit: the setup reads/_ensure_state must not leave an implicit
    # transaction open, so the explicit BEGIN IMMEDIATE below is the only one.
    conn.isolation_level = None
    try:
        # The additive core schema creates sync_state before the opt-in re-id
        # migration, so table existence is not a sufficient gate. Check the
        # actual sync_schema_version before any helper that could initialize the
        # singleton; in particular, a dry-run must remain strictly read-only.
        if not is_migrated(conn):
            print("error: DB is not sync-migrated; run tk-sync-migrate first.",
                  file=sys.stderr)
            return 2
        old = conn.execute(
            "SELECT node_id FROM sync_state WHERE id=1"
        ).fetchone()[0]
        new = gen_global_id("N")
        hw = _high_water_hlc(conn)

        print(f"current node_id: {old}")
        print(f"new node_id:     {new}")
        print(f"HLC high-water preserved: {hw or '(none)'}")
        if not do_apply:
            print("\n--dry-run: no changes written. Re-run with --apply to reset.")
            return 0

        # One exclusive transaction: absorb the high-water into the local clock,
        # switch the writer id, and drop any seen-cursor state scoped to the old
        # identity. Historical rows are left untouched.
        conn.execute("BEGIN IMMEDIATE")
        try:
            if hw:
                sync_identity.hlc_absorb(conn, hw)  # non-committing variant
            conn.execute(
                "UPDATE sync_state SET node_id=? WHERE id=1", (new,)
            )
            conn.execute("DELETE FROM sync_peer_vv")
            conn.execute("COMMIT")
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    _write_node_id_mirror(db_path, new)
    print(f"\ndone. node_id reset {old} -> {new}. Historical row origins "
          f"unchanged. Sync/local writes may resume with the new identity.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="tk-sync-reset-node",
        description="Assign a fresh sync identity to a CLONED thread-keeper DB. "
                    "Run this on the COPY before it accepts local writes or sync; "
                    "a plain backup-restore for the same writer does not need it.",
    )
    ap.add_argument("--apply", action="store_true",
                    help="perform the reset (default is a dry-run)")
    ap.add_argument("--db", default=None, help="path to db.sqlite (default: config)")
    args = ap.parse_args(argv)
    db_path = Path(args.db).expanduser() if args.db else DB_PATH
    return reset_node(db_path, do_apply=args.apply)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
