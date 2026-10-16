"""Repeated lesson violations escalate to enforcement (#228)."""
from __future__ import annotations

import time
from pathlib import Path


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _seed_lesson(pkg, title="never force push shared branches"):
    from threadkeeper import lessons

    lessons.append_lesson(title=title, body="Use a new branch.", source="shadow")
    return lessons.iter_lessons().__next__()["slug"]


def test_violation_is_counted_once_per_conversation_per_day(fresh_mp):
    pkg = fresh_mp
    slug = _seed_lesson(pkg)
    record = _tool(pkg, "lesson_violation")

    assert record(slug, "user corrected a force push again").startswith(
        f"ok slug={slug} violations=1/3"
    )
    again = record(slug, "same conversation, same day")
    assert "violations=1/3" in again
    assert "already_recorded_today=1" in again
    assert record("no-such-lesson", "x").startswith("ERR unknown_lesson")
    assert record(slug, "   ") == "ERR empty_evidence"


def test_threshold_marks_the_lesson_memory_insufficient(fresh_mp):
    pkg = fresh_mp
    slug = _seed_lesson(pkg)
    conn = pkg["db"].get_db()
    now = int(time.time())
    for session in ("s-other-1", "s-other-2"):
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, 'lesson_violation', ?, 'origin=shadow evidence=x', ?)",
            (session, slug, now - 3600),
        )
    conn.commit()

    out = _tool(pkg, "lesson_violation")(slug, "third distinct conversation")

    assert "violations=3/3" in out
    assert "memory_insufficient=1 recommend=hook_enforcement" in out
    dashboard = _tool(pkg, "mp_dashboard")()
    assert "memory_insufficient_lessons=1" in dashboard
    assert f"{slug}  violations=3" in dashboard


def test_old_violations_fall_out_of_the_window(fresh_mp):
    pkg = fresh_mp
    slug = _seed_lesson(pkg)
    conn = pkg["db"].get_db()
    long_ago = int(time.time()) - 200 * 86400
    for session in ("a", "b", "c"):
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, 'lesson_violation', ?, 'evidence=x', ?)",
            (session, slug, long_ago),
        )
    conn.commit()

    from threadkeeper.lesson_violations import memory_insufficient

    assert memory_insufficient(conn) == {}


def test_curator_inventory_flags_memory_insufficient_lessons(fresh_mp):
    from threadkeeper import curator

    item = {"slug": "never-force-push", "body": "Use a new branch.", "ts": 1,
            "source": "shadow"}
    line = curator._format_lesson(item, {}, (), 3)
    assert "[MEMORY-INSUFFICIENT]" in line
    assert "violations=3" in line
    assert "[MEMORY-INSUFFICIENT]" not in curator._format_lesson(item, {}, (), 2)
    assert "HOOK_ESCALATION" in curator.CURATOR_PROMPT


def test_learning_loops_may_record_violations():
    root = Path(__file__).parents[1] / "threadkeeper"
    for module in ("shadow_review.py", "candidate_reviewer.py"):
        text = (root / module).read_text()
        assert '"mcp__thread-keeper__lesson_violation,"' in text, module
        assert "lesson_violation(slug=<existing>" in text, module
