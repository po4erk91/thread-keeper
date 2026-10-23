"""model_route_write — the router child's only write path."""
from __future__ import annotations

import json

CATALOG = {
    "claude": {"models": ["opus", "sonnet"]},
    "codex": {"models": ["gpt-5.6-luna", "gpt-5.6-terra"],
              "effort_options": ["low", "medium", "high", "xhigh"]},
}
DECISIONS = [
    {"role": "curator", "cli": "codex", "model": "gpt-5.6-luna", "effort": "high",
     "reason": "luna matches terra outcomes at lower cost"},
    {"role": "shadow_observer", "cli": "claude", "model": "sonnet", "effort": "high",
     "reason": "terra hit capacity errors this week"},
]


def _setup(pkg, monkeypatch, *, origin="model_router", child=True, env_pass=None):
    from threadkeeper import model_routes
    conn = pkg["db"].get_db()
    router_pass = model_routes.open_pass(conn, ["curator", "shadow_observer"], 1000)
    monkeypatch.setattr(pkg["config"], "SPAWNED_CHILD", child)
    monkeypatch.setattr(pkg["config"], "WRITE_ORIGIN", origin)
    monkeypatch.setenv(model_routes.PASS_ID_ENV, env_pass or router_pass.pass_id)
    monkeypatch.setattr(model_routes, "cached_options",
                        lambda: model_routes.options_from_catalog(CATALOG))
    return conn, router_pass


def _write(pass_id, decisions):
    from threadkeeper.tools.model_router import model_route_write
    return model_route_write(pass_id=pass_id, decisions=decisions)


def test_write_records_routes_completes_pass_and_emits_event(fresh_mp, monkeypatch):
    from threadkeeper import model_routes
    conn, router_pass = _setup(fresh_mp, monkeypatch)
    result = _write(router_pass.pass_id, DECISIONS)

    assert result.startswith("ok routed ")
    assert model_routes.latest_route(conn, "curator").model == "gpt-5.6-luna"
    assert model_routes.get_pass(conn, router_pass.pass_id).status == "complete"
    event = conn.execute(
        "SELECT target, summary FROM events WHERE kind='model_router_pass' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert event["target"] == router_pass.pass_id
    assert "curator:" in event["summary"]


def test_write_accepts_json_string_decisions(fresh_mp, monkeypatch):
    from threadkeeper import model_routes
    conn, router_pass = _setup(fresh_mp, monkeypatch)
    assert _write(router_pass.pass_id, json.dumps(DECISIONS)).startswith("ok routed ")
    assert model_routes.latest_route(conn, "shadow_observer").cli == "claude"


def test_replay_after_completion_is_a_no_op(fresh_mp, monkeypatch):
    conn, router_pass = _setup(fresh_mp, monkeypatch)
    _write(router_pass.pass_id, DECISIONS)
    assert _write(router_pass.pass_id, DECISIONS) == "ok already_recorded"
    count = conn.execute("SELECT COUNT(*) FROM model_routes").fetchone()[0]
    assert count == 2


def test_write_rejects_unauthorized_callers(fresh_mp, monkeypatch):
    _, router_pass = _setup(fresh_mp, monkeypatch, origin="curator")
    assert _write(router_pass.pass_id, DECISIONS) == "ERR route_write_not_authorized"


def test_write_rejects_foreground_and_wrong_pass(fresh_mp, monkeypatch):
    _, router_pass = _setup(fresh_mp, monkeypatch, child=False)
    assert _write(router_pass.pass_id, DECISIONS) == "ERR route_write_not_authorized"
    monkeypatch.setattr(fresh_mp["config"], "SPAWNED_CHILD", True)
    assert _write("mr-other", DECISIONS) == "ERR route_write_not_authorized"


def test_invalid_decisions_write_nothing(fresh_mp, monkeypatch):
    from threadkeeper import model_routes
    conn, router_pass = _setup(fresh_mp, monkeypatch)
    bad = [dict(DECISIONS[0], effort="max"), dict(DECISIONS[1], model="gpt-9")]
    result = _write(router_pass.pass_id, bad)

    assert result.startswith("ERR invalid_decisions")
    assert "effort 'max' is not allowed for gpt-5.6-luna" in result
    assert "model 'gpt-9' is not offered by claude" in result
    assert conn.execute("SELECT COUNT(*) FROM model_routes").fetchone()[0] == 0
    assert model_routes.get_pass(conn, router_pass.pass_id).status == "running"


def test_unparseable_or_unknown_input(fresh_mp, monkeypatch):
    _, router_pass = _setup(fresh_mp, monkeypatch)
    assert _write(router_pass.pass_id, "not json") == "ERR decisions_must_be_a_list"
    assert _write(router_pass.pass_id, {"role": "curator"}) == "ERR decisions_must_be_a_list"
    monkeypatch.setenv("THREADKEEPER_MODEL_ROUTER_PASS_ID", "mr-missing")
    assert _write("mr-missing", DECISIONS) == "ERR unknown_pass"


def test_pass_id_reaches_child_mcp_env():
    from threadkeeper.adapters.base import CHILD_MCP_ENV_KEYS
    assert "THREADKEEPER_MODEL_ROUTER_PASS_ID" in CHILD_MCP_ENV_KEYS


def test_status_without_auto_roles(fresh_mp):
    from threadkeeper.tools.model_router import model_router_status
    text = model_router_status()
    assert "interval_s=0 fallback=codex/gpt-6.1-sol/xhigh" in text
    assert "last pass: none" in text
    assert "auto roles: (none)" in text
    assert "next pass: disabled" in text


def test_status_lists_routes_and_missing_decisions(fresh_mp, monkeypatch):
    from threadkeeper import model_routes
    from threadkeeper.tools.model_router import model_router_status
    conn = fresh_mp["db"].get_db()
    monkeypatch.setattr(fresh_mp["config"], "MODEL_ROUTER_INTERVAL_S", 604800.0)
    monkeypatch.setitem(fresh_mp["config"].settings.spawn.loop, "curator", "auto")
    monkeypatch.setitem(fresh_mp["config"].settings.spawn.loop, "probe_runner", "auto")
    monkeypatch.setattr(model_routes, "cached_options",
                        lambda: model_routes.options_from_catalog(CATALOG))
    router_pass = model_routes.open_pass(conn, ["curator", "probe_runner"], 1000)
    model_routes.insert_routes(conn, [DECISIONS[0]], router_pass.pass_id, 1000)
    conn.commit()

    text = model_router_status()
    assert f"last pass: {router_pass.pass_id} status=running attempt=primary" in text
    assert "auto roles: curator, probe_runner" in text
    assert "curator: codex gpt-5.6-luna high since" in text
    assert "luna matches terra outcomes at lower cost" in text
    assert "probe_runner: no decision yet — follows active CLI" in text
    assert "next pass: running (attempt primary)" in text


def test_run_tool_forces_a_tick(fresh_mp, monkeypatch):
    from threadkeeper import model_router
    from threadkeeper.tools.model_router import model_router_run
    seen = {}
    monkeypatch.setattr(model_router, "run_router_pass",
                        lambda force=False, scheduled=False: seen.setdefault("force", force) and "ok")
    assert model_router_run() == "ok"
    assert seen == {"force": True}


def _enable(pkg, monkeypatch, roles=("curator",)):
    from threadkeeper import model_routes
    monkeypatch.setattr(pkg["config"], "MODEL_ROUTER_INTERVAL_S", 604800.0)
    for role in roles:
        monkeypatch.setitem(pkg["config"].settings.spawn.loop, role, "auto")
    monkeypatch.setattr(model_routes, "cached_options",
                        lambda: model_routes.options_from_catalog(CATALOG))
    return pkg["db"].get_db()


def _next_pass_line(text):
    return next(line for line in text.splitlines() if line.startswith("next pass:"))


def test_status_next_pass_without_auto_roles(fresh_mp, monkeypatch):
    from threadkeeper.tools.model_router import model_router_status
    _enable(fresh_mp, monkeypatch, roles=())
    assert _next_pass_line(model_router_status()) == "next pass: none (no auto roles)"


def test_status_next_pass_due_now_without_decision(fresh_mp, monkeypatch):
    from threadkeeper.tools.model_router import model_router_status
    _enable(fresh_mp, monkeypatch)
    assert _next_pass_line(model_router_status()) == "next pass: due now"


def test_status_next_pass_after_completed_pass(fresh_mp, monkeypatch):
    import time
    from threadkeeper import model_routes
    from threadkeeper.tools.model_router import model_router_status
    conn = _enable(fresh_mp, monkeypatch)
    now = int(time.time())
    router_pass = model_routes.open_pass(conn, ["curator"], now - 86400)
    model_routes.insert_routes(conn, [DECISIONS[0]], router_pass.pass_id, now - 86400)
    model_routes.finish_pass(conn, router_pass.pass_id, "complete", now - 86400)
    conn.commit()
    assert _next_pass_line(model_router_status()) == "next pass: in 6d"


def test_status_next_pass_in_backoff(fresh_mp, monkeypatch):
    import time
    from threadkeeper import model_routes
    from threadkeeper.tools.model_router import model_router_status
    conn = _enable(fresh_mp, monkeypatch)
    now = int(time.time())
    router_pass = model_routes.open_pass(conn, ["curator"], now - 3600)
    model_routes.finish_pass(conn, router_pass.pass_id, "failed", now - 3600)
    conn.commit()
    assert _next_pass_line(model_router_status()) == "next pass: backoff, retry in 5h"

