"""Cross-machine synchronization (active-active P2P replication).

Symmetric anti-entropy: every instance holds a peer list and reconciles the
memory tables with each peer by Hybrid Logical Clock (HLC) — union for
append-only rows, last-writer-wins for mutable ones, tombstones for deletes.
Derived indexes (FTS5, sqlite-vec) are NOT replicated; they are rebuilt
locally from the base tables after a merge.

The feature is dormant until the operator runs the opt-in `tk-sync-migrate`
script (which converts INTEGER autoincrement PKs on the replicated tables to
global TEXT ids and bumps `PRAGMA user_version`) and configures peers. See
docs/sync.md.
"""
from __future__ import annotations

# Schema version the re-id migration bumps `PRAGMA user_version` to. The sync
# daemon and tools stay inert while the DB is below this — so installing the
# feature without migrating leaves an existing user completely unaffected.
SYNC_SCHEMA_VERSION = 1
