"""Dialectic user model — discrete claims about the user backed by
evidence that accumulates over time. Inspired by Honcho's Theory of Mind.

Confidence emerges from evidence rather than being asserted. We use a
weighted, smoothed ratio so that magnitude AND source-trust matter:

  ratio = (Σw_support - Σw_contradict) /
          (Σw_support + Σw_contradict + 3)

    < -0.2  → 'disputed'  (more contradictions than supports by margin)
    < 0.2   → 'low'
    < 0.6   → 'medium'
    else    → 'high'

Each evidence row carries a weight ∈ [0,1]. The weight is auto-derived
from the writer session's `WRITE_ORIGIN`: foreground=1.0 (explicit human
signal), background/shadow/candidate/curator review-forks=0.5 (the system
observing its own behavior — discounted to prevent self-confirmation
loops where claims promoted into brief() shape later observations).
Callers may pass an explicit `weight` to dialectic_evidence; that's
treated as a BASE weight and still multiplied by the origin discount.

A claim with no evidence stays 'low' regardless of age. Smoothing
constant 3 prevents a single piece of evidence from jumping to 'high':
3 foreground supports lands at medium (3/6=0.5), 5 at high (5/8=0.625).
Heavy contradiction still drags a claim down.

In addition to the ratio-derived `confidence` band, each claim carries
a discrete `tier` ∈ {hypothesis, observed, validated, disputed}. Tier
is a state machine with hysteresis — it's the action-gating signal:

  hypothesis → observed:  w_support ≥ 2.0 (claim has accumulated real
                          backing; brief surfaces it as a working pattern)
  observed → validated:   w_support ≥ 4.0 AND no contradict in 14 days
                          (claim is load-bearing; agent defaults to it
                          without asking)
  validated → observed:   any contradict (demote one step on pushback)
  any → disputed:         w_contradict > w_support
  disputed → hypothesis:  w_support > w_contradict (recovery)

Promotion/demotion fires as a discrete event (`tier_promoted` /
`tier_demoted` in events.kind) so the audit trail is queryable, unlike
continuous confidence drift.

Tools:
  dialectic_claim       — register a new claim with optional initial evidence
  dialectic_evidence    — attach evidence (support/contradict) to existing claim
  dialectic_review      — list claims by confidence/domain/state
  dialectic_synthesis   — terse 'who is this user' rendering for brief()
  dialectic_supersede   — retire claim A in favor of claim B (claim_B refines A)

Confidence and tier are recomputed on every evidence add. domain is
free-text but a small enumeration is recommended:
  'style','workflow','values','context','skills','other'.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Optional

from .._mcp import mcp
from ..config import WRITE_ORIGIN
from ..db import get_db
from ..helpers import fmt_age, q, gen_dialectic_id
from ..identity import _ensure_session, _detect_self_cid, _emit


VALID_KINDS = ("support", "contradict")
VALID_CONFIDENCE = ("low", "medium", "high", "disputed")
VALID_STATE = ("active", "retired", "superseded")
VALID_TIER = ("hypothesis", "observed", "validated", "disputed")
SUGGESTED_DOMAINS = (
    "style", "workflow", "values", "context", "skills", "other",
)
SMOOTHING = 3  # see module docstring for rationale

# Multiplicative discount on evidence weight by the writer's WRITE_ORIGIN.
# Foreground = 1.0 (human-attested or direct user signal). All review-fork
# origins = 0.5 (the system observing its own behavior — halved so that
# self-generated evidence can't single-handedly promote a claim into high
# confidence / validated tier).
EVIDENCE_DISCOUNT: dict[str, float] = {
    "foreground": 1.0,
    "shadow_review": 0.5,
    "background_review": 0.5,
    "candidate_review": 0.5,
    "curator": 0.5,
    # Full weight, but NOT a free pass: convene_panel grants the panel_vote
    # origin ONLY to a genuinely adversarial panel (diverse roles + mandatory
    # skeptic, each child free to vote against). A lone review-fork can't
    # elect itself a panel — it runs as background_review (0.5). So the only
    # route to full-weight self-generated evidence is surviving an independent
    # panel that could have contradicted — exactly the corroboration the
    # discount protects against rubber-stamping. Multiplier read from config
    # (PANEL_VOTE_WEIGHT) so the whole calculus is tunable in one place.
    "panel_vote": 1.0,
}

# Tier promotion thresholds. Tuned together with the smoothing constant so
# that a steady foreground stream of supports reaches `validated` in a
# realistic conversation window, while a single review-fork pass cannot.
TIER_OBSERVED_W_SUPPORT = 2.0
TIER_VALIDATED_W_SUPPORT = 4.0
TIER_VALIDATED_QUIET_S = 14 * 86400  # no contradict in this window

# Re-entrance guard: True while recompute_all_tiers() is on the call stack.
# identity._ensure_session reads this to skip the startup heal when
# recompute_all_tiers() is itself the first caller of _ensure_session — which
# would otherwise consume tier promotions before the outer call sees them.
_recompute_in_flight: bool = False


def _evidence_weight(write_origin: str, base_weight: float) -> float:
    """Resolve the effective weight for an evidence row.

    base_weight (default 1.0 from MCP API) multiplied by the origin
    discount; result clamped to [0,1]. The panel_vote multiplier is read
    from config (PANEL_VOTE_WEIGHT) so the promotion calculus stays tunable
    in one place; other origins use the static EVIDENCE_DISCOUNT table."""
    if write_origin == "panel_vote":
        try:
            from ..config import PANEL_VOTE_WEIGHT
            mult = PANEL_VOTE_WEIGHT
        except Exception:
            mult = 1.0
    else:
        mult = EVIDENCE_DISCOUNT.get(write_origin, 1.0)
    return max(0.0, min(1.0, base_weight * mult))


def _weighted_sums(conn: sqlite3.Connection,
                   claim_id: str) -> tuple[float, float, Optional[int]]:
    """Return (w_support, w_contradict, last_contradict_at) for a claim.

    last_contradict_at = max(created_at) over contradict-kind evidence, or
    None if no contradicts exist."""
    row = conn.execute(
        "SELECT "
        "  COALESCE(SUM(CASE WHEN kind='support' THEN weight ELSE 0 END), 0) "
        "    AS ws, "
        "  COALESCE(SUM(CASE WHEN kind='contradict' THEN weight ELSE 0 END), "
        "    0) AS wc, "
        "  MAX(CASE WHEN kind='contradict' THEN created_at END) "
        "    AS last_contradict "
        "FROM dialectic_evidence WHERE claim_id=?",
        (claim_id,),
    ).fetchone()
    if not row:
        return 0.0, 0.0, None
    return float(row["ws"] or 0.0), float(row["wc"] or 0.0), \
        row["last_contradict"]


def _recompute_confidence(conn: sqlite3.Connection, claim_id: str) -> str:
    """Recalculate confidence from weighted evidence sums and persist.

    Returns the new label. Bands match the legacy raw-count behavior when
    every weight equals 1.0 (foreground manual evidence), so existing
    tests are unaffected; weight discounts only narrow the rate at which
    review-fork evidence drives the bands."""
    ws, wc, _ = _weighted_sums(conn, claim_id)
    total = ws + wc
    if total == 0:
        new_conf = "low"
    else:
        ratio = (ws - wc) / (total + SMOOTHING)
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


def _recompute_tier(conn: sqlite3.Connection, claim_id: str,
                    now_t: int) -> tuple[str, str]:
    """Decide the new tier for a claim and persist if changed.

    Returns (old_tier, new_tier). Emits a 'tier_promoted' or
    'tier_demoted' event on transition so the audit trail is queryable.

    State machine:
      hypothesis → observed:   w_support ≥ TIER_OBSERVED_W_SUPPORT
                               AND w_contradict ≤ w_support
      observed   → validated:  w_support ≥ TIER_VALIDATED_W_SUPPORT
                               AND no contradict in TIER_VALIDATED_QUIET_S
      validated  → observed:   any contradict received recently (
                               last_contradict_at within quiet window)
      observed   → hypothesis: w_contradict > w_support (drift back)
      any        → disputed:   w_contradict > w_support AND there's been
                               real opposition (≥1.0 weighted contradict)
      disputed   → hypothesis: w_support > w_contradict (recovery)
    """
    row = conn.execute(
        "SELECT tier FROM user_dialectic WHERE id=?", (claim_id,)
    ).fetchone()
    if not row:
        return "hypothesis", "hypothesis"
    old_tier = row["tier"] or "hypothesis"
    ws, wc, last_c = _weighted_sums(conn, claim_id)

    # Disputed gate first — applies regardless of current tier.
    if wc > ws and wc >= 1.0:
        new_tier = "disputed"
    elif old_tier == "disputed":
        # Recovery: if support has overtaken contradict, slot back to
        # hypothesis (re-earn the higher tiers via the normal path).
        new_tier = "hypothesis" if ws > wc else "disputed"
    elif old_tier == "validated":
        # Demote on recent contradict. The quiet window is symmetric:
        # if a contradict landed within TIER_VALIDATED_QUIET_S, validated
        # is no longer safe to assume.
        if last_c is not None and (now_t - last_c) < TIER_VALIDATED_QUIET_S:
            new_tier = "observed"
        else:
            new_tier = "validated"
    elif old_tier == "observed":
        # Promote on enough support + quiet contradict window.
        quiet = (
            last_c is None
            or (now_t - last_c) >= TIER_VALIDATED_QUIET_S
        )
        if ws >= TIER_VALIDATED_W_SUPPORT and quiet:
            new_tier = "validated"
        elif wc > ws:
            new_tier = "hypothesis"
        else:
            new_tier = "observed"
    else:  # hypothesis
        if ws >= TIER_OBSERVED_W_SUPPORT and ws >= wc:
            new_tier = "observed"
        else:
            new_tier = "hypothesis"

    if new_tier != old_tier:
        conn.execute(
            "UPDATE user_dialectic SET tier=?, tier_changed_at=? WHERE id=?",
            (new_tier, now_t, claim_id),
        )
        # Direction: disputed is below hypothesis, so transitions to/from
        # it count as demotions/promotions accordingly. Ordering used for
        # the event direction tag:
        order = {"disputed": 0, "hypothesis": 1, "observed": 2, "validated": 3}
        direction = (
            "tier_promoted" if order[new_tier] > order[old_tier]
            else "tier_demoted"
        )
        _emit(
            conn, direction, target=claim_id,
            summary=f"{old_tier}→{new_tier} ws={ws:.2f} wc={wc:.2f}",
        )
    return old_tier, new_tier


def recompute_all_tiers() -> int:
    """One-shot: re-run the tier state machine over every active claim until
    each reaches a fixed point. Heals claims seeded before the tier machinery
    landed (tier defaulted to 'hypothesis', tier_changed_at NULL, and
    _recompute_tier only fires on new evidence). Idempotent — returns the
    number of claims whose tier actually changed.

    The state machine advances at most one level per _recompute_tier call
    (hysteresis), so hypothesis→observed→validated needs up to two steps; we
    iterate per claim to settle it."""
    global _recompute_in_flight
    _recompute_in_flight = True
    try:
        conn = get_db()
        _ensure_session(conn)
        now_t = int(time.time())
        rows = conn.execute(
            "SELECT id, tier FROM user_dialectic WHERE state='active'"
        ).fetchall()
        changed = 0
        for r in rows:
            start_tier = r["tier"] or "hypothesis"
            current_tier = start_tier
            for _ in range(4):  # ample to settle a 2-step climb
                _, new_tier = _recompute_tier(conn, r["id"], now_t)
                if new_tier == current_tier:
                    break
                current_tier = new_tier
            if current_tier != start_tier:
                changed += 1
        conn.commit()
        return changed
    finally:
        _recompute_in_flight = False


def _insert_evidence(conn: sqlite3.Connection, claim_id: str, kind: str,
                     quote: str, source: str, base_weight: float,
                     cid: Optional[str], now_t: int) -> int:
    """Write evidence row and bump the claim's counter. Caller commits.

    The stored weight is `base_weight × origin_discount(WRITE_ORIGIN)`.
    The kind-counter (support_count / contradict_count) increments by 1
    regardless of weight — counters are for observability; the weighted
    sums in dialectic_evidence drive confidence + tier."""
    eff_w = _evidence_weight(WRITE_ORIGIN, base_weight)
    cur = conn.execute(
        "INSERT INTO dialectic_evidence (claim_id, kind, source, quote, "
        "weight, created_by_cid, created_at) VALUES (?,?,?,?,?,?,?)",
        (claim_id, kind, source or None, quote or None, eff_w,
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

    Returns: 'ok id=<UCxxx> conf=<level> tier=<tier>'."""
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
        "created_at, tier, tier_changed_at) VALUES (?,?,?,?,?,?,?)",
        (pid, claim, dom, cid_db, now_t, "hypothesis", now_t),
    )
    seed_quote = evidence.strip()
    if seed_quote:
        _insert_evidence(conn, pid, evidence_kind, seed_quote,
                         "manual", 1.0, cid_short, now_t)
    new_conf = _recompute_confidence(conn, pid)
    _, new_tier = _recompute_tier(conn, pid, now_t)
    _emit(conn, "dialectic_claim", target=pid, summary=claim[:140])
    conn.commit()
    return f"ok id={pid} conf={new_conf} tier={new_tier}"


@mcp.tool()
def dialectic_evidence(claim_id: str, kind: str = "support",
                       quote: str = "", source: str = "",
                       weight: float = 1.0) -> str:
    """Attach evidence to a claim. `kind`: 'support' | 'contradict'.

    `source` is a freeform pointer like 'thread:T7f3', 'verbatim:42',
    'dialog:<uuid>', or 'manual'. `weight` ∈ [0,1] is the BASE trust
    (default 1.0); the effective stored weight is base × discount(
    WRITE_ORIGIN of the calling session). foreground sessions store
    `weight` as-is; shadow/background/candidate/curator forks store
    weight × 0.5 to prevent self-confirmation loops.

    Bumps support_count or contradict_count, recomputes confidence and
    tier (which may emit a tier_promoted/demoted event)."""
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
    _, new_tier = _recompute_tier(conn, claim_id, now_t)
    _emit(conn, f"dialectic_evidence:{kind}", target=claim_id,
          summary=(quote or "")[:140])
    conn.commit()
    return (
        f"ok evidence_id={eid} claim={claim_id} "
        f"conf={new_conf} tier={new_tier}"
    )


@mcp.tool()
def dialectic_review(min_confidence: str = "low",
                     domain: str = "",
                     k: int = 20) -> str:
    """List active claims filtered by confidence floor and optional
    domain. Retired/superseded claims are omitted.

    `min_confidence`: one of 'low','medium','high','disputed'. Note that
    'disputed' is treated as its own bucket (not ordered against the
    others) — passing `min_confidence='disputed'` returns only disputed.

    Format: '<id> [conf] tier=<tier> domain=<d> support=N contradict=N
            <claim>'."""
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
        "confidence, tier, last_evidence_at, created_at "
        "FROM user_dialectic WHERE state='active'"
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
        tier_str = r["tier"] or "hypothesis"
        out.append(
            f"{r['id']} [{conf}] tier={tier_str} domain={dom_str} "
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

    Tier markers in the output:
      ★ validated   — load-bearing; act on it without asking
      · observed    — pattern with backing; reference, mention if used
      ? hypothesis  — currently testing (only shown if no observed/validated
                      in same domain, to avoid surfacing weak guesses next
                      to load-bearing facts)

    If `domain` is provided, restricts to that domain (no group headers
    in that case)."""
    conn = get_db()
    sql = (
        "SELECT id, claim, domain, confidence, tier, "
        "  support_count, contradict_count FROM user_dialectic "
        "WHERE state='active' AND confidence IN ('medium','high')"
    )
    params: list = []
    dom_filter = domain.strip()
    if dom_filter:
        sql += " AND domain=?"
        params.append(dom_filter)
    # validated before observed; within each, more evidence first
    sql += (
        " ORDER BY "
        "  CASE tier "
        "    WHEN 'validated' THEN 0 "
        "    WHEN 'observed' THEN 1 "
        "    WHEN 'hypothesis' THEN 2 "
        "    ELSE 3 END, "
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
            tier = r["tier"] or "hypothesis"
            if tier == "validated":
                tag = "★"
            elif tier == "observed":
                tag = "·"
            else:
                tag = "?"
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
                tier = r["tier"] or "hypothesis"
                if tier == "validated":
                    tag = "★"
                elif tier == "observed":
                    tag = "·"
                else:
                    tag = "?"
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

    Returns: 'ok new=<UCxxx> old=<UCxxx> conf=<level> tier=<tier>'."""
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
        "created_at, tier, tier_changed_at) VALUES (?,?,?,?,?,?,?)",
        (pid, new_claim, dom, cid, now_t, "hypothesis", now_t),
    )
    seed_quote = quote.strip()
    if seed_quote:
        _insert_evidence(conn, pid, "support", seed_quote,
                         f"supersede:{old_id}", 1.0, cid, now_t)
    new_conf = _recompute_confidence(conn, pid)
    _, new_tier = _recompute_tier(conn, pid, now_t)
    conn.execute(
        "UPDATE user_dialectic SET state='superseded', superseded_by=? "
        "WHERE id=?",
        (pid, old_id),
    )
    _emit(conn, "dialectic_supersede", target=pid,
          summary=f"{old_id}→{pid} {new_claim[:100]}")
    conn.commit()
    return f"ok new={pid} old={old_id} conf={new_conf} tier={new_tier}"


@mcp.tool()
def dialectic_observation_resolve(id: int, note: str = "") -> str:
    """Mark a dialectic_observations buffer row 'processed' so the validator
    never re-interprets it. Called by the validator child after it has written
    (or deliberately skipped) the observation's claims/evidence."""
    conn = get_db()
    _ensure_session(conn)
    r = conn.execute(
        "SELECT status FROM dialectic_observations WHERE id=?", (int(id),)
    ).fetchone()
    if not r:
        return f"ERR observation_not_found={id}"
    if r["status"] == "processed":
        return f"ok already_processed #{id}"
    now_t = int(time.time())
    conn.execute(
        "UPDATE dialectic_observations SET status='processed', processed_at=? "
        "WHERE id=?",
        (now_t, int(id)),
    )
    _emit(conn, "dialectic_observation_resolve", target=str(id), summary=note[:140])
    conn.commit()
    return f"ok resolved #{id}"
