"""Dialectic user model — discrete claims about the user backed by
evidence that accumulates over time. Inspired by Honcho's Theory of Mind.

Confidence emerges from evidence rather than being asserted. We use a
smoothed ratio so that magnitude matters (3 supports < 5 supports), not
just sign:

  ratio = (support_count - contradict_count) / (support_count + contradict_count + 3)

    < -0.2  → 'disputed'  (more contradictions than supports by margin)
    < 0.2   → 'low'
    < 0.6   → 'medium'
    else    → 'high'

A claim with no evidence stays 'low' regardless of age. The smoothing
constant (3) means a single piece of evidence does not jump to 'high':
3 supports lands at medium (3/6=0.5), 5 supports at high (5/8=0.625).
Heavy contradiction can still drag a claim back: 1 support + 3 contradicts
→ -2/7 → disputed.

Tools:
  dialectic_claim       — register a new claim with optional initial evidence
  dialectic_evidence    — attach evidence (support/contradict) to existing claim
  dialectic_review      — list claims by confidence/domain/state
  dialectic_synthesis   — terse 'who is this user' rendering for brief()
  dialectic_supersede   — retire claim A in favor of claim B (claim_B refines A)

Confidence is recomputed on every evidence add. domain is free-text but a
small enumeration is recommended:
  'style','workflow','values','context','skills','other'.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Optional

from .._mcp import mcp
from ..db import get_db
from ..helpers import fmt_age, q, gen_dialectic_id
from ..identity import _ensure_session, _detect_self_cid, _emit


VALID_KINDS = ("support", "contradict")
VALID_CONFIDENCE = ("low", "medium", "high", "disputed")
VALID_STATE = ("active", "retired", "superseded")
SUGGESTED_DOMAINS = (
    "style", "workflow", "values", "context", "skills", "other",
)
SMOOTHING = 3  # see module docstring for rationale


def _recompute_confidence(conn: sqlite3.Connection, claim_id: str) -> str:
    """Recalculate confidence from current support/contradict counts and
    persist. Returns the new label."""
    row = conn.execute(
        "SELECT support_count, contradict_count FROM user_dialectic WHERE id=?",
        (claim_id,),
    ).fetchone()
    if not row:
        return "low"
    s, c = row["support_count"], row["contradict_count"]
    total = s + c
    if total == 0:
        new_conf = "low"
    else:
        ratio = (s - c) / (total + SMOOTHING)
        if ratio < -0.2:
            new_conf = "disputed"
        elif ratio < 0.2:
            new_conf = "low"
        elif ratio < 0.6:
            new_conf = "medium"
        else:
            new_conf = "high"
    conn.execute(
        "UPDATE user_dialectic SET confidence=? WHERE id=?",
        (new_conf, claim_id),
    )
    return new_conf


def _insert_evidence(conn: sqlite3.Connection, claim_id: str, kind: str,
                     quote: str, source: str, weight: float,
                     cid: Optional[str], now_t: int) -> int:
    """Write evidence row and bump the claim's counter. Caller commits."""
    cur = conn.execute(
        "INSERT INTO dialectic_evidence (claim_id, kind, source, quote, "
        "weight, created_by_cid, created_at) VALUES (?,?,?,?,?,?,?)",
        (claim_id, kind, source or None, quote or None, float(weight),
         cid, now_t),
    )
    col = "support_count" if kind == "support" else "contradict_count"
    conn.execute(
        f"UPDATE user_dialectic SET {col}={col}+1, last_evidence_at=? "
        "WHERE id=?",
        (now_t, claim_id),
    )
    return cur.lastrowid


@mcp.tool()
def dialectic_claim(claim: str, domain: str = "", evidence: str = "",
                    evidence_kind: str = "support") -> str:
    """Register a new claim about the user. Optionally seed with first
    piece of evidence — pass the supporting (or contradicting) quote in
    `evidence` and set `evidence_kind` to 'support' (default) or
    'contradict'.

    `domain` is free-text; recommended values:
      'style','workflow','values','context','skills','other'.

    Returns: 'ok id=<UCxxx> conf=<level>'."""
    claim = claim.strip()
    if not claim:
        return "ERR empty_claim"
    if len(claim) > 1000:
        return "ERR claim_too_long max=1000"
    dom = domain.strip() or None
    if dom and len(dom) > 64:
        return "ERR domain_too_long max=64"
    if evidence_kind not in VALID_KINDS:
        return f"ERR bad_kind={evidence_kind} valid={'/'.join(VALID_KINDS)}"
    conn = get_db()
    _ensure_session(conn)
    cid = _detect_self_cid()
    cid_short = cid
    cid_db = cid
    pid = gen_dialectic_id(conn)
    now_t = int(time.time())
    conn.execute(
        "INSERT INTO user_dialectic (id, claim, domain, created_by_cid, "
        "created_at) VALUES (?,?,?,?,?)",
        (pid, claim, dom, cid_db, now_t),
    )
    seed_quote = evidence.strip()
    if seed_quote:
        _insert_evidence(conn, pid, evidence_kind, seed_quote,
                         "manual", 1.0, cid_short, now_t)
    new_conf = _recompute_confidence(conn, pid)
    _emit(conn, "dialectic_claim", target=pid, summary=claim[:140])
    conn.commit()
    return f"ok id={pid} conf={new_conf}"


@mcp.tool()
def dialectic_evidence(claim_id: str, kind: str = "support",
                       quote: str = "", source: str = "",
                       weight: float = 1.0) -> str:
    """Attach evidence to a claim. `kind`: 'support' | 'contradict'.

    `source` is a freeform pointer like 'thread:T7f3', 'verbatim:42',
    'dialog:<uuid>', or 'manual'. `weight` ∈ [0,1] (default 1.0) is
    captured for future reweighting; counts increment by 1 regardless.

    Bumps support_count or contradict_count and recomputes confidence."""
    claim_id = claim_id.strip()
    if kind not in VALID_KINDS:
        return f"ERR bad_kind={kind} valid={'/'.join(VALID_KINDS)}"
    try:
        w = float(weight)
    except (TypeError, ValueError):
        return f"ERR bad_weight={weight}"
    if w < 0.0 or w > 1.0:
        return f"ERR weight_out_of_range value={w} valid=0..1"
    conn = get_db()
    _ensure_session(conn)
    row = conn.execute(
        "SELECT state FROM user_dialectic WHERE id=?", (claim_id,)
    ).fetchone()
    if not row:
        return f"ERR claim_not_found={claim_id}"
    if row["state"] != "active":
        return f"ERR claim_not_active state={row['state']} id={claim_id}"
    cid = _detect_self_cid()
    now_t = int(time.time())
    eid = _insert_evidence(conn, claim_id, kind, quote.strip(),
                           source.strip(), w, cid, now_t)
    new_conf = _recompute_confidence(conn, claim_id)
    _emit(conn, f"dialectic_evidence:{kind}", target=claim_id,
          summary=(quote or "")[:140])
    conn.commit()
    return f"ok evidence_id={eid} claim={claim_id} conf={new_conf}"


@mcp.tool()
def dialectic_review(min_confidence: str = "low",
                     domain: str = "",
                     k: int = 20) -> str:
    """List active claims filtered by confidence floor and optional
    domain. Retired/superseded claims are omitted.

    `min_confidence`: one of 'low','medium','high','disputed'. Note that
    'disputed' is treated as its own bucket (not ordered against the
    others) — passing `min_confidence='disputed'` returns only disputed.

    Format: '<id> [conf] domain=<d> support=N contradict=N <claim>'."""
    rank = {"low": 0, "medium": 1, "high": 2}
    if min_confidence not in VALID_CONFIDENCE:
        return f"ERR bad_confidence={min_confidence}"
    try:
        klim = max(1, int(k))
    except (TypeError, ValueError):
        return f"ERR bad_k={k}"
    conn = get_db()
    sql = (
        "SELECT id, claim, domain, support_count, contradict_count, "
        "confidence, last_evidence_at, created_at FROM user_dialectic "
        "WHERE state='active'"
    )
    params: list = []
    dom_filter = domain.strip()
    if dom_filter:
        sql += " AND domain=?"
        params.append(dom_filter)
    sql += " ORDER BY last_evidence_at DESC, created_at DESC"
    rows = conn.execute(sql, tuple(params)).fetchall()
    out: list[str] = []
    for r in rows:
        conf = r["confidence"]
        if min_confidence == "disputed":
            if conf != "disputed":
                continue
        else:
            if conf == "disputed":
                # disputed is not above low/medium/high in normal ranks —
                # surface it only when the caller explicitly asked.
                continue
            if rank.get(conf, 0) < rank[min_confidence]:
                continue
        dom_str = r["domain"] or "-"
        out.append(
            f"{r['id']} [{conf}] domain={dom_str} "
            f"support={r['support_count']} contradict={r['contradict_count']} "
            f"{r['claim']}"
        )
        if len(out) >= klim:
            break
    if not out:
        return (
            f"no_claims (min_confidence={min_confidence}"
            + (f" domain={dom_filter}" if dom_filter else "")
            + ")"
        )
    return "\n".join([f"claims n={len(out)}"] + out)


@mcp.tool()
def dialectic_synthesis(domain: str = "") -> str:
    """Terse rendering of accumulated beliefs about the user, grouped by
    domain. Used as brief() input. Excludes low/disputed claims and
    non-active states. Returns at most 12 lines.

    If `domain` is provided, restricts to that domain (no group headers
    in that case)."""
    conn = get_db()
    sql = (
        "SELECT id, claim, domain, confidence, support_count, "
        "contradict_count FROM user_dialectic "
        "WHERE state='active' AND confidence IN ('medium','high')"
    )
    params: list = []
    dom_filter = domain.strip()
    if dom_filter:
        sql += " AND domain=?"
        params.append(dom_filter)
    # high before medium; within each, more evidence first
    sql += (
        " ORDER BY "
        "  CASE confidence WHEN 'high' THEN 0 ELSE 1 END, "
        "  (support_count - contradict_count) DESC, "
        "  domain ASC"
    )
    rows = conn.execute(sql, tuple(params)).fetchall()
    if not rows:
        return "no_synthesis"
    # Group by domain, render with at most 12 total output lines (including
    # group headers when no single-domain filter is active).
    lines: list[str] = []
    if dom_filter:
        for r in rows:
            if len(lines) >= 12:
                break
            tag = "★" if r["confidence"] == "high" else "·"
            lines.append(f"  {tag} {r['claim']}")
    else:
        grouped: dict[str, list[sqlite3.Row]] = {}
        order: list[str] = []
        for r in rows:
            key = r["domain"] or "other"
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(r)
        for dom in order:
            if len(lines) >= 12:
                break
            lines.append(f"[{dom}]")
            for r in grouped[dom]:
                if len(lines) >= 12:
                    break
                tag = "★" if r["confidence"] == "high" else "·"
                lines.append(f"  {tag} {r['claim']}")
    return "\n".join(lines)


@mcp.tool()
def dialectic_supersede(old_claim_id: str, new_claim: str,
                        domain: str = "", quote: str = "") -> str:
    """Retire `old_claim_id` and register `new_claim` that refines or
    replaces it. The old claim moves to state='superseded' with
    superseded_by=<new_id>; its evidence is preserved (not deleted).

    If `quote` is provided, it seeds the new claim with one supporting
    piece of evidence sourced as 'supersede:<old_id>'.

    If `domain` is empty, the new claim inherits the old claim's domain.

    Returns: 'ok new=<UCxxx> old=<UCxxx> conf=<level>'."""
    old_id = old_claim_id.strip()
    new_claim = new_claim.strip()
    if not new_claim:
        return "ERR empty_new_claim"
    if len(new_claim) > 1000:
        return "ERR new_claim_too_long max=1000"
    conn = get_db()
    _ensure_session(conn)
    old = conn.execute(
        "SELECT id, domain, state FROM user_dialectic WHERE id=?", (old_id,)
    ).fetchone()
    if not old:
        return f"ERR old_claim_not_found={old_id}"
    if old["state"] != "active":
        return f"ERR old_claim_not_active state={old['state']} id={old_id}"
    dom = domain.strip() or old["domain"] or None
    if dom and len(dom) > 64:
        return "ERR domain_too_long max=64"
    cid = _detect_self_cid()
    pid = gen_dialectic_id(conn)
    now_t = int(time.time())
    conn.execute(
        "INSERT INTO user_dialectic (id, claim, domain, created_by_cid, "
        "created_at) VALUES (?,?,?,?,?)",
        (pid, new_claim, dom, cid, now_t),
    )
    seed_quote = quote.strip()
    if seed_quote:
        _insert_evidence(conn, pid, "support", seed_quote,
                         f"supersede:{old_id}", 1.0, cid, now_t)
    new_conf = _recompute_confidence(conn, pid)
    conn.execute(
        "UPDATE user_dialectic SET state='superseded', superseded_by=? "
        "WHERE id=?",
        (pid, old_id),
    )
    _emit(conn, "dialectic_supersede", target=pid,
          summary=f"{old_id}→{pid} {new_claim[:100]}")
    conn.commit()
    return f"ok new={pid} old={old_id} conf={new_conf}"
