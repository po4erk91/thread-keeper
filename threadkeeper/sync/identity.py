"""Node identity + Hybrid Logical Clock for cross-machine sync.

Every TK install gets ONE persistent `node_id` (generated once, stored in the
`sync_state` singleton and mirrored to ~/.threadkeeper/node.id). All sync
ordering uses an HLC — a monotonic, causally-consistent clock that stays
sensible even when two machines' wall clocks drift. An HLC value is a
lexicographically-sortable string `"<phys_ms:015d>:<counter:06d>:<node_id>"`;
comparing two of them gives a total order for last-writer-wins merges.

Nothing here mutates the memory tables — it only reads/writes `sync_state`.
"""
from __future__ import annotations

import logging
import sqlite3
import time

from ..config import DB_PATH
from ..helpers import gen_global_id

logger = logging.getLogger(__name__)

_PHYS_WIDTH = 15   # zero-pad ms so lexical order == numeric order (~year 5138)
_CTR_WIDTH = 6


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ensure_state(conn: sqlite3.Connection) -> None:
    """Insert the singleton row if missing (id=1). First writer wins."""
    conn.execute(
        "INSERT OR IGNORE INTO sync_state (id, node_id) VALUES (1, ?)",
        (gen_global_id("N"),),
    )


def get_node_id(conn: sqlite3.Connection) -> str:
    """Return this install's persistent node_id, creating it on first call.

    Also best-effort mirrors it to ~/.threadkeeper/node.id for external
    tooling / debugging (the DB remains the source of truth)."""
    _ensure_state(conn)
    row = conn.execute("SELECT node_id FROM sync_state WHERE id=1").fetchone()
    node_id = row[0]
    conn.commit()
    try:
        p = DB_PATH.parent / "node.id"
        if not p.exists():
            p.write_text(node_id + "\n")
    except OSError:
        pass
    return node_id


def _fmt(phys: int, ctr: int, node_id: str) -> str:
    return f"{phys:0{_PHYS_WIDTH}d}:{ctr:0{_CTR_WIDTH}d}:{node_id}"


def parse_hlc(hlc: str) -> tuple[int, int, str]:
    """Split an HLC string into (phys_ms, counter, node_id). Tolerant of a
    missing node_id segment. Malformed input sorts as (0, 0, '')."""
    try:
        phys_s, ctr_s, node = hlc.split(":", 2)
        return int(phys_s), int(ctr_s), node
    except (ValueError, AttributeError):
        return 0, 0, ""


def hlc_now(conn: sqlite3.Connection) -> str:
    """Advance and persist the local HLC, returning the new value.

    Standard HLC send rule: phys = max(last_phys, wall_now); counter bumps
    only when phys stalls (same ms), else resets to 0."""
    _ensure_state(conn)
    row = conn.execute(
        "SELECT node_id, hlc_phys_ms, hlc_counter FROM sync_state WHERE id=1"
    ).fetchone()
    node_id, last_phys, last_ctr = row[0], int(row[1]), int(row[2])
    now = _now_ms()
    if now > last_phys:
        phys, ctr = now, 0
    else:
        phys, ctr = last_phys, last_ctr + 1
    conn.execute(
        "UPDATE sync_state SET hlc_phys_ms=?, hlc_counter=? WHERE id=1",
        (phys, ctr),
    )
    conn.commit()
    return _fmt(phys, ctr, node_id)


def hlc_update(conn: sqlite3.Connection, remote_hlc: str) -> str:
    """Merge a received remote HLC into the local clock (standard HLC receive
    rule) and return the new local HLC. Keeps causality across machines."""
    _ensure_state(conn)
    row = conn.execute(
        "SELECT node_id, hlc_phys_ms, hlc_counter FROM sync_state WHERE id=1"
    ).fetchone()
    node_id, last_phys, last_ctr = row[0], int(row[1]), int(row[2])
    r_phys, r_ctr, _ = parse_hlc(remote_hlc)
    now = _now_ms()
    phys = max(last_phys, r_phys, now)
    if phys == last_phys and phys == r_phys:
        ctr = max(last_ctr, r_ctr) + 1
    elif phys == last_phys:
        ctr = last_ctr + 1
    elif phys == r_phys:
        ctr = r_ctr + 1
    else:
        ctr = 0
    conn.execute(
        "UPDATE sync_state SET hlc_phys_ms=?, hlc_counter=? WHERE id=1",
        (phys, ctr),
    )
    conn.commit()
    return _fmt(phys, ctr, node_id)
