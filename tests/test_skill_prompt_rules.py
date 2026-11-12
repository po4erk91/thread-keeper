"""Every learning-loop writer gets the same skill-writing rules and catalog."""
from __future__ import annotations


def test_every_loop_prompt_carries_the_skill_writing_rules(fresh_mp):
    from threadkeeper.candidate_reviewer import CANDIDATE_REVIEW_PROMPT
    from threadkeeper.review_prompts import (
        COMBINED_REVIEW_PROMPT, SKILL_REVIEW_PROMPT, SKILL_WRITING_RULES,
    )
    from threadkeeper.shadow_review import SHADOW_REVIEW_PROMPT

    for prompt in (CANDIDATE_REVIEW_PROMPT, SHADOW_REVIEW_PROMPT,
                   SKILL_REVIEW_PROMPT, COMBINED_REVIEW_PROMPT):
        assert SKILL_WRITING_RULES in prompt
    for rule in ("PATCH ONLY THE SAME TASK", "PATCH ONLY ON EVIDENCE",
                 "CREATE ONLY ON REPETITION", "GENERALIZE BEFORE WRITING",
                 "## Not for (required)", "NO WIKILINKS", "PROJECT SCOPE",
                 "CHECK THE CATALOG", "SKIP WHAT THE MODEL ALREADY KNOWS"):
        assert rule in SKILL_WRITING_RULES
    assert "patch beats create" not in SKILL_REVIEW_PROMPT


def test_candidate_inventory_shows_full_session_and_day(fresh_mp):
    from threadkeeper.candidate_reviewer import _format_candidate

    line = _format_candidate({
        "id": 7, "kind": "insight", "source_cid": "e8bc5f56-1111-2222-3333-444455556666",
        "rationale": "pattern", "content": "a rule", "created_at": 1_794_000_000,
    })
    assert "session=e8bc5f56-1111-2222-3333-444455556666 day=2026-11-06" in line


def test_shadow_window_headers_carry_session_and_day(fresh_mp):
    from threadkeeper import db, shadow_review

    conn = db.get_db()
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, session_id, role, content, created_at, memory_scope) "
        "VALUES ('u1', 'claude_code', 'abcdef123456789', 'user', ?, 1794000000, 'global')",
        ("always rerun the failing check before blaming the code " * 3,),
    )
    conn.commit()
    dump, *_ = shadow_review._collect_window(conn, 0, 10**10)
    assert "[user @session=abcdef123456 day=2026-11-06]" in dump


def test_related_sessions_block(fresh_mp, monkeypatch):
    from threadkeeper import db, embeddings, skill_recurrence

    conn = db.get_db()
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, session_id, role, content, created_at) "
        "VALUES ('u1', 'claude_code', 's1', 'user', 'x', 1)"
    )
    conn.commit()
    hits = [
        {"session_id": "s1", "score": 0.9, "created_at": 1_794_000_000},
        {"session_id": "s1", "score": 0.8, "created_at": 1_794_000_000},
        {"session_id": "s2", "score": 0.7, "created_at": 1_794_100_000},
        {"session_id": "s3", "score": 0.3, "created_at": 1_794_100_000},
    ]
    monkeypatch.setattr(embeddings, "_dialog_cosine_search", lambda *a, **k: hits)
    block = skill_recurrence.related_sessions_block(conn, "review a PR")
    assert "session=s1 day=2026-11-06 score=0.90" in block
    assert "session=s2" in block
    assert "s3" not in block
    monkeypatch.setattr(embeddings, "_dialog_cosine_search", lambda *a, **k: hits[:1])
    assert "single episode" in skill_recurrence.related_sessions_block(conn, "review a PR")
