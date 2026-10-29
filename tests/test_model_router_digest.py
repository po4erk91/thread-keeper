"""model_router_digest — deterministic telemetry digest for the router."""
from __future__ import annotations

import json

NOW = 10_000_000
DAY = 86400
CURRENT = {
    "curator": {"cli": "codex", "model": "gpt-5.6-terra", "effort": "xhigh",
                "cli_source": "active CLI"},
}


def _task(conn, task_id, *, age_days, rc=0, ended=True, model="gpt-5.6-terra",
          effort="xhigh", duration=100, tokens=1000, role="curator",
          cost=None, prompt="p", session=None):
    started = NOW - int(age_days * DAY)
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at, ended_at, return_code, "
        "duration_s, tokens_total, role, chosen_cli, model, effort, cost_usd, spawned_cid) "
        "VALUES (?, 1, '/tmp', ?, ?, ?, ?, ?, ?, ?, 'codex', ?, ?, ?, ?)",
        (task_id, prompt, started, started + duration if ended else None,
         rc if ended else None, duration if ended else None, tokens, role,
         model, effort, cost, session),
    )
    conn.commit()


def _options():
    from threadkeeper import model_routes
    return model_routes.options_from_catalog({
        "codex": {"models": ["gpt-5.6-terra", "gpt-5.6-luna"],
                  "effort_options": ["low", "medium", "high", "xhigh"]},
    })


def _digest(conn, **kwargs):
    from threadkeeper.model_router_digest import build_digest
    return build_digest(
        conn, ["curator"], _options(), CURRENT, NOW,
        failure_reason=kwargs.get("failure_reason", lambda task_id: "ERROR: Selected model is at capacity. task tk_123"),
        outcome=kwargs.get("outcome", lambda task_id: f"Report written for {task_id}"),
    )


def test_percentile_nearest_rank():
    from threadkeeper.model_router_digest import percentile
    assert percentile([], 0.5) is None
    assert percentile([5, 1, 3], 0.5) == 3
    assert percentile(list(range(1, 11)), 0.9) == 9


def test_normalize_failure_groups_volatile_tokens():
    from threadkeeper.model_router_digest import normalize_failure
    assert normalize_failure("ERROR: capacity tk_0ec04c after 12 s") == (
        "ERROR: capacity <id> after <n> s")


def test_digest_buckets_weeks_and_counts_statuses(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _task(conn, "tk_a", age_days=1, duration=100)
    _task(conn, "tk_b", age_days=2, duration=300, rc=1)
    _task(conn, "tk_c", age_days=9, duration=50)
    _task(conn, "tk_d", age_days=0.1, ended=False)
    _task(conn, "tk_old", age_days=40)
    text = _digest(conn)

    assert "## ROLE curator" in text
    assert "current: codex / gpt-5.6-terra / xhigh (active CLI)" in text
    assert "codex / gpt-5.6-terra / xhigh" in text
    assert ("this week: runs=3 ok=1 failed=1 unknown=0 running=1 "
            "p50_s=100 p90_s=300 p50_tokens=1000") in text
    assert "1 week(s) ago: runs=1 ok=1" in text
    assert "ERROR: Selected model is at capacity. task <id> ×1" in text
    assert "tk_old" not in text


def test_digest_shows_na_tokens_outcomes_and_options(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _task(conn, "tk_a", age_days=1, tokens=None)
    text = _digest(conn)
    assert "p50_tokens=n/a" in text
    assert "Report written for tk_a" in text
    assert "codex / gpt-5.6-luna: low, medium, high, xhigh" in text


def test_digest_lists_recent_decisions(fresh_mp):
    from threadkeeper import model_routes
    conn = fresh_mp["db"].get_db()
    model_routes.insert_routes(conn, [{"role": "curator", "cli": "codex",
        "model": "gpt-5.6-luna", "effort": "high", "reason": "cheaper"}], "mr-1", NOW - DAY)
    conn.commit()
    text = _digest(conn)
    assert "recent decisions:" in text
    assert "codex gpt-5.6-luna high — cheaper" in text


def test_large_past_rationales_leave_room_for_actual_task_results(fresh_mp):
    from threadkeeper import model_routes
    conn = fresh_mp["db"].get_db()
    reason = "Earlier comparison: " + "unverified detail " * 800 + "Quality still uncertain."
    for i in range(3):
        model_routes.insert_routes(conn, [{"role": "curator", "cli": "codex",
            "model": "gpt-5.6-luna", "effort": "high", "reason": reason}], f"mr-{i}", NOW - i)
    _task(conn, "tk_result", age_days=1)
    text = _digest(conn, outcome=lambda _: "Actual result: integration test still fails.")
    assert "Actual result: integration test still fails." in text
    assert "Earlier comparison:" in text and "Quality still uncertain." in text
    assert len(text) < 5000


def test_digest_role_without_runs(fresh_mp):
    conn = fresh_mp["db"].get_db()
    assert "(no runs in the last 30 days)" in _digest(conn)


def test_digest_trims_to_cap(fresh_mp, monkeypatch):
    from threadkeeper import model_router_digest
    conn = fresh_mp["db"].get_db()
    for index in range(6):
        _task(conn, f"tk_{index}", age_days=index * 7 + 1)
    monkeypatch.setattr(model_router_digest, "MAX_DIGEST_CHARS", 1500)
    text = _digest(conn, outcome=lambda task_id: "x" * 3000)
    assert len(text) <= 1500
    assert "x" * 2000 not in text
    assert "result_source=log:" in text


def _dialog(conn, session, text, *, role="assistant", deleted=0, at=None):
    conn.execute(
        "INSERT INTO dialog_messages "
        "(uuid, source, session_id, role, content, created_at, deleted) "
        "VALUES (?, 'codex', ?, ?, ?, ?, ?)",
        (f"{session}-{role}-{deleted}-{at}", session, role, text,
         at if at is not None else NOW - DAY + 50, deleted),
    )
    conn.commit()


def test_digest_compares_recorded_costs_without_treating_missing_cost_as_zero(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _task(conn, "tk_priced_a", age_days=1, cost=0.04)
    _task(conn, "tk_priced_b", age_days=2, cost=0.06)
    _task(conn, "tk_unpriced", age_days=3)
    text = _digest(conn)
    assert "cost_known_runs=2/3 p50_cost_usd=0.04 recorded_cost_usd=0.1" in text
    assert "cost_usd=n/a" in text


def test_digest_distinguishes_zero_recorded_cost_from_unknown_cost(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _task(conn, "tk_free", age_days=1, cost=0)
    _task(conn, "tk_unknown", age_days=1, model="gpt-5.6-luna")
    text = _digest(conn)
    assert "cost_known_runs=1/1 p50_cost_usd=0.0 recorded_cost_usd=0.0" in text
    assert "cost_known_runs=0/1 p50_cost_usd=n/a recorded_cost_usd=n/a" in text


def test_digest_prices_effort_from_input_output_coverage_and_reported_model(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _task(conn, "tk_tokens", age_days=1, session="priced-session")
    conn.execute("UPDATE tasks SET tokens_in=900, tokens_out=100 WHERE id='tk_tokens'")
    _dialog(conn, "priced-session", "Useful verified result")
    conn.execute("UPDATE dialog_messages SET model='gpt-5.6-terra' WHERE session_id='priced-session'")
    _task(conn, "tk_unknown_tokens", age_days=1, tokens=None)
    text = _digest(conn)
    assert "input_known_runs=1/2 p50_input_tokens=900" in text
    assert "output_known_runs=1/2 p50_output_tokens=100" in text
    assert "input_tokens=900 output_tokens=100" in text
    assert "reported_model=gpt-5.6-terra" in text
    assert "input_tokens=n/a output_tokens=n/a" in text


def test_digest_respects_budget_reserved_for_official_prices(fresh_mp):
    from threadkeeper.model_router_digest import build_digest
    conn = fresh_mp["db"].get_db()
    for i in range(10):
        _task(conn, f"tk_budget_{i}", age_days=1)
    text = build_digest(conn, ["curator"], _options(), CURRENT, NOW,
                        failure_reason=lambda _: "", outcome=lambda _: "x" * 3000,
                        max_chars=2200)
    assert len(text) <= 2200
    assert "previous task results" in text


def test_later_completed_router_execution_supersedes_old_startup_failures(fresh_mp):
    from threadkeeper import model_routes
    conn = fresh_mp["db"].get_db()
    _task(conn, "tk_old_reject", age_days=2, rc=1, model="gpt-6.1-sol")
    _task(conn, "tk_verified", age_days=.1, model="gpt-6.1-sol", role="model_router")
    _task(conn, "tk_unverified", age_days=.1, model="gpt-6-luna", role="model_router")
    router_pass = model_routes.open_pass(conn, ["curator"], NOW - DAY)
    model_routes.set_attempt(conn, router_pass.pass_id, "fallback", "tk_verified")
    model_routes.finish_pass(conn, router_pass.pass_id, "complete", NOW)
    conn.commit()
    text = _digest(conn)
    access = text.split("## ROLE curator", 1)[0]
    assert "codex / gpt-6.1-sol / xhigh: last_success_unix=" in access
    assert "gpt-6-luna" not in access
    assert "not quality on other roles" in access
    assert "not an unresolved access blocker" in access


def test_router_quota_failures_are_visible_without_becoming_quality_evidence(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _task(conn, "tk_quota", age_days=.1, model="opus", role="model_router", rc=1)
    text = _digest(conn, failure_reason=lambda _: "usage_limit_reached; resets 1:10am")
    assert "## RECENT ROUTER EXECUTION FAILURES" in text
    assert "usage_limit_reached; resets 1:10am" in text
    assert "Separate temporary quota/transport state from model quality" in text


def test_structured_quota_error_preserves_cause_instead_of_json_prefix(fresh_mp, monkeypatch):
    from threadkeeper import agent_status
    from threadkeeper.model_router_digest import _default_failure_reason
    event = {"type": "result", "usage": {"details": "x" * 1000}, "is_error": True,
             "api_error": "usage_limit_reached", "result": "Session limit; resets 1:10am"}
    monkeypatch.setattr(agent_status, "_read_log_sample", lambda _: json.dumps(event))
    assert _default_failure_reason("tk_quota") == "usage_limit_reached: Session limit; resets 1:10am"


def test_digest_uses_saved_final_answer_with_task_context_when_log_is_missing(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _task(conn, "tk_saved", age_days=1, session="session-saved",
          prompt="Fix the import failure and verify the integration test.")
    result = "Implemented the import repair. " + "Details. " * 40 + "Integration test passed."
    _dialog(conn, "session-saved", result)
    text = _digest(conn, outcome=lambda task_id: "wrong log fragment")
    assert "Fix the import failure and verify the integration test." in text
    assert "result_source=transcript:" in text
    assert "Integration test passed." in text
    assert "wrong log fragment" not in text


def test_digest_excludes_deleted_and_user_messages_from_saved_results(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _task(conn, "tk_deleted", age_days=1, session="session-deleted")
    _dialog(conn, "session-deleted", "Deleted claim of success", deleted=1)
    _dialog(conn, "session-deleted", "User instruction is not a result", role="user")
    text = _digest(conn, outcome=lambda task_id: "Actual log result")
    assert "result_source=log: Actual log result" in text
    assert "Deleted claim of success" not in text
    assert "User instruction is not a result" not in text


def test_digest_does_not_attribute_a_later_session_reply_to_an_earlier_task(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _task(conn, "tk_resumed", age_days=1, session="session-resumed")
    _dialog(conn, "session-resumed", "Original result")
    _dialog(conn, "session-resumed", "Later unrelated task result", at=NOW)
    text = _digest(conn)
    assert "result_source=transcript: Original result" in text
    assert "Later unrelated task result" not in text


def test_digest_redacts_task_and_result_secrets_before_clipping(fresh_mp):
    conn = fresh_mp["db"].get_db()
    secret = "ghp_" + "a" * 30
    _task(conn, "tk_private", age_days=1, prompt=f"Check token {secret}",
          session="session-private")
    _dialog(conn, "session-private", f"Verified token {secret} without publishing it.")
    text = _digest(conn)
    assert (secret in text) is False
    assert "[REDACTED_SECRET]" in text


def test_evidence_clipping_preserves_result_conclusion(fresh_mp):
    from threadkeeper.model_router_digest import _evidence_text
    text = "Task failed: " + "middle detail " * 100 + "Tests still fail."
    clipped = _evidence_text(text, 100)
    assert len(clipped) == 100
    assert clipped.startswith("Task failed:")
    assert clipped.endswith("Tests still fail.")


def test_digest_samples_failed_and_older_alternative_model_results(fresh_mp):
    conn = fresh_mp["db"].get_db()
    for index in range(12):
        _task(conn, f"tk_current_{index}", age_days=index / 100 + 1)
    _task(conn, "tk_alternative", age_days=20, model="gpt-5.6-luna", rc=1,
          prompt="Validate the proposed lesson.")
    _task(conn, "tk_running", age_days=0, ended=False)
    text = _digest(conn, outcome=lambda task_id: "Rejected valid lesson" if task_id == "tk_alternative" else "Verified result")
    assert "task=tk_alternative" in text
    assert "execution=failed" in text
    assert "Rejected valid lesson" in text
    assert "task=tk_running" not in text


def test_digest_marks_missing_results_as_unknown_quality(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _task(conn, "tk_no_result", age_days=1)
    text = _digest(conn, outcome=lambda task_id: "")
    assert "execution=ok" in text
    assert "result_source=missing: (result unavailable; quality unknown)" in text


def test_digest_builds_from_task_history_without_writing_to_the_database(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _task(conn, "tk_readonly", age_days=1, session="session-readonly")
    _dialog(conn, "session-readonly", "Verified the repaired import.")
    conn.execute("PRAGMA query_only = ON")
    try:
        text = _digest(conn)
    finally:
        conn.execute("PRAGMA query_only = OFF")
    assert "result_source=transcript: Verified the repaired import." in text


def test_log_result_reader_extracts_final_codex_text_not_tool_output(fresh_mp, monkeypatch):
    from threadkeeper import agent_status
    from threadkeeper.model_router_digest import _default_outcome
    events = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Fixed import; integration passed."}},
        {"type": "item.completed", "item": {"type": "command_execution", "text": "Unrelated tool output"}},
    ]
    monkeypatch.setattr(agent_status, "_read_log_sample",
                        lambda task_id: "\n".join(json.dumps(e) for e in events))
    assert _default_outcome("tk_codex") == "Fixed import; integration passed."


def test_log_result_reader_extracts_claude_result_and_ignores_invalid_events(fresh_mp, monkeypatch):
    from threadkeeper import agent_status
    from threadkeeper.model_router_digest import _default_outcome
    sample = '\n'.join([
        json.dumps({"type": "result", "is_error": False, "result": "Checked candidates; rejected duplicate."}),
        '[]', '{"type":"result","is_error":true,"result":"API error"}', 'partial json',
    ])
    monkeypatch.setattr(agent_status, "_read_log_sample", lambda task_id: sample)
    assert _default_outcome("tk_claude") == "Checked candidates; rejected duplicate."
