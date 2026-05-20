"""Tests for the skill_usage tier promotion + curator tier-awareness
added on top of the existing skill_manage/skill_record/curator_run.

Covers:
- foreground 'use' bumps foreground_use_count; review-origin 'use' does not
- tier promotion: hypothesis → observed → validated based on foreground
  usage; demotion on 'wrong' outcome
- skill_tier_promoted / skill_tier_demoted events on transitions
- Curator never archives validated tier
- Hypothesis tier ages at half the stale window
- skill_list output includes the tier column
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest


_FAKE_CID = "ddddeeee-ffff-0011-2233-445566778899"


def _bootstrap_skills(tmp_path, monkeypatch, write_origin: str = "foreground"):
    skills_root = tmp_path / "claude_skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CURATOR_INTERVAL_S": "0",
        "THREADKEEPER_EXTRACT_INTERVAL_S": "0",
        "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_FORCE_CID": _FAKE_CID,
        "CLAUDE_SKILLS_DIR": str(skills_root),
        "THREADKEEPER_WRITE_ORIGIN": write_origin,
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import _mcp, db

    return {
        "mcp": _mcp.mcp,
        "db": db,
        "skills_root": skills_root,
        "tmp": tmp_path,
    }


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _seed_skill(pkg, name="seed-skill"):
    _tool(pkg, "skill_manage")(
        action="create",
        name=name,
        description=f"Use when seeding {name} for a test.",
        content="# Seed\n\nOriginal body.\n",
    )


def _tier(pkg, name: str) -> str:
    return pkg["db"].get_db().execute(
        "SELECT tier FROM skill_usage WHERE name=?", (name,)
    ).fetchone()["tier"]


def _fg_uses(pkg, name: str) -> int:
    return pkg["db"].get_db().execute(
        "SELECT foreground_use_count FROM skill_usage WHERE name=?", (name,)
    ).fetchone()["foreground_use_count"]


# ── foreground vs review-fork usage counter ─────────────────────────


def test_foreground_use_bumps_foreground_counter(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch, write_origin="foreground")
    _seed_skill(pkg, "fg-skill")
    _tool(pkg, "skill_record")(name="fg-skill", kind="use")
    assert _fg_uses(pkg, "fg-skill") == 1


def test_shadow_review_use_does_not_bump_foreground_counter(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap_skills(
        tmp_path, monkeypatch, write_origin="shadow_review",
    )
    _seed_skill(pkg, "shadow-skill")
    _tool(pkg, "skill_record")(name="shadow-skill", kind="use")
    # use_count still increments — fg_uses does not
    row = pkg["db"].get_db().execute(
        "SELECT use_count, foreground_use_count FROM skill_usage WHERE name=?",
        ("shadow-skill",),
    ).fetchone()
    assert row["use_count"] == 1
    assert row["foreground_use_count"] == 0


def test_background_review_use_does_not_bump_foreground_counter(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap_skills(
        tmp_path, monkeypatch, write_origin="background_review",
    )
    _seed_skill(pkg, "bg-skill")
    _tool(pkg, "skill_record")(name="bg-skill", kind="use")
    assert _fg_uses(pkg, "bg-skill") == 0


# ── tier promotion ────────────────────────────────────────────────


def test_new_skill_starts_at_hypothesis(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    _seed_skill(pkg, "fresh")
    assert _tier(pkg, "fresh") == "hypothesis"


def test_two_foreground_uses_promote_to_observed(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    _seed_skill(pkg, "climber")
    rec = _tool(pkg, "skill_record")
    rec(name="climber", kind="use")
    assert _tier(pkg, "climber") == "hypothesis"
    rec(name="climber", kind="use")
    assert _tier(pkg, "climber") == "observed"


def test_five_foreground_uses_promote_to_validated(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    _seed_skill(pkg, "trusted")
    rec = _tool(pkg, "skill_record")
    for _ in range(5):
        rec(name="trusted", kind="use")
    assert _tier(pkg, "trusted") == "validated"


def test_shadow_uses_alone_never_promote(tmp_path, monkeypatch):
    """The whole point of fg-discount: a skill that only the system itself
    uses (via review-forks) must never auto-promote to observed/validated."""
    pkg = _bootstrap_skills(
        tmp_path, monkeypatch, write_origin="shadow_review",
    )
    _seed_skill(pkg, "self-used")
    rec = _tool(pkg, "skill_record")
    for _ in range(20):
        rec(name="self-used", kind="use")
    assert _tier(pkg, "self-used") == "hypothesis"


# ── demotion on 'wrong' outcome ───────────────────────────────────


def test_validated_demotes_to_observed_on_wrong(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    _seed_skill(pkg, "demoted")
    rec = _tool(pkg, "skill_record")
    for _ in range(5):
        rec(name="demoted", kind="use")
    assert _tier(pkg, "demoted") == "validated"
    rec(name="demoted", kind="use", outcome="wrong")
    assert _tier(pkg, "demoted") == "observed"


def test_two_wrong_outcomes_demote_observed_to_hypothesis(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    _seed_skill(pkg, "twice-wrong")
    rec = _tool(pkg, "skill_record")
    # Promote to observed
    rec(name="twice-wrong", kind="use")
    rec(name="twice-wrong", kind="use")
    assert _tier(pkg, "twice-wrong") == "observed"
    rec(name="twice-wrong", kind="use", outcome="wrong")
    rec(name="twice-wrong", kind="use", outcome="wrong")
    assert _tier(pkg, "twice-wrong") == "hypothesis"


def test_helped_outcome_does_not_demote(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    _seed_skill(pkg, "still-good")
    rec = _tool(pkg, "skill_record")
    for _ in range(5):
        rec(name="still-good", kind="use", outcome="helped")
    assert _tier(pkg, "still-good") == "validated"


# ── tier_promoted/demoted events ──────────────────────────────────


def test_skill_promotion_emits_event(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    _seed_skill(pkg, "evented")
    rec = _tool(pkg, "skill_record")
    rec(name="evented", kind="use")
    rec(name="evented", kind="use")
    rows = pkg["db"].get_db().execute(
        "SELECT kind, summary FROM events "
        "WHERE kind IN ('skill_tier_promoted','skill_tier_demoted') "
        "  AND target=?",
        ("evented",),
    ).fetchall()
    assert any(r["kind"] == "skill_tier_promoted" for r in rows)
    promo = [r for r in rows if r["kind"] == "skill_tier_promoted"][0]
    assert "hypothesis→observed" in promo["summary"]


# ── curator tier-awareness ────────────────────────────────────────


def _force_agent_origin(pkg, name: str, last_used_offset_days: int = 0):
    """Mark a freshly-created skill as agent-authored with aged activity."""
    conn = pkg["db"].get_db()
    now = int(time.time())
    activity = now - last_used_offset_days * 86400
    conn.execute(
        "UPDATE skill_usage SET created_by_origin='background_review', "
        "created_at=?, last_used_at=?, last_viewed_at=?, last_patched_at=? "
        "WHERE name=?",
        (activity, activity, activity, activity, name),
    )
    conn.commit()


def test_curator_never_archives_validated(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    _seed_skill(pkg, "ancient-validated")
    # Promote to validated via foreground use
    rec = _tool(pkg, "skill_record")
    for _ in range(5):
        rec(name="ancient-validated", kind="use")
    assert _tier(pkg, "ancient-validated") == "validated"
    # Now age it to 200 days ago AND mark as agent-origin so it would
    # normally be archived
    conn = pkg["db"].get_db()
    now = int(time.time())
    far_past = now - 200 * 86400
    conn.execute(
        "UPDATE skill_usage SET created_by_origin='background_review', "
        "created_at=?, last_used_at=?, last_viewed_at=?, last_patched_at=? "
        "WHERE name=?",
        (far_past, far_past, far_past, far_past, "ancient-validated"),
    )
    conn.commit()
    cur = _tool(pkg, "curator_run")
    result = cur(dry_run=True)
    # Validated skills must NOT appear in the plan
    assert "ancient-validated" not in result


def test_curator_ages_hypothesis_faster(tmp_path, monkeypatch):
    """Hypothesis tier with last activity 20 days ago should be marked
    stale (window scaled to 15d = 30d * 0.5). Observed tier at 20 days
    should not be — it uses the full 30d window."""
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    _seed_skill(pkg, "young-hypothesis")
    _seed_skill(pkg, "young-observed")
    # Promote observed-target to observed
    rec = _tool(pkg, "skill_record")
    rec(name="young-observed", kind="use")
    rec(name="young-observed", kind="use")
    assert _tier(pkg, "young-observed") == "observed"
    # Age both 20 days, make both background_review origin
    _force_agent_origin(pkg, "young-hypothesis", last_used_offset_days=20)
    _force_agent_origin(pkg, "young-observed", last_used_offset_days=20)

    cur = _tool(pkg, "curator_run")
    result = cur(dry_run=True)
    # hypothesis past the half-window threshold → stale
    assert "young-hypothesis: active → stale" in result
    # observed still within the full window → no transition
    assert "young-observed" not in result


def test_curator_observed_uses_default_window(tmp_path, monkeypatch):
    """Sanity check: observed at 40d (past default 30d) is stale-aged."""
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    _seed_skill(pkg, "elderly-observed")
    rec = _tool(pkg, "skill_record")
    rec(name="elderly-observed", kind="use")
    rec(name="elderly-observed", kind="use")
    assert _tier(pkg, "elderly-observed") == "observed"
    _force_agent_origin(pkg, "elderly-observed", last_used_offset_days=40)
    cur = _tool(pkg, "curator_run")
    result = cur(dry_run=True)
    assert "elderly-observed: active → stale" in result


# ── skill_list output ─────────────────────────────────────────────


def test_skill_list_shows_tier(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    _seed_skill(pkg, "tier-shown")
    txt = _tool(pkg, "skill_list")()
    assert "tier=hypothesis" in txt
    rec = _tool(pkg, "skill_record")
    for _ in range(5):
        rec(name="tier-shown", kind="use")
    txt = _tool(pkg, "skill_list")()
    assert "tier=validated" in txt
    assert "fg_uses=5" in txt
