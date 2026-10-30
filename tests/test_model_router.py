"""model_router — tick state machine, primary/fallback spawning, backoff."""
from __future__ import annotations

import os
import time
import pytest

CATALOG = {
    "claude": {"models": ["opus", "sonnet"]},
    "codex": {"models": ["gpt-5.6-luna", "gpt-6.1-sol"],
              "effort_options": ["low", "medium", "high", "xhigh"]},
}


def _setup(pkg, monkeypatch, auto_roles=("curator",)):
    from threadkeeper import model_pricing, model_router, model_routes
    monkeypatch.setattr(model_pricing, "pricing_context",
                        lambda options, now: "## OFFICIAL PRICING\nTest official price data")
    config = pkg["config"]
    monkeypatch.setattr(config, "MODEL_ROUTER_INTERVAL_S", 604800.0)
    for role in auto_roles:
        monkeypatch.setitem(config.settings.spawn.loop, role, "auto")
    monkeypatch.setattr(model_routes, "cached_options",
                        lambda: model_routes.options_from_catalog(CATALOG))
    from threadkeeper import model_catalog
    monkeypatch.setattr(model_catalog, "cli_catalog", lambda refresh=False: [])
    conn = pkg["db"].get_db()
    calls = []

    def fake_spawn(prompt, cli="", model="", effort=""):
        task_id = f"tk_{len(calls):06x}"
        calls.append({"prompt": prompt, "cli": cli, "model": model, "effort": effort,
                      "pass_env": os.environ.get(model_routes.PASS_ID_ENV)})
        conn.execute(
            "INSERT INTO tasks (id, pid, cwd, prompt, started_at, role) "
            "VALUES (?, ?, '/tmp', ?, ?, 'model_router')",
            (task_id, os.getpid(), prompt, int(time.time())),
        )
        conn.commit()
        return f"ok task={task_id} pid={os.getpid()}"

    monkeypatch.setattr(model_router, "_spawn_child", fake_spawn)
    return model_router, model_routes, conn, calls


def _exit(conn, task_id, rc):
    conn.execute("UPDATE tasks SET ended_at=?, return_code=? WHERE id=?",
                 (int(time.time()), rc, task_id))
    conn.commit()


def _record_valid_route(model_routes, conn, router_pass, decided_at=None):
    model_routes.insert_routes(conn, [{"role": "curator", "cli": "codex",
        "model": "gpt-5.6-luna", "effort": "high", "reason": "r"}],
        router_pass.pass_id, decided_at or int(time.time()))
    model_routes.finish_pass(conn, router_pass.pass_id, "complete",
                             decided_at or int(time.time()))
    conn.commit()


def test_disabled_without_force(fresh_mp):
    from threadkeeper import model_router
    assert model_router.run_router_pass() == "disabled"


def test_skip_without_auto_roles(fresh_mp, monkeypatch):
    router, _, _, calls = _setup(fresh_mp, monkeypatch, auto_roles=())
    assert router.run_router_pass() == "skip no_auto_roles"
    assert calls == []


def test_new_auto_role_starts_primary_pass(fresh_mp, monkeypatch):
    router, routes, conn, calls = _setup(fresh_mp, monkeypatch)
    result = router.run_router_pass()

    assert result.startswith("spawned attempt=primary pass=mr-")
    call = calls[0]
    assert (call["cli"], call["model"], call["effort"]) == ("", "", "")
    assert call["prompt"].startswith(router.ROUTER_PROMPT_PREFIX)
    assert "## ROLE curator" in call["prompt"]
    router_pass = routes.latest_pass(conn)
    assert call["pass_env"] == router_pass.pass_id
    assert os.environ.get(routes.PASS_ID_ENV) is None
    assert (router_pass.roles, router_pass.status, router_pass.task_id) == (
        ("curator",), "running", "tk_000000")


def test_running_child_blocks_new_spawn(fresh_mp, monkeypatch):
    router, _, _, calls = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    assert router.run_router_pass().startswith("running pass=mr-")
    assert len(calls) == 1


def test_failed_primary_falls_back_to_sol(fresh_mp, monkeypatch):
    router, routes, conn, calls = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    _exit(conn, "tk_000000", 1)
    result = router.run_router_pass()

    assert result.startswith("spawned attempt=fallback")
    assert (calls[1]["cli"], calls[1]["model"], calls[1]["effort"]) == (
        "codex", "gpt-6.1-sol", "xhigh")
    assert calls[1]["pass_env"] == calls[0]["pass_env"]
    assert routes.latest_pass(conn).attempt == "fallback"


def test_clean_exit_without_decision_also_falls_back(fresh_mp, monkeypatch):
    router, _, conn, _ = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    _exit(conn, "tk_000000", 0)
    assert router.run_router_pass().startswith("spawned attempt=fallback")


def test_recorded_pass_does_not_fall_back_after_crash(fresh_mp, monkeypatch):
    router, routes, conn, calls = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    _record_valid_route(routes, conn, routes.latest_pass(conn))
    _exit(conn, "tk_000000", 1)
    assert router.run_router_pass().startswith("idle next_due_in=")
    assert len(calls) == 1


def test_failed_fallback_marks_pass_and_backs_off(fresh_mp, monkeypatch):
    router, routes, conn, calls = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    _exit(conn, "tk_000000", 1)
    router.run_router_pass()
    _exit(conn, "tk_000001", 1)

    assert router.run_router_pass().startswith("spawn_failed pass=mr-")
    assert routes.latest_pass(conn).status == "failed"
    assert router.run_router_pass().startswith("backoff pass=mr-")
    assert len(calls) == 2

    conn.execute("UPDATE model_router_passes SET ended_at=?",
                 (int(time.time()) - router._BACKOFF_S - 1,))
    conn.commit()
    assert router.run_router_pass().startswith("spawned attempt=primary")
    assert len(calls) == 3


def test_spawn_error_on_primary_falls_back_next_tick(fresh_mp, monkeypatch):
    router, routes, conn, _ = _setup(fresh_mp, monkeypatch)
    monkeypatch.setattr(router, "_spawn_child",
                        lambda prompt, cli="", model="", effort="": "ERR budget_exceeded")
    assert router.run_router_pass().startswith("spawn_error attempt=primary")
    assert routes.latest_pass(conn).task_id == ""
    assert router.run_router_pass().startswith("spawn_error attempt=fallback")


def test_idle_until_interval_then_due(fresh_mp, monkeypatch):
    router, routes, conn, calls = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    router_pass = routes.latest_pass(conn)
    _record_valid_route(routes, conn, router_pass)
    _exit(conn, "tk_000000", 0)
    assert router.run_router_pass().startswith("idle next_due_in=")

    conn.execute("UPDATE model_router_passes SET ended_at=?",
                 (int(time.time()) - 604800 - 1,))
    conn.commit()
    assert router.run_router_pass().startswith("spawned attempt=primary")
    assert len(calls) == 2


def test_role_added_mid_pass_starts_next_pass(fresh_mp, monkeypatch):
    router, routes, conn, calls = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    monkeypatch.setitem(fresh_mp["config"].settings.spawn.loop, "probe_runner", "auto")
    assert router.run_router_pass().startswith("running pass=")

    _record_valid_route(routes, conn, routes.latest_pass(conn))
    _exit(conn, "tk_000000", 0)
    assert router.run_router_pass().startswith("spawned attempt=primary")
    assert routes.latest_pass(conn).roles == ("curator", "probe_runner")


def test_force_starts_even_when_idle(fresh_mp, monkeypatch):
    router, routes, conn, calls = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    _record_valid_route(routes, conn, routes.latest_pass(conn))
    _exit(conn, "tk_000000", 0)
    assert router.run_router_pass(force=True).startswith("spawned attempt=primary")


def test_state_changes_are_recorded_as_events(fresh_mp, monkeypatch):
    router, _, conn, _ = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    router.run_router_pass()
    kinds = conn.execute(
        "SELECT summary FROM events WHERE kind='model_router_pass' ORDER BY id"
    ).fetchall()
    assert [row["summary"].split()[0] for row in kinds] == ["spawned"]


def test_loop_is_registered(fresh_mp):
    from threadkeeper import agent_status, host
    from threadkeeper.notify import _LOOP_PASS_KINDS
    from threadkeeper.tools.dashboard import _LOOP_TASK_PREFIXES

    loop = next(d for d in agent_status._LOOP_DEFS if d["id"] == "model_router")
    assert (loop["event"], loop["role"], loop["interval"]) == (
        "model_router_pass", "model_router", "MODEL_ROUTER_INTERVAL_S")
    assert ("model_router", "start_model_router_daemon") in host._DAEMON_STARTERS
    assert "model_router_pass" in _LOOP_PASS_KINDS
    assert _LOOP_TASK_PREFIXES["model_router"] == "You are a MODEL ROUTER"


def test_empty_catalog_skips_without_opening_a_pass(fresh_mp, monkeypatch):
    from threadkeeper import model_catalog
    router, routes, conn, calls = _setup(fresh_mp, monkeypatch)
    refreshed = []
    monkeypatch.setattr(model_catalog, "cli_catalog",
                        lambda refresh=False: refreshed.append(refresh) or [])
    monkeypatch.setattr(routes, "cached_options", lambda: {})

    assert router.run_router_pass() == "skip catalog_unavailable"
    assert refreshed == [False]
    assert calls == []
    assert routes.latest_pass(conn) is None


def test_start_refreshes_catalog_before_building_the_prompt(fresh_mp, monkeypatch):
    from threadkeeper import model_catalog
    router, _, _, calls = _setup(fresh_mp, monkeypatch)
    refreshed = []
    monkeypatch.setattr(model_catalog, "cli_catalog",
                        lambda refresh=False: refreshed.append(refresh) or [])

    assert router.run_router_pass().startswith("spawned attempt=primary")
    assert refreshed == [False]
    assert "codex / gpt-6.1-sol: low, medium, high, xhigh" in calls[0]["prompt"]


def test_router_child_may_only_call_route_write(fresh_mp, monkeypatch):
    from threadkeeper import model_router
    import threadkeeper.tools.spawn as spawn_mod
    seen = {}
    monkeypatch.setattr(spawn_mod, "_spawn_impl",
                        lambda **kwargs: seen.update(kwargs) or "ok task=tk_x pid=1")

    model_router._spawn_child("prompt", cli="codex", model="gpt-6.1-sol", effort="xhigh")
    assert seen["allowed_tools_override"] == (model_router.ROUTER_TOOL,)
    assert seen.get("extra_allowed_tools", "") == ""
    assert (seen["role"], seen["write_origin"]) == ("model_router", "model_router")
    assert (seen["cli"], seen["model"], seen["effort"]) == ("codex", "gpt-6.1-sol", "xhigh")


def test_router_does_not_spawn_when_parent_cannot_expose_route_write(fresh_mp, monkeypatch):
    from threadkeeper import _mcp, model_router
    import threadkeeper.tools.spawn as spawn_mod
    calls = []
    monkeypatch.setattr(_mcp, "active_tool_names", lambda: frozenset({"brief"}))
    monkeypatch.setattr(spawn_mod, "_spawn_impl", lambda **kwargs: calls.append(kwargs))
    assert model_router._spawn_child("prompt") == "ERR model_router_write_tool_unavailable"
    assert calls == []


@pytest.mark.parametrize("cli", ["", "claude"])
def test_claude_router_cannot_expand_into_shell_or_skill_research(fresh_mp, monkeypatch, cli):
    from threadkeeper import model_router
    import threadkeeper.tools.spawn as spawn_mod
    seen = {}
    monkeypatch.setattr(spawn_mod, "_spawn_impl",
                        lambda **kwargs: seen.update(kwargs) or "ok task=tk_x pid=1")
    model_router._spawn_child("prompt", cli=cli, model="opus", effort="max")
    assert seen["native_tools_override"] == ("ToolSearch",)
    assert seen["allowed_tools_override"] == (model_router.ROUTER_TOOL,)


def test_router_prompt_requires_task_result_cost_and_efficiency_comparison(fresh_mp, monkeypatch):
    router, _, _, calls = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    prompt = calls[0]["prompt"]
    assert "Task effectiveness:" in prompt
    assert "Cost:" in prompt
    assert "Efficiency:" in prompt
    assert "Reassess the current setting against alternatives on every pass" in prompt
    assert "Exit code 0 means the process completed, not that the" in prompt
    assert "including for roles that write memory or code" in prompt
    assert "Switch only" not in prompt
    assert "fewer than 5 runs" not in prompt
    assert "Test official price data" in prompt
    assert "Stale cards are" in prompt
    assert "Effort has no invented fixed price" in prompt
    assert "API USD rates are reference costs, not subscription charges" in prompt


def test_pending_pass_reconciles_before_the_hourly_interval(fresh_mp, monkeypatch):
    router, routes, conn, calls = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    primary = routes.latest_pass(conn).task_id
    _exit(conn, primary, 1)
    intervals = []
    monkeypatch.setattr(router.daemon_state, "claim_pass",
                        lambda name, interval, **kwargs: intervals.append(interval) or True)
    assert router._poll_interval(conn) == 30
    assert router.run_router_pass(scheduled=True).startswith("spawned attempt=fallback")
    assert intervals == [30]
    _record_valid_route(routes, conn, routes.latest_pass(conn))
    assert router._poll_interval(conn) == 3600
