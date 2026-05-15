"""Integration tests for the three new brief() sections:
  - memory_nudge: counter-driven push to consolidate memory
  - skill_nudge:  counter-driven push to materialize / patch a skill
  - user_model:   dialectic synthesis of high+medium confidence claims

The compute_* functions are unit-tested in test_nudges.py and the dialectic
tools in test_dialectic.py. This file verifies they actually surface in the
brief output.
"""
from __future__ import annotations

import time
import os


_FAKE_CID = "dddddddd-eeee-ffff-0000-111122223333"


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _brief_text(pkg, **kwargs):
    return pkg["mcp"]._tool_manager._tools["brief"].fn(**kwargs)


def _seed_rich_active_thread(pkg, n_notes=4):
    """Active thread with enough notes to satisfy `_has_rich_thread`."""
    open_t = _tool(pkg, "open_thread")
    note = _tool(pkg, "note")
    tid = open_t(question="rich working thread")
    for i in range(n_notes):
        note(thread_id=tid, content=f"note {i}", kind="insight" if i % 2 else "move")
    return tid


def _seed_rich_closed_thread(pkg, n_total=6, n_rich=3):
    open_t = _tool(pkg, "open_thread")
    note = _tool(pkg, "note")
    close = _tool(pkg, "close_thread")
    tid = open_t(question="rich closed thread")
    for i in range(n_total):
        kind = "insight" if i < n_rich // 2 else ("move" if i < n_rich else "open_q")
        note(thread_id=tid, content=f"closed note {i}", kind=kind)
    close(thread_id=tid, outcome="ok")
    return tid


def _emit_filler_events(pkg, n: int):
    """Insert n events of a non-reset kind to push the counter forward."""
    conn = pkg["db"].get_db()
    sess = pkg["identity"]._session_id or "test"
    now = int(time.time())
    for i in range(n):
        conn.execute(
            "INSERT INTO events (session_id, kind, target, created_at) "
            "VALUES (?, ?, ?, ?)",
            (sess, "search", None, now + i),
        )
    conn.commit()


# ──────────────────────────────────────────────────────────────────────
# memory_nudge in brief
# ──────────────────────────────────────────────────────────────────────

def test_memory_nudge_does_not_show_below_threshold(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_MEMORY_NUDGE_INTERVAL", "10")
    pkg = mp_with_cid(_FAKE_CID)
    _seed_rich_active_thread(pkg)
    # No filler events → counter stays at 0
    txt = _brief_text(pkg)
    assert "memory_nudge" not in txt


def test_memory_nudge_surfaces_above_threshold(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_MEMORY_NUDGE_INTERVAL", "3")
    pkg = mp_with_cid(_FAKE_CID)
    _seed_rich_active_thread(pkg)
    _emit_filler_events(pkg, 5)
    txt = _brief_text(pkg)
    assert "memory_nudge" in txt
    assert "CONSOLIDATE" in txt or "MUST consolidate" in txt


def test_memory_nudge_demanding_at_double_threshold(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_MEMORY_NUDGE_INTERVAL", "3")
    pkg = mp_with_cid(_FAKE_CID)
    _seed_rich_active_thread(pkg)
    _emit_filler_events(pkg, 8)  # 8 ≥ 2 * 3
    txt = _brief_text(pkg)
    assert "memory_nudge" in txt
    assert "⚠️" in txt
    assert "overdue=2x" in txt


def test_memory_nudge_silent_without_rich_thread(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_MEMORY_NUDGE_INTERVAL", "3")
    pkg = mp_with_cid(_FAKE_CID)
    # No rich thread → suppressed even when counter ready
    _emit_filler_events(pkg, 10)
    txt = _brief_text(pkg)
    assert "memory_nudge" not in txt


# ──────────────────────────────────────────────────────────────────────
# skill_nudge in brief
# ──────────────────────────────────────────────────────────────────────

def test_skill_nudge_surfaces_when_rich_closed_present(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_SKILL_NUDGE_INTERVAL", "3")
    pkg = mp_with_cid(_FAKE_CID)
    _seed_rich_closed_thread(pkg, n_total=6, n_rich=3)
    _emit_filler_events(pkg, 5)
    txt = _brief_text(pkg)
    assert "skill_nudge" in txt
    assert "review_thread" in txt or "skill_manage" in txt


def test_consulted_skills_surfaces_when_session_records_skill_events(
    mp_with_cid, monkeypatch,
):
    """brief() must surface a `consulted_skills` block listing skills
    invoked / viewed / patched in the current session, plus any
    user-judgment outcomes. Drives the patch-loop in the next turn."""
    pkg = mp_with_cid(_FAKE_CID)
    sr = _tool(pkg, "skill_record")
    sr(name="payout-flow-debug", kind="view")
    sr(name="payout-flow-debug", kind="use", outcome="helped")
    sr(name="wda-recovery", kind="use", outcome="wrong")
    sr(name="wda-recovery", kind="use", outcome="wrong")
    txt = _brief_text(pkg)
    assert "consulted_skills" in txt
    assert "payout-flow-debug" in txt
    assert "wda-recovery" in txt
    assert "viewed×1" in txt
    assert "helped×1" in txt
    assert "wrong×2" in txt


def test_consulted_skills_silent_without_events(mp_with_cid):
    """Empty section in fresh-session case — no consulted_skills line
    when no skill_record events present."""
    pkg = mp_with_cid(_FAKE_CID)
    txt = _brief_text(pkg)
    assert "consulted_skills" not in txt


def test_skill_nudge_silent_after_materialization(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_SKILL_NUDGE_INTERVAL", "3")
    pkg = mp_with_cid(_FAKE_CID)
    tid = _seed_rich_closed_thread(pkg, n_total=6, n_rich=3)
    _emit_filler_events(pkg, 5)
    # Materialize → silence
    mark = _tool(pkg, "mark_skill_materialized")
    mark(thread_id=tid, skill_path="/tmp/foo")
    txt = _brief_text(pkg)
    # skill_hint and skill_nudge BOTH suppressed for the thread we materialized
    assert "skill_nudge" not in txt


# ──────────────────────────────────────────────────────────────────────
# user_model (dialectic) in brief
# ──────────────────────────────────────────────────────────────────────

def test_user_model_section_omitted_when_no_high_confidence(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    txt = _brief_text(pkg)
    assert "user_model" not in txt


def _extract_claim_id(result: str) -> str:
    """dialectic_claim/supersede returns 'ok id=UCxxx conf=<level>'."""
    for part in result.split():
        if part.startswith("id="):
            return part[3:]
    raise AssertionError(f"could not parse claim id from: {result}")


def test_user_model_surfaces_high_confidence_claim(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    claim = _tool(pkg, "dialectic_claim")
    evidence = _tool(pkg, "dialectic_evidence")
    # 5 supports under smoothing 3 → 5/8 = 0.625 → high
    claim_id = _extract_claim_id(
        claim(claim="prefers Russian lean prose", domain="style")
    )
    for _ in range(5):
        evidence(claim_id=claim_id, kind="support", quote="ru prose")
    txt = _brief_text(pkg)
    assert "user_model" in txt
    assert "[style]" in txt
    assert "prefers Russian lean prose" in txt
    assert "★" in txt  # high-confidence marker


def test_user_model_omits_disputed_claims(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    claim = _tool(pkg, "dialectic_claim")
    evidence = _tool(pkg, "dialectic_evidence")
    claim_id = _extract_claim_id(claim(claim="hates emojis", domain="style"))
    # 1 support + 5 contradicts → ratio negative → disputed
    evidence(claim_id=claim_id, kind="support", quote="ok")
    for _ in range(5):
        evidence(claim_id=claim_id, kind="contradict", quote="emoji used")
    txt = _brief_text(pkg)
    # disputed never surfaces
    assert "hates emojis" not in txt
