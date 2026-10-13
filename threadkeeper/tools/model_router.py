"""Model router MCP tools.

  model_route_write(pass_id, decisions) — the router child records its decisions
  model_router_run(force=True)          — run one router tick now
  model_router_status()                 — config, last pass, current routes
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

from .. import config
from .. import model_routes as routes
from .._mcp import read_tool, write_tool
from ..agent_metadata import ROUTER_ROLE
from ..db import get_db
from ..helpers import fmt_age
from ..identity import _emit, _ensure_session


def _write_authorized(pass_id: str) -> bool:
    return (
        bool(pass_id)
        and bool(config.SPAWNED_CHILD)
        and config.WRITE_ORIGIN == ROUTER_ROLE
        and os.environ.get(routes.PASS_ID_ENV, "").strip() == pass_id
    )


def _decision_list(decisions: Any) -> list[Any] | None:
    if isinstance(decisions, str):
        try:
            decisions = json.loads(decisions)
        except ValueError:
            return None
    return decisions if isinstance(decisions, list) else None


def _previous_triple(conn: sqlite3.Connection, role: str) -> str:
    route = routes.latest_route(conn, role)
    if route is not None:
        return routes.triple(route.cli, route.model, route.effort)
    from ..identity import active_cli
    from ..spawn_config import resolution_details

    details = resolution_details(role, active_cli())
    return routes.triple(details["cli"], details["model"], details["effort"])


def _record(conn: sqlite3.Connection, router_pass: routes.RouterPass,
            decisions: list[dict[str, str]]) -> str:
    now = int(time.time())
    previous = {role: _previous_triple(conn, role) for role in router_pass.roles}
    recorded = routes.insert_routes(conn, decisions, router_pass.pass_id, now)
    summary = routes.routing_summary(recorded, previous)
    routes.finish_pass(conn, router_pass.pass_id, "complete", now)
    _emit(conn, "model_router_pass", target=router_pass.pass_id, summary=summary)
    conn.commit()
    return summary


@write_tool(idempotent=True)
def model_route_write(pass_id: str, decisions: list[dict] | str) -> str:
    """Record the model router's decision for every role in this pass.

    Only the spawned router child carrying the parent-issued pass id may call
    this. `decisions` is a list (or a JSON-encoded list) with one object per
    role: {"role", "cli", "model", "effort", "reason"}. Every decision is
    checked against the local model catalog; any problem rejects the whole
    call and lists every error so the router can fix them and call again.
    """
    clean_id = (pass_id or "").strip()
    if not _write_authorized(clean_id):
        return "ERR route_write_not_authorized"
    decision_list = _decision_list(decisions)
    if decision_list is None:
        return "ERR decisions_must_be_a_list"
    conn = get_db()
    _ensure_session(conn)
    router_pass = routes.get_pass(conn, clean_id)
    if router_pass is None:
        return "ERR unknown_pass"
    if router_pass.status == "complete":
        return "ok already_recorded"
    if router_pass.status != "running":
        return f"ERR pass_not_running status={router_pass.status}"
    errors = routes.decision_errors(
        decision_list, set(router_pass.roles), routes.cached_options()
    )
    if errors:
        return "ERR invalid_decisions\n" + "\n".join(f"- {e}" for e in errors)
    return f"ok {_record(conn, router_pass, decision_list)}"


def _config_line() -> str:
    return (
        f"interval_s={float(config.MODEL_ROUTER_INTERVAL_S):.0f} "
        f"fallback={config.MODEL_ROUTER_FALLBACK_CLI}/"
        f"{config.MODEL_ROUTER_FALLBACK_MODEL}/{config.MODEL_ROUTER_FALLBACK_EFFORT}"
    )


def _last_pass_line(router_pass: routes.RouterPass | None, now: int) -> str:
    if router_pass is None:
        return "last pass: none"
    return (
        f"last pass: {router_pass.pass_id} status={router_pass.status} "
        f"attempt={router_pass.attempt} "
        f"started={fmt_age(max(0, now - router_pass.started_at))} ago"
    )


def _route_line(role: str, now: int) -> str:
    route = routes.active_route(role)
    if route is None:
        return f"  {role}: no decision yet — follows active CLI"
    return (
        f"  {role}: {routes.triple(route.cli, route.model, route.effort)} "
        f"since {fmt_age(max(0, now - route.decided_at))} ago — {route.reason}"
    )


@write_tool()
def model_router_run(force: bool = True) -> str:
    """Run one model-router tick now. force=True starts a pass even when
    none is due (a pass that is already running is never duplicated)."""
    from ..model_router import run_router_pass

    return run_router_pass(force=force)


@read_tool()
def model_router_status() -> str:
    """Model-router config, last pass, and the current route of each auto role."""
    from ..spawn_config import roles_on_auto

    now = int(time.time())
    roles = roles_on_auto()
    lines = [
        _config_line(),
        _last_pass_line(routes.latest_pass(get_db()), now),
        "auto roles: " + (", ".join(roles) if roles else "(none)"),
    ]
    lines.extend(_route_line(role, now) for role in roles)
    return "\n".join(lines)
