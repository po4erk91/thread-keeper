"""Helpful/harmful counters, contribution-based retirement and the active cap."""
from __future__ import annotations

import time

from tests.test_skills import _bootstrap_skills, _tool


def _skill_row(conn, name, origin="candidate_review", uses=0, wrong=0, last_used_ago=0, tier="observed"):
    now = int(time.time())
    conn.execute(
        "INSERT INTO skill_usage (name, created_at, created_by_origin, last_used_at, "
        "use_count, foreground_use_count, wrong_count, state, tier) "
        "VALUES (?,?,?,?,?,?,?, 'active', ?)",
        (name, now - 40 * 86400, origin, now - last_used_ago, uses, uses, wrong, tier),
    )


def _install(root, name):
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Use when x.\n---\n\nBody\n")


def _use(conn, name, session, at):
    conn.execute(
        "INSERT INTO skill_use_events (skill_name, source, session_id, signal, created_at) "
        "VALUES (?, 'codex', ?, 'open', ?)",
        (name, session, at),
    )


def _user_turn(conn, session, at, text):
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, session_id, role, content, created_at) "
        "VALUES (?, 'codex', ?, 'user', ?, ?)",
        (f"{session}-{at}", session, text, at),
    )


def test_uses_followed_by_a_correction_count_as_harmful(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    from threadkeeper.skill_outcomes import classify_pending_outcomes, contribution

    conn = pkg["db"].get_db()
    _skill_row(conn, "pr-review", uses=4)
    now = int(time.time()) - 7200
    _use(conn, "pr-review", "s-bad", now)
    _user_turn(conn, "s-bad", now + 30, "нет, это не то — я же просил без моков")
    _use(conn, "pr-review", "s-good", now)
    _user_turn(conn, "s-good", now + 30, "great, merge it")
    conn.commit()
    assert classify_pending_outcomes(conn) == 2
    outcomes = dict(conn.execute("SELECT session_id, outcome FROM skill_use_events").fetchall())
    assert outcomes == {"s-bad": "corrected", "s-good": "helped"}
    value = contribution(conn, "pr-review")
    assert (value.helpful, value.harmful) == (3, 1)
    assert classify_pending_outcomes(conn) == 0


def test_fresh_uses_wait_for_the_next_turns(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    from threadkeeper.skill_outcomes import classify_pending_outcomes

    conn = pkg["db"].get_db()
    _use(conn, "pr-review", "s1", int(time.time()) - 60)
    conn.commit()
    assert classify_pending_outcomes(conn) == 0


def test_skill_list_shows_helpful_and_harmful(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _skill_row(conn, "pr-review", uses=5, wrong=2)
    conn.commit()
    listing = _tool(pkg, "skill_list")()
    assert "helpful=3 harmful=2" in listing


def test_harmful_skill_is_retired_even_when_used_recently(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _skill_row(conn, "noisy-skill", uses=4, wrong=3, last_used_ago=60)
    _skill_row(conn, "busy-skill", uses=90, wrong=4, last_used_ago=60, origin="background_review")
    conn.commit()
    plan = _tool(pkg, "curator_run")(dry_run=True)
    assert "noisy-skill: active → stale" in plan and "reason=harmful" in plan
    assert "busy-skill" not in plan


def test_active_cap_stales_the_lowest_contribution_quiet_loop_skills(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    import threadkeeper.tools.skills as skills_mod

    monkeypatch.setattr(skills_mod, "SKILL_ACTIVE_CAP", 2)
    conn = pkg["db"].get_db()
    root = pkg["skills_root"]
    _skill_row(conn, "kept-busy", uses=20, last_used_ago=60)
    _skill_row(conn, "quiet-useful", uses=10, last_used_ago=10 * 86400)
    _skill_row(conn, "quiet-useless", uses=0, last_used_ago=10 * 86400)
    _skill_row(conn, "human-skill", origin="foreground", uses=0, last_used_ago=10 * 86400)
    for name in ("kept-busy", "quiet-useful", "quiet-useless", "human-skill"):
        _install(root, name)
    conn.commit()
    plan = _tool(pkg, "curator_run")(stale_after_days=60, archive_after_days=120, dry_run=True)
    lines = [line for line in plan.splitlines() if "over_cap" in line]
    assert len(lines) == 2
    assert any("quiet-useless" in line for line in lines)
    assert any("quiet-useful" in line for line in lines)
    assert "kept-busy" not in plan and "human-skill" not in plan


def test_audit_reports_quality_staleness_and_contribution(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    from threadkeeper.skill_audit import build_skill_audit

    root = pkg["skills_root"]
    (root / "noisy").mkdir()
    (root / "noisy" / "SKILL.md").write_text(
        "---\nname: noisy\ndescription: Use when x.\n---\n\n"
        "In T5a3 we learned this. See [[old-lesson]].\n"
        "Call mcp__thread-keeper__no_such_tool.\n"
    )
    conn = pkg["db"].get_db()
    _skill_row(conn, "noisy", uses=2, wrong=1)
    conn.commit()
    manifest = build_skill_audit(conn)
    record = next(item for item in manifest["skills"] if item["name"] == "noisy")
    for finding in ("quality:history_noise", "quality:wikilinks", "stale:unknown_tool:no_such_tool"):
        assert finding in record["findings"]
    assert record["quality"]["wikilinks"] == ["old-lesson"]
    assert (record["telemetry"]["helpful"], record["telemetry"]["harmful"]) == (1, 1)
    assert manifest["semantic_duplicates"] is None
