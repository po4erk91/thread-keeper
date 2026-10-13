"""Model router MCP tools.

  model_route_write(pass_id, decisions) — the router child records its decisions
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

from .. import config
from .. import model_routes as routes
from .._mcp import write_tool
from ..agent_metadata import ROUTER_ROLE
from ..db import get_db
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
