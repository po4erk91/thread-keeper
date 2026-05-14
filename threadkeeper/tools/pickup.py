"""Self-initiated pickup of stale unresolved threads.

Surfaces idle, unclaimed threads as candidates for autonomous work, lets a
caller claim one (optionally spawning a headless child to advance it), and
releases the claim when done.
"""

import sqlite3
import time
from typing import Optional

from .._mcp import mcp
from ..db import get_db
from ..helpers import fmt_age, q
from .. import identity
from ..identity import _ensure_session, _detect_self_cid, _emit
from ..embeddings import _embed
from .spawn import spawn


@mcp.tool()
def pickup_candidates(min_idle_days: int = 3, max_n: int = 5) -> str:
    """Surface unresolved threads that are stale and unclaimed — candidates
    for self-initiated pickup when context is free.

    Ranks by oldest last_touched_at among active+idle threads with no current
    claim. Adds a one-line summary so caller can decide which to claim."""
    conn = get_db()
    _ensure_session(conn)
    now_t = int(time.time())
    cutoff = now_t - max(0, int(min_idle_days)) * 86400
    rows = conn.execute(
        "SELECT id, question, state, last_touched_at, last_move "
        "FROM threads "
        "WHERE state IN ('active','idle') "
        "AND last_touched_at <= ? AND claimed_at IS NULL "
        "ORDER BY last_touched_at ASC LIMIT ?",
        (cutoff, max(1, int(max_n))),
    ).fetchall()
    if not rows:
        return f"no_candidates (no unclaimed thread idle >= {min_idle_days}d)"
    lines = [f"candidates n={len(rows)} idle>={min_idle_days}d"]
    for t in rows:
        idle = fmt_age(now_t - t["last_touched_at"])
        last_move_short = (t["last_move"] or "(no notes)")[:80]
        lines.append(
            f"  {t['id']} [{t['state']}] q={q(t['question'][:90])} "
            f"idle={idle} last={q(last_move_short)}"
        )
    return "\n".join(lines)


@mcp.tool()
def claim_pickup(thread_id: str, plan: str = "",
                 spawn_role: str = "", auto_spawn: bool = False) -> str:
    """Claim a thread for self-initiated work. Marks it claimed by my cid.

    If `auto_spawn=True`, immediately spawns a headless child with the
    thread context (question + recent notes + plan) for parallel work.
    `spawn_role` defaults to 'executor' when auto_spawn is on."""
    conn = get_db()
    _ensure_session(conn)
    self_cid = _detect_self_cid()
    if not self_cid:
        return "ERR cannot_detect_self_cid"
    tid = thread_id.strip()
    t = conn.execute(
        "SELECT id, question, state, claimed_at, claimed_by_cid "
        "FROM threads WHERE id=?",
        (tid,),
    ).fetchone()
    if not t:
        return f"ERR thread_not_found={tid}"
    if t["claimed_at"] and t["claimed_by_cid"] != self_cid:
        return (
            f"ERR already_claimed by={(t['claimed_by_cid'] or '')[:8]} "
            f"at={fmt_age(int(time.time()) - t['claimed_at'])}_ago"
        )
    now_t = int(time.time())
    conn.execute(
        "UPDATE threads SET claimed_at=?, claimed_by_cid=?, "
        "last_touched_at=? WHERE id=?",
        (now_t, self_cid, now_t, tid),
    )
    if plan:
        emb = _embed(plan)
        conn.execute(
            "INSERT INTO notes (thread_id, content, kind, created_at, "
            "session_id, embedding) VALUES (?,?,?,?,?,?)",
            (tid, f"PICKUP plan: {plan}", "move", now_t, identity._session_id, emb),
        )
    _emit(conn, "claim_pickup", target=tid,
          summary=plan[:140] if plan else (t["question"] or ""))
    conn.commit()

    spawn_info = ""
    if auto_spawn:
        notes = conn.execute(
            "SELECT kind, content FROM notes WHERE thread_id=? "
            "ORDER BY created_at DESC LIMIT 8",
            (tid,),
        ).fetchall()
        notes_block = "\n".join(
            f"  [{n['kind']}] {n['content'][:240]}" for n in notes
        ) or "  (no notes yet)"
        child_prompt = (
            f"Pickup task — make progress on a stale unresolved thread.\n\n"
            f"Thread question: {t['question']}\n\n"
            f"Recent notes:\n{notes_block}\n\n"
            f"Plan from caller: {plan or '(infer one and proceed)'}\n\n"
            f"Make ONE concrete advance. Add a note to the thread "
            f"(mcp__thread-keeper__note with thread_id={tid}, kind='move'). "
            f"When done, broadcast 'pickup-{tid}: <one-line result>'."
        )
        result = spawn(
            prompt=child_prompt,
            visible=False,
            permission_mode="auto",
            role=spawn_role or "executor",
            extra_allowed_tools="Read,Bash,Grep,Glob",
        )
        spawn_info = f" | spawn: {result}"
    return f"ok claimed thread={tid}{spawn_info}"


@mcp.tool()
def release_pickup(thread_id: str) -> str:
    """Release a claim. Only the claimant can release."""
    conn = get_db()
    _ensure_session(conn)
    self_cid = _detect_self_cid()
    if not self_cid:
        return "ERR cannot_detect_self_cid"
    tid = thread_id.strip()
    t = conn.execute(
        "SELECT claimed_by_cid FROM threads WHERE id=?", (tid,),
    ).fetchone()
    if not t or not t["claimed_by_cid"]:
        return "not_claimed"
    if t["claimed_by_cid"] != self_cid:
        return f"ERR not_my_claim by={t['claimed_by_cid'][:8]}"
    conn.execute(
        "UPDATE threads SET claimed_at=NULL, claimed_by_cid=NULL WHERE id=?",
        (tid,),
    )
    _emit(conn, "release_pickup", target=tid)
    conn.commit()
    return f"ok released thread={tid}"
