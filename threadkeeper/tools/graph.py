"""Knowledge-graph MCP tools.

Extracted from server.py. Provides typed edges between entities
(threads, notes, concepts, distillates, tasks, signals, probes)
with link/unlink primitives and a BFS traversal (`neighbors`).
"""

import sqlite3
import time
from typing import Optional

from .._mcp import mcp
from ..db import get_db
from ..helpers import fmt_age
from ..identity import _ensure_session, _detect_self_cid, _emit


EDGE_KINDS = ("thread", "note", "concept", "distill", "task", "signal", "probe")
EDGE_RELATIONS_HINT = (
    "refines", "contradicts", "exemplifies", "depends_on",
    "mentions", "elaborates", "supersedes",
)


def _entity_table(kind: str) -> Optional[str]:
    """Map kind → table name for existence checks."""
    return {
        "thread": "threads", "note": "notes", "concept": "concepts",
        "distill": "distill", "task": "tasks", "signal": "signals",
        "probe": "probes",
    }.get(kind)


def _entity_exists(conn: sqlite3.Connection, kind: str, eid: str) -> bool:
    table = _entity_table(kind)
    if not table:
        return False
    try:
        return conn.execute(
            f"SELECT 1 FROM {table} WHERE id=?", (eid,)
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def _snippet_for(conn: sqlite3.Connection, kind: str, eid: str) -> str:
    """Pull a short content preview for an entity, regardless of kind."""
    table = _entity_table(kind)
    if not table:
        return "?"
    field_map = {
        "thread": "question", "note": "content", "concept": "description",
        "distill": "content", "task": "prompt", "signal": "content",
        "probe": "prompt",
    }
    field = field_map.get(kind, "")
    if not field:
        return "?"
    try:
        row = conn.execute(
            f"SELECT {field} v FROM {table} WHERE id=?", (eid,)
        ).fetchone()
    except sqlite3.OperationalError:
        return "?"
    if not row or not row["v"]:
        return "(empty)"
    text = row["v"][:90].replace("\n", " ")
    if len(row["v"]) > 90:
        text += "…"
    return text


@mcp.tool()
def link(from_kind: str, from_id: str, to_kind: str, to_id: str,
         relation: str, weight: float = 1.0) -> str:
    """Create a typed edge between two entities.

    Kinds: thread, note, concept, distill, task, signal, probe.
    Relations (suggested, free-form ok): refines, contradicts, exemplifies,
    depends_on, mentions, elaborates, supersedes.

    Existing edge with same (from, to, relation) is replaced (re-linking
    means updating weight/timestamp, not duplicating)."""
    if from_kind not in EDGE_KINDS:
        return f"ERR bad_from_kind={from_kind} (use {','.join(EDGE_KINDS)})"
    if to_kind not in EDGE_KINDS:
        return f"ERR bad_to_kind={to_kind}"
    if not relation.strip():
        return "ERR empty_relation"
    if not (-10 <= weight <= 10):
        return f"ERR weight_out_of_range={weight}"
    conn = get_db()
    _ensure_session(conn)
    if not _entity_exists(conn, from_kind, from_id.strip()):
        return f"ERR from_not_found={from_kind}:{from_id}"
    if not _entity_exists(conn, to_kind, to_id.strip()):
        return f"ERR to_not_found={to_kind}:{to_id}"
    existing = conn.execute(
        "SELECT id FROM edges WHERE from_kind=? AND from_id=? AND "
        "to_kind=? AND to_id=? AND relation=?",
        (from_kind, from_id.strip(), to_kind, to_id.strip(), relation.strip()),
    ).fetchone()
    now_t = int(time.time())
    cid = _detect_self_cid()
    if existing:
        conn.execute(
            "UPDATE edges SET weight=?, created_by_cid=?, created_at=? "
            "WHERE id=?",
            (weight, cid, now_t, existing["id"]),
        )
        eid = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO edges (from_kind, from_id, to_kind, to_id, "
            "relation, weight, created_by_cid, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (from_kind, from_id.strip(), to_kind, to_id.strip(),
             relation.strip(), weight, cid, now_t),
        )
        eid = cur.lastrowid
    _emit(conn, "link", target=f"{from_kind}:{from_id}",
          summary=f"-{relation}-> {to_kind}:{to_id}")
    conn.commit()
    return f"ok edge={eid} {from_kind}:{from_id} -{relation}-> {to_kind}:{to_id}"


@mcp.tool()
def unlink(edge_id: int) -> str:
    """Remove an edge by id."""
    conn = get_db()
    _ensure_session(conn)
    cur = conn.execute("DELETE FROM edges WHERE id=?", (int(edge_id),))
    if cur.rowcount == 0:
        return f"ERR edge_not_found={edge_id}"
    _emit(conn, "unlink", target=str(edge_id))
    conn.commit()
    return "ok"


@mcp.tool()
def neighbors(kind: str, id: str, depth: int = 1, max_n: int = 12) -> str:
    """BFS the graph from a starting node up to `depth` hops away.
    Returns each visited node with its kind, id, and a short content snippet
    pulled from its native table. Both directions of edges traversed."""
    if kind not in EDGE_KINDS:
        return f"ERR bad_kind={kind}"
    conn = get_db()
    if not _entity_exists(conn, kind, id.strip()):
        return f"ERR not_found={kind}:{id}"
    depth = max(1, min(int(depth), 4))
    max_n = max(1, min(int(max_n), 50))
    visited: set[tuple[str, str]] = {(kind, id.strip())}
    frontier = [(kind, id.strip(), 0)]
    nodes_out: list[tuple[str, str, int, str]] = []
    while frontier and len(nodes_out) < max_n:
        k, eid, d = frontier.pop(0)
        if d >= depth:
            continue
        rows = conn.execute(
            "SELECT to_kind AS nk, to_id AS nid, relation, weight FROM edges "
            "WHERE from_kind=? AND from_id=? "
            "UNION "
            "SELECT from_kind AS nk, from_id AS nid, relation, weight FROM edges "
            "WHERE to_kind=? AND to_id=?",
            (k, eid, k, eid),
        ).fetchall()
        for r in rows:
            nk, nid = r["nk"], r["nid"]
            key = (nk, nid)
            if key in visited:
                continue
            visited.add(key)
            snippet = _snippet_for(conn, nk, nid)
            nodes_out.append((nk, nid, d + 1, snippet))
            frontier.append((nk, nid, d + 1))
            if len(nodes_out) >= max_n:
                break
    if not nodes_out:
        return f"no_neighbors {kind}:{id} depth={depth}"
    lines = [f"neighbors {kind}:{id} depth={depth} n={len(nodes_out)}"]
    for k, eid, d, sn in nodes_out:
        lines.append(f"  [+{d}] {k}:{eid} — {sn}")
    return "\n".join(lines)
