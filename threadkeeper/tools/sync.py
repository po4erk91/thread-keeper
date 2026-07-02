"""MCP tools for cross-machine sync: inspect status and trigger a reconcile.

All read-only or manual; the automatic path is sync/daemon.py. Dormant until
the DB is migrated (`tk-sync-migrate`) and peers/token are configured.
"""
from __future__ import annotations

from .._mcp import read_tool, write_tool
from ..config import SYNC_LISTEN
from ..db import get_db
from ..sync import daemon, protocol
from ..sync import identity as sync_identity
from ..sync.capture import is_migrated


@read_tool()
def sync_status() -> str:
    """Cross-machine sync status: migrated?, this node id, peer count, listen
    address, oplog size, and how many origin nodes this DB has seen."""
    conn = get_db()
    try:
        mig = is_migrated(conn)
        node = sync_identity.get_node_id(conn) if mig else "-"
        vv = protocol.version_vector(conn) if mig else {}
        oplog = (conn.execute("SELECT COUNT(*) FROM sync_oplog").fetchone()[0]
                 if mig else 0)
    finally:
        conn.close()
    peers = daemon.peers()
    return (f"migrated={mig} node={node} peers={len(peers)} "
            f"listen={SYNC_LISTEN or '-'} oplog={oplog} origins={len(vv)}")


@read_tool()
def sync_peers() -> str:
    """List the configured sync peers (THREADKEEPER_SYNC_PEERS)."""
    ps = daemon.peers()
    return ", ".join(ps) if ps else "(none configured)"


@write_tool(idempotent=True)
def sync_now() -> str:
    """Reconcile with every configured peer right now (bidirectional). Returns
    per-peer (pulled, pushed) counts or an error marker."""
    res = daemon.sync_all()
    if not res:
        return "no peers configured (set THREADKEEPER_SYNC_PEERS)"
    return " ".join(f"{p}={v}" for p, v in res.items())
