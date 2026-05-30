"""Judge panel — fill the distill/dialectic vote quorum with spawned agents.

The vote quorum on distillates (`vote_sum >= 2`) and the dialectic tier
ladder (`w_support >= 2.0/4.0`) were designed for multiple independent
voters. In a single-CLI install they never fill: one human, and the
system's own review-forks are discounted to 0.5 precisely so they can't
self-promote. So good distillates/claims sat un-promoted forever.

This closes that gap the way the user intended: spawn a small panel of
agents that each evaluate the target INDEPENDENTLY and vote — and may vote
AGAINST. The honesty guard is structural, not trust-based:

  - A panel earns the non-discounted `panel_vote` origin ONLY when it is
    adversarial: multiple distinct roles including a mandatory skeptic
    (PANEL_REQUIRE_SKEPTIC). Otherwise children run as `background_review`
    (0.5) and can't move the needle.
  - The SPAWNER grants the origin once, for the whole panel. No child sets
    its own origin, so a lone fork can't elect itself a one-member panel.

Distill votes aren't origin-discounted (raw sum per distinct voter_cid), so
a distill panel works by simple headcount — N children = N voters. Dialectic
evidence IS discounted, so there the `panel_vote` origin is what lifts each
child's vote to full weight.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Optional

from .._mcp import mcp
from ..config import (
    PANEL_SIZE, PANEL_ROLES, PANEL_REQUIRE_SKEPTIC,
    PANEL_MODEL, PANEL_EFFORT,
)
from ..db import get_db
from ..identity import _ensure_session, _emit

PANEL_PROMPT_PREFIX = "You are a PANEL JUDGE"

_VALID_KINDS = ("distill", "claim")


def _roles(size: Optional[int] = None, override: str = "") -> list[str]:
    """Resolve the panel's role list: explicit override (comma-sep) else
    config PANEL_ROLES, trimmed/padded to `size` (default PANEL_SIZE)."""
    n = PANEL_SIZE if size is None else max(1, int(size))
    base = (
        [r.strip() for r in override.split(",") if r.strip()]
        if override.strip() else list(PANEL_ROLES)
    )
    if not base:
        base = ["skeptic", "critic", "generator"]
    # cycle roles if size exceeds the configured set
    return [base[i % len(base)] for i in range(n)]


def _is_adversarial(roles: list[str]) -> bool:
    """A panel is adversarial when it can genuinely dissent. With the
    skeptic requirement on, that means a skeptic is present; off, any panel
    with ≥2 distinct roles qualifies."""
    if PANEL_REQUIRE_SKEPTIC:
        return any(r.lower() == "skeptic" for r in roles)
    return len(set(r.lower() for r in roles)) >= 2


def _panel_origin(roles: list[str]) -> str:
    """Origin the spawner grants the whole panel. Adversarial → full-weight
    `panel_vote`; otherwise the discounted `background_review` so a
    rubber-stamp panel can't promote anything."""
    return "panel_vote" if _is_adversarial(roles) else "background_review"


def _fetch_target(conn: sqlite3.Connection, kind: str,
                  target_id: str) -> Optional[str]:
    """Return the human-readable content of the target, or None if absent."""
    tid = target_id.strip()
    if kind == "distill":
        row = conn.execute(
            "SELECT content FROM distill WHERE id=?", (tid,)
        ).fetchone()
        return row["content"] if row else None
    row = conn.execute(
        "SELECT claim FROM user_dialectic WHERE id=?", (tid,)
    ).fetchone()
    return row["claim"] if row else None


def _child_prompt(kind: str, target_id: str, content: str,
                  role: str) -> str:
    """Build the per-child judging prompt. The child evaluates independently
    and casts ONE vote via the kind-appropriate tool; it is explicitly told
    it may vote against."""
    head = (
        f"{PANEL_PROMPT_PREFIX} (role: {role}). You evaluate ONE item "
        "independently and cast a single vote. You MAY vote against — an "
        "honest negative verdict is the point of the panel, not a failure.\n\n"
    )
    if kind == "distill":
        return head + (
            f"DISTILLATE {target_id}:\n{content}\n\n"
            "Decide how much you endorse this as a durable, reusable insight. "
            "Then call exactly once:\n"
            f"  vote_distill(distill_id='{target_id}', weight=W)\n"
            "where W ∈ [-1, +1]: +1 strong endorse, 0 neutral, -1 reject. "
            "Vote your honest independent verdict, then stop."
        )
    return head + (
        f"CLAIM {target_id} (about the user):\n{content}\n\n"
        "Decide whether your independent reading of the evidence SUPPORTS or "
        "CONTRADICTS this claim. Then call exactly once:\n"
        "  dialectic_evidence(claim_id='" + target_id + "', "
        "evidence_kind='support'|'contradict', evidence='<your one-line "
        "reasoning>')\n"
        "Use 'contradict' if you disagree. Vote once, then stop."
    )


@mcp.tool()
def convene_panel(target_kind: str, target_id: str,
                  size: int = 0, roles: str = "") -> str:
    """Spawn a panel of independent agents to vote on a distillate or claim,
    filling the promotion quorum the way a second human otherwise would.

    `target_kind`: 'distill' (vote via vote_distill) or 'claim' (vote via
    dialectic_evidence). `target_id`: Dxxx or UCxxx. `size`/`roles` override
    the configured PANEL_SIZE / PANEL_ROLES.

    The panel runs adversarially: with a skeptic present (default), each
    child's vote carries full weight (`panel_vote` origin); a panel without
    a skeptic is discounted so it can't rubber-stamp. Children are
    fire-and-forget — they vote directly into the DB and aggregates
    recompute per vote; check pending_distillates() / the dialectic brief
    afterward."""
    kind = target_kind.strip().lower()
    if kind not in _VALID_KINDS:
        return f"ERR bad_target_kind={kind} (distill|claim)"
    conn = get_db()
    _ensure_session(conn)
    content = _fetch_target(conn, kind, target_id)
    if content is None:
        return f"ERR target_not_found kind={kind} id={target_id}"

    role_list = _roles(size or None, roles)
    origin = _panel_origin(role_list)
    vote_tool = (
        "mcp__thread-keeper__vote_distill" if kind == "distill"
        else "mcp__thread-keeper__dialectic_evidence"
    )
    allowed = (
        f"{vote_tool},"
        "mcp__thread-keeper__brief,"
        "mcp__thread-keeper__context,"
        "mcp__thread-keeper__search,"
        "mcp__thread-keeper__dialog_search"
    )

    from .spawn import spawn  # late import — avoids import cycle
    spawned: list[str] = []
    errors: list[str] = []
    for role in role_list:
        prompt = _child_prompt(kind, target_id.strip(), content, role)
        try:
            res = spawn(
                prompt=prompt,
                visible=False,
                capture_output=True,
                permission_mode="auto",
                role=role,
                write_origin=origin,
                slim=True,
                model=PANEL_MODEL,
                effort=PANEL_EFFORT,
                extra_allowed_tools=allowed,
            )
        except Exception as e:  # noqa: BLE001 — never crash the caller
            errors.append(f"{role}:{e}")
            continue
        m = re.search(r"task=(\S+)", str(res))
        spawned.append(m.group(1) if m else role)

    _emit(conn, "convene_panel", target=target_id.strip(),
          summary=f"{kind} origin={origin} roles={','.join(role_list)} "
                  f"spawned={len(spawned)}")
    conn.commit()
    adv = "adversarial" if origin == "panel_vote" else "discounted"
    out = [
        f"panel kind={kind} target={target_id.strip()} {adv} "
        f"origin={origin} size={len(role_list)} "
        f"roles={','.join(role_list)} spawned={len(spawned)}"
    ]
    if errors:
        out.append("spawn_errors: " + "; ".join(errors))
    return "\n".join(out)
