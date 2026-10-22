"""model_routes — route/pass storage, catalog options, decision validation."""
from __future__ import annotations

CATALOG = {
    "claude": {"models": ["opus", "sonnet"], "effort_options": None, "model_efforts": {}},
    "codex": {
        "models": ["gpt-6.1-sol", "gpt-5.6-luna"],
        "effort_options": ["low", "medium", "high", "xhigh"],
        "model_efforts": {"gpt-5.6-luna": ["low", "medium", "high", "xhigh", "max"]},
    },
    "antigravity": {
        "models": ["gemini-3.8-flash-high\tGemini 3.8 Flash (High)"],
        "effort_options": [],
        "model_efforts": {},
    },
}


def _decision(role="curator", cli="codex", model="gpt-5.6-luna", effort="high",
              reason="cheaper with equal outcomes"):
    return {"role": role, "cli": cli, "model": model, "effort": effort, "reason": reason}


def _options():
    from threadkeeper import model_routes
    return model_routes.options_from_catalog(CATALOG)


def test_options_strip_labels_and_fall_back_to_cli_efforts():
    options = _options()
    assert options["antigravity"].models == ("gemini-3.8-flash-high",)
    assert options["antigravity"].effort_mode == "model"
    assert options["claude"].efforts_for("opus") == ("low", "medium", "high", "xhigh", "max")
    assert options["codex"].efforts_for("gpt-5.6-luna")[-1] == "max"
    assert options["codex"].efforts_for("gpt-6.1-sol")[-1] == "xhigh"


def test_route_problem_cases():
    from threadkeeper.model_routes import route_problem
    options = _options()
    assert route_problem("codex", "gpt-6.1-sol", "xhigh", options) == ""
    assert "not in the model catalog" in route_problem("copilot", "x", "high", options)
    assert "not offered" in route_problem("codex", "gpt-9", "high", options)
    assert "not allowed" in route_problem("codex", "gpt-6.1-sol", "max", options)
    assert "must be empty" in route_problem("antigravity", "gemini-3.8-flash-high", "high", options)
    assert route_problem("antigravity", "gemini-3.8-flash-high", "", options) == ""


def test_valid_decision_set_has_no_errors():
    from threadkeeper.model_routes import decision_errors
    decisions = [_decision(), _decision(role="shadow_observer", cli="claude",
                                        model="sonnet", effort="high")]
    assert decision_errors(decisions, {"curator", "shadow_observer"}, _options()) == []


def test_role_set_must_match_pass():
    from threadkeeper.model_routes import decision_errors
    errors = decision_errors(
        [_decision(), _decision(), _decision(role="probe_runner")],
        {"curator", "shadow_observer"},
        _options(),
    )
    joined = "\n".join(errors)
    assert "duplicate roles: curator" in joined
    assert "missing roles: shadow_observer" in joined
    assert "roles not in this pass: probe_runner" in joined


def test_router_cannot_route_itself_and_reason_is_bounded():
    from threadkeeper.model_routes import MAX_REASON_CHARS, decision_errors
    errors = decision_errors(
        [_decision(role="model_router", reason="x" * (MAX_REASON_CHARS + 1))],
        {"model_router"},
        _options(),
    )
    joined = "\n".join(errors)
    assert "cannot route itself" in joined
    assert "reason must be 1-500 characters" in joined


def test_non_object_and_missing_fields_are_reported():
    from threadkeeper.model_routes import decision_errors
    errors = decision_errors(["nope", {"role": "curator"}], {"curator"}, _options())
    joined = "\n".join(errors)
    assert "decision 0: must be an object" in joined
    assert "decision 1: missing string fields: cli, model, effort, reason" in joined


def test_decision_model_ids_are_case_sensitive():
    from threadkeeper.model_routes import decision_errors
    errors = decision_errors([_decision(model="GPT-5.6-Luna")], {"curator"}, _options())
    assert errors == ["decision 0: model 'GPT-5.6-Luna' is not offered by codex"]


def test_missing_catalog_blocks_writes():
    from threadkeeper.model_routes import decision_errors
    assert decision_errors([_decision()], {"curator"}, {}) == [
        "model catalog is unavailable; cannot validate decisions"
    ]


def test_insert_and_latest_route(fresh_mp):
    from threadkeeper import model_routes
    conn = fresh_mp["db"].get_db()
    model_routes.insert_routes(conn, [_decision(model="gpt-6.1-sol", effort="xhigh")], "mr-1", 100)
    model_routes.insert_routes(conn, [_decision()], "mr-2", 200)
    conn.commit()
    route = model_routes.latest_route(conn, "curator")
    assert (route.model, route.effort, route.pass_id, route.decided_at) == (
        "gpt-5.6-luna", "high", "mr-2", 200)
    assert [r.pass_id for r in model_routes.recent_routes(conn, "curator")] == ["mr-2", "mr-1"]
    assert model_routes.latest_route(conn, "shadow_observer") is None


def test_active_route_requires_enabled_router_and_valid_catalog(fresh_mp, monkeypatch):
    from threadkeeper import model_routes
    conn = fresh_mp["db"].get_db()
    model_routes.insert_routes(conn, [_decision()], "mr-1", 100)
    conn.commit()
    monkeypatch.setattr(model_routes, "cached_options", _options)

    assert model_routes.active_route("curator") is None  # interval 0 in tests

    monkeypatch.setattr(fresh_mp["config"], "MODEL_ROUTER_INTERVAL_S", 604800.0)
    assert model_routes.active_route("curator").model == "gpt-5.6-luna"
    assert model_routes.active_route("model_router") is None
    assert model_routes.stale_route("curator") is None

    monkeypatch.setattr(model_routes, "cached_options",
                        lambda: model_routes.options_from_catalog({"codex": {"models": ["other"]}}))
    assert model_routes.active_route("curator") is None
    assert model_routes.stale_route("curator").model == "gpt-5.6-luna"

    monkeypatch.setattr(model_routes, "cached_options", lambda: {})
    assert model_routes.active_route("curator").model == "gpt-5.6-luna"


def test_pass_lifecycle(fresh_mp):
    from threadkeeper import model_routes
    conn = fresh_mp["db"].get_db()
    assert model_routes.latest_pass(conn) is None
    router_pass = model_routes.open_pass(conn, ["shadow_observer", "curator"], 1000)
    assert router_pass.roles == ("curator", "shadow_observer")
    assert (router_pass.attempt, router_pass.status, router_pass.task_id) == ("primary", "running", "")

    model_routes.set_attempt(conn, router_pass.pass_id, "fallback", "tk_abc")
    model_routes.finish_pass(conn, router_pass.pass_id, "complete", 2000)
    conn.commit()
    stored = model_routes.latest_pass(conn)
    assert (stored.attempt, stored.task_id, stored.status, stored.ended_at) == (
        "fallback", "tk_abc", "complete", 2000)
    assert model_routes.last_complete_at(conn) == 2000


def test_routing_summary_lists_only_changes():
    from threadkeeper.model_routes import Route, routing_summary
    routes = [
        Route("curator", "codex", "gpt-5.6-luna", "high", "r", "mr-1", 1),
        Route("shadow_observer", "codex", "gpt-5.6-terra", "xhigh", "r", "mr-1", 1),
    ]
    previous = {
        "curator": "codex gpt-5.6-terra xhigh",
        "shadow_observer": "codex gpt-5.6-terra xhigh",
    }
    assert routing_summary(routes, previous) == (
        "routed 1/2: curator: codex gpt-5.6-terra xhigh → codex gpt-5.6-luna high"
    )
    assert routing_summary(routes[1:], previous) == "routed 0/1 (all kept)"


def test_settings_catalog_marks_routed_roles(fresh_mp, monkeypatch):
    from threadkeeper import model_catalog, model_routes

    monkeypatch.setitem(fresh_mp["config"].settings.spawn.loop, "curator", "auto")
    route = model_routes.Route("curator", "codex", "gpt-5.6-luna", "high", "r", "mr-1", 1234)
    monkeypatch.setattr(model_routes, "active_route",
                        lambda role: route if role == "curator" else None)
    monkeypatch.setattr(model_catalog, "cli_catalog", lambda refresh=False: [])

    roles = {r["role"]: r for r in model_catalog.settings_catalog()["agent_roles"]}
    assert (roles["curator"]["routed"], roles["curator"]["routed_at"]) == (True, 1234)
    assert (roles["curator"]["cli"], roles["curator"]["cli_dynamic"]) == ("codex", False)
    assert (roles["shadow_observer"]["routed"], roles["shadow_observer"]["routed_at"]) == (False, 0)
