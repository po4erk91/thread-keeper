"""model_router_digest — deterministic telemetry digest for the router."""
from __future__ import annotations

NOW = 10_000_000
DAY = 86400
CURRENT = {
    "curator": {"cli": "codex", "model": "gpt-5.6-terra", "effort": "xhigh",
                "cli_source": "active CLI"},
}


def _task(conn, task_id, *, age_days, rc=0, ended=True, model="gpt-5.6-terra",
          effort="xhigh", duration=100, tokens=1000, role="curator"):
    started = NOW - int(age_days * DAY)
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at, ended_at, return_code, "
        "duration_s, tokens_total, role, chosen_cli, model, effort) "
        "VALUES (?, 1, '/tmp', 'p', ?, ?, ?, ?, ?, ?, 'codex', ?, ?)",
        (task_id, started, started + duration if ended else None, rc if ended else None,
         duration if ended else None, tokens, role, model, effort),
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


def test_digest_role_without_runs(fresh_mp):
    conn = fresh_mp["db"].get_db()
    assert "(no runs in the last 30 days)" in _digest(conn)


def test_digest_trims_to_cap(fresh_mp, monkeypatch):
    from threadkeeper import model_router_digest
    conn = fresh_mp["db"].get_db()
    for index in range(6):
        _task(conn, f"tk_{index}", age_days=index * 7 + 1)
    monkeypatch.setattr(model_router_digest, "MAX_DIGEST_CHARS", 900)
    text = _digest(conn, outcome=lambda task_id: "x" * 240)
    assert len(text) <= 900
    assert "x" * 240 not in text
