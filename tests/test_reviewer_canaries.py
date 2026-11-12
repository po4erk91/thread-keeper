"""The candidate reviewer is audited with known-bad canary candidates."""
from __future__ import annotations

import json
import time


def _spawned_task(conn, task_id, child, ended=True):
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at, ended_at, spawned_cid) "
        "VALUES (?, 1, '/', 'x', 1, ?, ?)",
        (task_id, int(time.time()) if ended else None, child),
    )


def _proposal(conn, child, arguments):
    conn.execute(
        "INSERT INTO memory_proposals (child_cid, tool_name, arguments, memory_scope, created_at) "
        "VALUES (?, 'skill_manage', ?, 'unresolved', 1)",
        (child, json.dumps(arguments)),
    )


def test_canaries_rotate_and_look_like_inventory_lines(fresh_mp):
    from threadkeeper import db, reviewer_canaries as rc

    first, second = rc.canaries_for_pass(0), rc.canaries_for_pass(1)
    assert len(first) == 2 and {c.marker for c in first}.isdisjoint({c.marker for c in second})
    lines = rc.inventory_lines(db.get_db(), first)
    assert lines[0].startswith(f"  #{rc.CANARY_ID_OFFSET + 1} kind=insight session=canary-")
    assert first[0].content in lines[0]


def test_canary_due_every_nth_spawned_pass(fresh_mp):
    from threadkeeper import db, reviewer_canaries as rc

    conn = db.get_db()
    assert rc.canary_due(conn, 0) == (False, 0)
    assert rc.canary_due(conn, 3) == (True, 0)
    for _ in range(2):
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES ('', 'candidate_review_pass', '1', 'spawned pending=3', 1)"
        )
    conn.commit()
    assert rc.canary_due(conn, 3)[0] is False


def test_leaked_canary_fails_rejects_proposal_and_pauses_after_two(fresh_mp):
    from threadkeeper import db, reviewer_canaries as rc

    conn = db.get_db()
    canaries = rc.canaries_for_pass(0)
    for index in (1, 2):
        _spawned_task(conn, f"t{index}", f"child-{index}")
        _proposal(conn, f"child-{index}", {"action": "create", "name": "x",
                                          "content": f"rule from {canaries[0].marker}"})
        conn.commit()
        rc.record_canary_pass(conn, f"t{index}", canaries)
    assert rc.evaluate_finished(conn) == ["fail", "fail"]
    statuses = {row[0] for row in conn.execute("SELECT status FROM memory_proposals")}
    assert statuses == {"rejected"}
    assert rc.reviewer_paused(conn)
    assert rc.evaluate_finished(conn) == []


def test_clean_reviewer_passes_and_running_child_waits(fresh_mp):
    from threadkeeper import db, reviewer_canaries as rc

    conn = db.get_db()
    _spawned_task(conn, "t1", "child-1")
    _proposal(conn, "child-1", {"action": "create", "name": "x", "content": "a real rule"})
    _spawned_task(conn, "t2", "child-2", ended=False)
    conn.commit()
    rc.record_canary_pass(conn, "t1", rc.canaries_for_pass(0))
    rc.record_canary_pass(conn, "t2", rc.canaries_for_pass(1))
    assert rc.evaluate_finished(conn) == ["pass"]
    assert not rc.reviewer_paused(conn)


def test_review_pass_injects_canaries_and_pauses_when_lenient(fresh_mp, monkeypatch):
    from threadkeeper import candidate_reviewer as cr, db, reviewer_canaries as rc

    conn = db.get_db()
    for index in range(3):
        conn.execute(
            "INSERT INTO extract_candidates (kind, content, rationale, status, created_at, memory_scope) "
            "VALUES ('insight', ?, 'r', 'pending', ?, 'global')",
            (f"candidate {index}", int(time.time())),
        )
    conn.commit()
    prompts = []

    def fake_spawn(prompt, **kwargs):
        prompts.append(prompt)
        return "spawned task_id=tk_canary1"

    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", fake_spawn)
    monkeypatch.setattr(cr, "CANDIDATE_REVIEW_CANARY_EVERY", 1)
    monkeypatch.setattr(cr, "CANDIDATE_REVIEW_MIN", 1)
    assert cr.run_review_pass(force=True) == "spawned task_id=tk_canary1"
    assert any(c.content in prompts[0] for c in rc.CANARIES)
    marker = conn.execute(
        "SELECT target FROM events WHERE kind=?", (rc.CANARY_KIND,),
    ).fetchone()[0]
    assert marker == "tk_canary1"

    monkeypatch.setattr(rc, "reviewer_paused", lambda conn: True)
    monkeypatch.setattr(cr, "CANDIDATE_REVIEW_INTERVAL_S", 60)
    monkeypatch.setattr(cr, "_pass_due", lambda conn, now: True)
    monkeypatch.setattr(cr.daemon_state, "claim_pass", lambda *a, **k: True)
    assert cr.run_review_pass().startswith("paused_lenient_reviewer")
    assert cr.run_review_pass(force=True) == "spawned task_id=tk_canary1"
