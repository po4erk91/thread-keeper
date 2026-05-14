"""Distillation MCP tools.

Extracted from server.py. Provides the distillation channel — content
worth carrying forward across sessions: insights, patterns, anti-patterns,
fixes, terminology, concepts. Other sessions can vote on a distillate;
high-vote items are exported to a curated jsonl bucket.
"""

import json as _json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .._mcp import mcp
from ..db import get_db
from ..config import TASK_LOG_DIR
from ..helpers import fmt_age, q, gen_distill_id
from ..identity import _ensure_session, _detect_self_cid, _emit


DISTILL_KINDS = ("insight", "pattern", "anti-pattern", "fix",
                 "terminology", "concept")


@mcp.tool()
def distill(content: str, kind: str = "insight",
            confidence: str = "medium", source_thread: str = "") -> str:
    """Mark content as worth carrying forward (distillation channel).

    `kind` ∈ {insight, pattern, anti-pattern, fix, terminology, concept}.
    `confidence` ∈ {low, medium, high}. `source_thread` optional. Other
    sessions can vote on it via vote_distill; export_distillates emits
    a curated jsonl bucket."""
    if kind not in DISTILL_KINDS:
        return f"ERR bad_kind={kind} (valid: {','.join(DISTILL_KINDS)})"
    if confidence not in ("low", "medium", "high"):
        return f"ERR bad_confidence={confidence}"
    if not content.strip():
        return "ERR empty_content"
    conn = get_db()
    _ensure_session(conn)
    src = source_thread.strip() or None
    if src and not conn.execute(
        "SELECT 1 FROM threads WHERE id=?", (src,)
    ).fetchone():
        return f"ERR source_thread_not_found={src}"
    cid = _detect_self_cid()
    pid = gen_distill_id(conn)
    now_t = int(time.time())
    conn.execute(
        "INSERT INTO distill (id, content, kind, confidence, source_thread, "
        "source_cid, created_at) VALUES (?,?,?,?,?,?,?)",
        (pid, content, kind, confidence, src, cid, now_t),
    )
    # Auto-vote +1 from author (still bounded by uniqueness)
    if cid:
        conn.execute(
            "INSERT INTO distill_votes (distill_id, voter_cid, weight, "
            "voted_at) VALUES (?,?,?,?)",
            (pid, cid, 1.0, now_t),
        )
        conn.execute(
            "UPDATE distill SET vote_sum=1.0, vote_count=1 WHERE id=?",
            (pid,),
        )
    _emit(conn, "distill", target=pid, summary=content[:140])
    conn.commit()
    return f"ok id={pid} kind={kind} conf={confidence} vote=1.0"


@mcp.tool()
def vote_distill(distill_id: str, weight: float) -> str:
    """Vote on a distillate, weight ∈ [-1, +1]. One vote per cid; re-voting
    overwrites your previous vote. Updates aggregate vote_sum/vote_count."""
    try:
        w = float(weight)
    except (TypeError, ValueError):
        return "ERR weight_not_numeric"
    if w < -1 or w > 1:
        return f"ERR weight_out_of_range={w}"
    conn = get_db()
    _ensure_session(conn)
    cid = _detect_self_cid()
    if not cid:
        return "ERR cannot_detect_self_cid"
    did = distill_id.strip()
    if not conn.execute("SELECT 1 FROM distill WHERE id=?", (did,)).fetchone():
        return f"ERR distill_not_found={did}"
    now_t = int(time.time())
    # upsert vote
    conn.execute(
        "INSERT INTO distill_votes (distill_id, voter_cid, weight, voted_at) "
        "VALUES (?,?,?,?) ON CONFLICT(distill_id, voter_cid) DO UPDATE SET "
        "weight=excluded.weight, voted_at=excluded.voted_at",
        (did, cid, w, now_t),
    )
    # recompute aggregates
    agg = conn.execute(
        "SELECT SUM(weight) s, COUNT(*) c FROM distill_votes WHERE distill_id=?",
        (did,),
    ).fetchone()
    conn.execute(
        "UPDATE distill SET vote_sum=?, vote_count=? WHERE id=?",
        (agg["s"] or 0.0, agg["c"] or 0, did),
    )
    _emit(conn, "vote_distill", target=did, summary=f"w={w}")
    conn.commit()
    return f"ok id={did} vote_sum={agg['s']:.2f} count={agg['c']}"


@mcp.tool()
def pending_distillates(min_vote: float = 1.0, k: int = 10) -> str:
    """List distillates with vote_sum >= min_vote, not yet exported."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, kind, confidence, content, vote_sum, vote_count, "
        "source_thread, created_at "
        "FROM distill WHERE vote_sum >= ? AND exported_at IS NULL "
        "ORDER BY vote_sum DESC, created_at DESC LIMIT ?",
        (float(min_vote), max(1, int(k))),
    ).fetchall()
    if not rows:
        return f"no_pending min_vote={min_vote}"
    now_t = int(time.time())
    lines = [f"pending n={len(rows)} min_vote={min_vote}"]
    for r in rows:
        snip = r["content"][:160].replace("\n", " ")
        if len(r["content"]) > 160:
            snip += "…"
        lines.append(
            f"  {r['id']} {r['kind']:<13} conf={r['confidence']} "
            f"votes={r['vote_sum']:.1f}/{r['vote_count']} "
            f"src={r['source_thread'] or '-'} age="
            f"{fmt_age(now_t - r['created_at'])}_ago"
        )
        lines.append(f"    {snip}")
    return "\n".join(lines)


@mcp.tool()
def export_distillates(min_vote: float = 1.0,
                       output_path: str = "") -> str:
    """Write distillates with vote_sum >= min_vote to a jsonl bucket.
    Marks them exported_at so the same item isn't re-exported next call.
    Default output: /tmp/thread-keeper-tasks/distillates.jsonl."""
    out_path = Path(output_path.strip()) if output_path.strip() else (
        TASK_LOG_DIR / "distillates.jsonl"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    rows = conn.execute(
        "SELECT id, kind, confidence, content, vote_sum, vote_count, "
        "source_thread, source_cid, created_at "
        "FROM distill WHERE vote_sum >= ? AND exported_at IS NULL "
        "ORDER BY created_at",
        (float(min_vote),),
    ).fetchall()
    if not rows:
        return f"nothing_to_export min_vote={min_vote}"
    now_t = int(time.time())
    written = 0
    with out_path.open("a", encoding="utf-8") as fp:
        for r in rows:
            obj = {
                "id": r["id"], "kind": r["kind"],
                "confidence": r["confidence"],
                "content": r["content"],
                "vote_sum": r["vote_sum"], "vote_count": r["vote_count"],
                "source_thread": r["source_thread"],
                "source_cid": r["source_cid"],
                "created_at": r["created_at"],
                "exported_at": now_t,
            }
            fp.write(_json.dumps(obj, ensure_ascii=False) + "\n")
            written += 1
    ids = [r["id"] for r in rows]
    conn.execute(
        f"UPDATE distill SET exported_at=? WHERE id IN "
        f"({','.join('?' * len(ids))})",
        (now_t, *ids),
    )
    conn.commit()
    return f"exported n={written} → {out_path} (now total: {out_path.stat().st_size} bytes)"
