"""Client side of cross-machine sync: a symmetric daemon that reconciles with
each configured peer on an interval.

Topology is a decentralized P2P mesh — every instance runs BOTH this client
daemon and the server (sync/server.py) and lists its peers in
THREADKEEPER_SYNC_PEERS. Peer lists may be partial/asymmetric: because merges
are transitive (a row carries its origin's HLC), a connected graph converges.
Adding a machine = add its address on some node; no central hub.

Self-healing: an unreachable peer just fails this tick and is retried next
tick. All OFF by default (interval 0, no peers); dormant until a migrated DB
is configured. Follows the same threading-daemon shape as skill_watcher.py.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.request

from ..config import SYNC_INTERVAL_S, SYNC_PEERS, SYNC_TOKEN
from ..db import get_db
from ..helpers import daemon_sleep
from . import protocol
from .capture import is_migrated

logger = logging.getLogger(__name__)
_started = False


def peers() -> list[str]:
    return [p.strip().rstrip("/") for p in SYNC_PEERS.split(",") if p.strip()]


def _post(url: str, obj: dict, timeout: float = 30.0) -> dict:
    headers = {"Content-Type": "application/json"}
    if SYNC_TOKEN:
        headers["Authorization"] = f"Bearer {SYNC_TOKEN}"
    req = urllib.request.Request(
        url, data=protocol.dumps(obj).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (trusted peer)
        return json.loads(r.read().decode() or "{}")


def sync_with_peer(peer: str) -> tuple[int, int]:
    """One bidirectional reconcile with a peer. Returns (pulled, pushed)."""
    conn = get_db()
    try:
        if not is_migrated(conn):
            return (0, 0)
        pull = _post(peer + "/sync/pull", {"vv": protocol.version_vector(conn)})
        pulled = protocol.apply_changes(conn, pull.get("changes", []))
        push = protocol.collect_changes(conn, pull.get("vv", {}))
        resp = _post(peer + "/sync/push", {"changes": push})
        protocol.rebuild_derived(conn)
        return (pulled, int(resp.get("applied", 0)))
    finally:
        conn.close()


def sync_all() -> dict:
    """Reconcile with every configured peer once. Returns per-peer results."""
    out = {}
    for p in peers():
        try:
            out[p] = sync_with_peer(p)
        except Exception as e:  # unreachable peer / transient error → retry next
            out[p] = f"err:{type(e).__name__}"
            logger.debug("sync with %s failed: %s", p, e)
    return out


def _serve_loop() -> None:
    while True:
        try:
            sync_all()
        except Exception:
            logger.debug("sync tick failed", exc_info=True)
        daemon_sleep(SYNC_INTERVAL_S)


def start_sync_daemon() -> None:
    """Start the peer-reconcile loop if configured. Safe to call repeatedly."""
    global _started
    if _started:
        return
    if SYNC_INTERVAL_S <= 0 or not peers():
        return
    from ..config import BACKGROUND_DAEMONS_ALLOWED
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(target=_serve_loop, name="sync_daemon", daemon=True)
    t.start()
    _started = True
