"""Tests for the tier promotion machinery + source-based evidence discount
added on top of the dialectic user model.

Covers:
- weight discount applied when WRITE_ORIGIN != 'foreground'
- weighted confidence formula (5 foreground supports → high; 10
  shadow-origin supports → same band, since 10 × 0.5 = 5)
- tier state machine transitions (hypothesis→observed→validated, demote
  on contradict, disputed gate)
- tier_promoted / tier_demoted events emitted on transitions
- brief() user_model section reflects tier (★ validated, · observed)
  and a separate `currently_testing` block surfaces hypothesis claims
"""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest


_FAKE_CID = "aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000"


def _tools(pkg):
    return pkg["mcp"]._tool_manager._tools


def _bootstrap(tmp_path, monkeypatch, write_origin: str = "foreground"):
    """Build a fresh threadkeeper instance with the chosen WRITE_ORIGIN.

    The discount kicks in based on `config.WRITE_ORIGIN`, which is read
    from env once at import time, so we MUST set the env before purging
    sys.modules.
    """
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
        "tmp": tmp_path,
    }


# ── discount on evidence weight ────────────────────────────────────────


def test_foreground_evidence_weight_is_one(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="foreground")
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(claim="probe", domain="style")
    cid = out.split()[1].split("=", 1)[1]
    t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    row = pkg["db"].get_db().execute(
        "SELECT weight FROM dialectic_evidence WHERE claim_id=?", (cid,)
    ).fetchone()
    assert row["weight"] == 1.0


def test_shadow_review_origin_halves_evidence_weight(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(claim="probe", domain="style")
    cid = out.split()[1].split("=", 1)[1]
    # Default base weight 1.0 × shadow discount 0.5 = 0.5
    t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    row = pkg["db"].get_db().execute(
        "SELECT weight FROM dialectic_evidence WHERE claim_id=? "
        "ORDER BY id DESC LIMIT 1", (cid,)
    ).fetchone()
    assert row["weight"] == pytest.approx(0.5)


def test_background_review_origin_halves_evidence_weight(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="background_review")
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(claim="probe", domain="style")
    cid = out.split()[1].split("=", 1)[1]
    t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    row = pkg["db"].get_db().execute(
        "SELECT weight FROM dialectic_evidence WHERE claim_id=?", (cid,)
    ).fetchone()
    assert row["weight"] == pytest.approx(0.5)


def test_explicit_base_weight_still_multiplied_by_discount(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(claim="probe", domain="style")
    cid = out.split()[1].split("=", 1)[1]
    # base 0.8 × 0.5 discount = 0.4
    t["dialectic_evidence"].fn(claim_id=cid, kind="support", weight=0.8)
    row = pkg["db"].get_db().execute(
        "SELECT weight FROM dialectic_evidence WHERE claim_id=?", (cid,)
    ).fetchone()
    assert row["weight"] == pytest.approx(0.4)


def test_unknown_origin_no_discount(tmp_path, monkeypatch):
    """Falls back to multiplier 1.0 for write_origins not in the table.
    Tests don't break under custom origins; default behavior is no-op."""
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="random_unknown")
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(claim="probe", domain="style")
    cid = out.split()[1].split("=", 1)[1]
    t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    row = pkg["db"].get_db().execute(
        "SELECT weight FROM dialectic_evidence WHERE claim_id=?", (cid,)
    ).fetchone()
    assert row["weight"] == 1.0


# ── weighted confidence formula ─────────────────────────────────────────


def test_shadow_origin_needs_twice_as_much_evidence_for_high(
    tmp_path, monkeypatch,
):
    """5 foreground supports = high (ratio 5/8 = 0.625). 5 shadow supports
    = ratio 2.5/(2.5+3) = 0.45 = medium. 10 shadow supports = ratio 5/8 =
    0.625 = high. The discount makes review-fork evidence buy half as
    much confidence."""
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(claim="halved", domain="style")
    cid = out.split()[1].split("=", 1)[1]
    for _ in range(5):
        t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    conf = pkg["db"].get_db().execute(
        "SELECT confidence FROM user_dialectic WHERE id=?", (cid,)
    ).fetchone()["confidence"]
    assert conf == "medium", (
        "5 shadow-origin supports should land at medium, not high — "
        "discount must apply"
    )
    # 5 more shadow supports (10 total = 5.0 weighted) → high
    for _ in range(5):
        t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    conf = pkg["db"].get_db().execute(
        "SELECT confidence FROM user_dialectic WHERE id=?", (cid,)
    ).fetchone()["confidence"]
    assert conf == "high"


# ── tier transitions ────────────────────────────────────────────────────


def test_new_claim_starts_at_hypothesis(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(claim="fresh", domain="style")
    assert "tier=hypothesis" in out
    cid = out.split()[1].split("=", 1)[1]
    row = pkg["db"].get_db().execute(
        "SELECT tier FROM user_dialectic WHERE id=?", (cid,)
    ).fetchone()
    assert row["tier"] == "hypothesis"


def test_two_foreground_supports_promote_to_observed(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(claim="probe", domain="style")
    cid = out.split()[1].split("=", 1)[1]
    t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    # After 1 support — still hypothesis
    row = pkg["db"].get_db().execute(
        "SELECT tier FROM user_dialectic WHERE id=?", (cid,)
    ).fetchone()
    assert row["tier"] == "hypothesis"
    t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    # After 2 supports — promoted to observed
    row = pkg["db"].get_db().execute(
        "SELECT tier FROM user_dialectic WHERE id=?", (cid,)
    ).fetchone()
    assert row["tier"] == "observed"


def test_four_foreground_supports_promote_to_validated(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(claim="probe", domain="style")
    cid = out.split()[1].split("=", 1)[1]
    for _ in range(4):
        t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    row = pkg["db"].get_db().execute(
        "SELECT tier FROM user_dialectic WHERE id=?", (cid,)
    ).fetchone()
    assert row["tier"] == "validated"


def test_shadow_supports_need_twice_as_many_for_validated(
    tmp_path, monkeypatch,
):
    """8 shadow-origin supports = w_support 4.0 → validated. 7 shadow =
    3.5 → stays observed. Mirrors the dialectic-confidence behavior."""
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(claim="slow", domain="style")
    cid = out.split()[1].split("=", 1)[1]
    for _ in range(7):
        t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    row = pkg["db"].get_db().execute(
        "SELECT tier FROM user_dialectic WHERE id=?", (cid,)
    ).fetchone()
    assert row["tier"] == "observed", (
        "7 shadow supports (w=3.5) should not yet validate"
    )
    t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    row = pkg["db"].get_db().execute(
        "SELECT tier FROM user_dialectic WHERE id=?", (cid,)
    ).fetchone()
    assert row["tier"] == "validated"


def test_validated_demotes_to_observed_on_contradict(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(claim="rock-solid", domain="style")
    cid = out.split()[1].split("=", 1)[1]
    for _ in range(4):
        t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    row = pkg["db"].get_db().execute(
        "SELECT tier FROM user_dialectic WHERE id=?", (cid,)
    ).fetchone()
    assert row["tier"] == "validated"
    # A single contradict demotes back to observed
    t["dialectic_evidence"].fn(claim_id=cid, kind="contradict")
    row = pkg["db"].get_db().execute(
        "SELECT tier FROM user_dialectic WHERE id=?", (cid,)
    ).fetchone()
    assert row["tier"] == "observed"


def test_heavy_contradict_moves_to_disputed(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(claim="contested", domain="style")
    cid = out.split()[1].split("=", 1)[1]
    t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    for _ in range(3):
        t["dialectic_evidence"].fn(claim_id=cid, kind="contradict")
    row = pkg["db"].get_db().execute(
        "SELECT tier FROM user_dialectic WHERE id=?", (cid,)
    ).fetchone()
    assert row["tier"] == "disputed"


def test_disputed_recovers_to_hypothesis_when_support_overtakes(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(claim="rebound", domain="style")
    cid = out.split()[1].split("=", 1)[1]
    # Drag into disputed: 1 support, 3 contradicts
    t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    for _ in range(3):
        t["dialectic_evidence"].fn(claim_id=cid, kind="contradict")
    row = pkg["db"].get_db().execute(
        "SELECT tier FROM user_dialectic WHERE id=?", (cid,)
    ).fetchone()
    assert row["tier"] == "disputed"
    # Now add 4 supports → 5 vs 3 → not disputed, back to hypothesis
    for _ in range(4):
        t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    row = pkg["db"].get_db().execute(
        "SELECT tier FROM user_dialectic WHERE id=?", (cid,)
    ).fetchone()
    assert row["tier"] in ("hypothesis", "observed"), (
        "Recovery path goes hypothesis first; further supports may "
        "promote it. Either is acceptable here."
    )


# ── tier_promoted/demoted events ───────────────────────────────────────


def test_tier_promotion_emits_event(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(claim="tracked", domain="style")
    cid = out.split()[1].split("=", 1)[1]
    t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    rows = pkg["db"].get_db().execute(
        "SELECT kind, summary FROM events WHERE kind IN "
        "('tier_promoted','tier_demoted') AND target=?",
        (cid,),
    ).fetchall()
    assert any(r["kind"] == "tier_promoted" for r in rows)
    promo = [r for r in rows if r["kind"] == "tier_promoted"][0]
    assert "hypothesis→observed" in promo["summary"]


def test_tier_demotion_emits_event(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(claim="brittle", domain="style")
    cid = out.split()[1].split("=", 1)[1]
    for _ in range(4):
        t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    t["dialectic_evidence"].fn(claim_id=cid, kind="contradict")
    rows = pkg["db"].get_db().execute(
        "SELECT kind, summary FROM events WHERE kind='tier_demoted' AND target=?",
        (cid,),
    ).fetchall()
    assert rows, "demotion must emit tier_demoted event"
    assert any("validated→observed" in r["summary"] for r in rows)


# ── brief.user_model and currently_testing rendering ──────────────────


def test_brief_validated_marked_with_star(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(
        claim="prefers Russian lean prose", domain="style",
    )
    cid = out.split()[1].split("=", 1)[1]
    for _ in range(4):
        t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    txt = t["brief"].fn()
    assert "user_model (dialectic)" in txt
    assert "★ prefers Russian lean prose" in txt


def test_brief_observed_marked_with_dot(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(claim="midsize claim", domain="style")
    cid = out.split()[1].split("=", 1)[1]
    for _ in range(3):  # tier=observed (ws=3, not yet validated at ≥4)
        t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    txt = t["brief"].fn()
    assert "user_model (dialectic)" in txt
    assert "· midsize claim" in txt


def test_brief_currently_testing_renders_hypothesis(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    t = _tools(pkg)
    out = t["dialectic_claim"].fn(
        claim="rough probe about workflow", domain="workflow",
    )
    cid = out.split()[1].split("=", 1)[1]
    # One support → still hypothesis (needs 2 for observed)
    t["dialectic_evidence"].fn(claim_id=cid, kind="support")
    txt = t["brief"].fn()
    assert "currently_testing" in txt
    assert "rough probe about workflow" in txt
    # And it should NOT be in user_model (which only shows
    # observed/validated)
    assert "user_model (dialectic)" not in txt or "★ rough probe" not in txt


def test_brief_currently_testing_omitted_when_no_support(tmp_path, monkeypatch):
    """Hypothesis claims with 0 supports don't surface — they're brand-new
    and hold no information. Brief budget is precious."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    t = _tools(pkg)
    t["dialectic_claim"].fn(claim="no evidence yet", domain="style")
    txt = t["brief"].fn()
    assert "currently_testing" not in txt
