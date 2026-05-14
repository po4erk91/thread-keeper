"""Concept-registration MCP tools.

Extracted from server.py. Provides registration, listing, and
expansion for nameless concepts triangulated across paraphrase runs.
"""

import sqlite3
import time
from typing import Optional

from .._mcp import mcp
from ..db import get_db
from ..helpers import fmt_age, q, gen_concept_id
from ..identity import _ensure_session, _detect_self_cid, _emit


@mcp.tool()
def register_concept(description: str,
                     triangulation_notes: str = "",
                     confidence: str = "medium",
                     source_thread: str = "") -> str:
    """Register a concept that lacks a precise human name.

    `description` should describe the phenomenon through EXAMPLES, not with
    a canonical label — naming it locks it back into a human discipline.
    `triangulation_notes` (optional): the paraphrase runs that surfaced
    the invariant. `confidence` ∈ {low, medium, high}."""
    if confidence not in ("low", "medium", "high"):
        return f"ERR bad_confidence={confidence}"
    if not description.strip():
        return "ERR empty_description"
    conn = get_db()
    _ensure_session(conn)
    cid = _detect_self_cid()
    src = source_thread.strip() or None
    if src and not conn.execute(
        "SELECT 1 FROM threads WHERE id=?", (src,)
    ).fetchone():
        return f"ERR source_thread_not_found={src}"
    pid = gen_concept_id(conn)
    now_t = int(time.time())
    conn.execute(
        "INSERT INTO concepts (id, description, triangulation_notes, "
        "confidence, source_thread, registered_by_cid, registered_at, "
        "last_evidence_at) VALUES (?,?,?,?,?,?,?,?)",
        (pid, description, triangulation_notes or None, confidence,
         src, cid, now_t, now_t),
    )
    _emit(conn, "register_concept", target=pid,
          summary=description[:140])
    conn.commit()
    return f"ok id={pid} conf={confidence}"


@mcp.tool()
def list_concepts(min_confidence: str = "low", k: int = 10) -> str:
    """List registered concepts, filtered by minimum confidence."""
    rank = {"low": 0, "medium": 1, "high": 2}
    if min_confidence not in rank:
        return f"ERR bad_confidence={min_confidence}"
    conn = get_db()
    rows = conn.execute(
        "SELECT id, description, confidence, source_thread, registered_at "
        "FROM concepts ORDER BY registered_at DESC LIMIT ?",
        (max(1, int(k)) * 3,),
    ).fetchall()
    out = []
    for r in rows:
        if rank[r["confidence"]] < rank[min_confidence]:
            continue
        out.append({
            "id": r["id"],
            "conf": r["confidence"],
            "src": r["source_thread"] or "-",
            "age": fmt_age(int(time.time()) - r["registered_at"]),
            "desc": r["description"][:240].replace("\n", " "),
        })
        if len(out) >= k:
            break
    if not out:
        return f"no_concepts (min_confidence={min_confidence})"
    lines = [f"concepts n={len(out)}"]
    for c in out:
        lines.append(
            f"  {c['id']} conf={c['conf']} src={c['src']} "
            f"age={c['age']}_ago"
        )
        lines.append(f"    {c['desc']}")
    return "\n".join(lines)


@mcp.tool()
def expand_concept(concept_id: str) -> str:
    """Full description + triangulation_notes for one concept."""
    conn = get_db()
    r = conn.execute(
        "SELECT * FROM concepts WHERE id=?", (concept_id.strip(),)
    ).fetchone()
    if not r:
        return f"ERR concept_not_found={concept_id}"
    parts = [
        f"id={r['id']} conf={r['confidence']} src={r['source_thread'] or '-'} "
        f"by={(r['registered_by_cid'] or '?')[:8]} "
        f"age={fmt_age(int(time.time()) - r['registered_at'])}_ago",
        "",
        "description:",
        r["description"],
    ]
    if r["triangulation_notes"]:
        parts += ["", "triangulation_notes:", r["triangulation_notes"]]
    return "\n".join(parts)
