"""Model-router persistence and validation.

`model_routes` holds one row per routing decision (the latest row per role is
the current route); `model_router_passes` holds the state of each router pass.
Spawn resolution reads routes through `active_route`, which only returns a
route the local model catalog can still run.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from .agent_metadata import ROUTER_ROLE

PASS_ID_ENV = "THREADKEEPER_MODEL_ROUTER_PASS_ID"
MAX_REASON_CHARS = 500
_DECISION_FIELDS = ("role", "cli", "model", "effort", "reason")
_ROUTE_COLUMNS = "role, cli, model, effort, reason, pass_id, decided_at"
_PASS_COLUMNS = "pass_id, roles_json, attempt, task_id, status, started_at, ended_at"


@dataclass(frozen=True)
class Route:
    role: str
    cli: str
    model: str
    effort: str
    reason: str
    pass_id: str
    decided_at: int


@dataclass(frozen=True)
class RouterPass:
    pass_id: str
    roles: tuple[str, ...]
    attempt: str
    task_id: str
    status: str
    started_at: int
    ended_at: Optional[int]


@dataclass(frozen=True)
class CliOptions:
    models: tuple[str, ...]
    model_efforts: dict[str, tuple[str, ...]]
    efforts: tuple[str, ...]
    effort_mode: str

    def efforts_for(self, model: str) -> tuple[str, ...]:
        return self.model_efforts.get(model) or self.efforts


# ── catalog options ─────────────────────────────────────────────────────────

def _model_id(entry: Any) -> str:
    return str(entry).split("\t", 1)[0].strip()


def _cli_options(cli: str, data: dict[str, Any]) -> CliOptions:
    from .spawn_config import EFFORT_OPTIONS

    models = tuple(m for m in (_model_id(e) for e in data.get("models") or []) if m)
    model_efforts = {
        _model_id(model): tuple(efforts)
        for model, efforts in (data.get("model_efforts") or {}).items()
    }
    efforts = tuple(data.get("effort_options") or EFFORT_OPTIONS.get(cli, ()))
    effort_mode = "model" if cli == "antigravity" else "independent"
    return CliOptions(models, model_efforts, efforts, effort_mode)


def options_from_catalog(clis: dict[str, Any]) -> dict[str, CliOptions]:
    return {
        cli: _cli_options(cli, data)
        for cli, data in (clis or {}).items()
        if isinstance(data, dict)
    }


def cached_options() -> dict[str, CliOptions]:
    """Catalog options from the on-disk cache; {} when there is no cache."""
    from .model_catalog import read_cached_clis

    return options_from_catalog(read_cached_clis())


def route_problem(cli: str, model: str, effort: str,
                  options: dict[str, CliOptions]) -> str:
    """Why a CLI/model/effort triple cannot run here, or '' when it can."""
    cli_options = options.get(cli)
    if cli_options is None:
        return f"cli {cli!r} is not in the model catalog"
    if model not in cli_options.models:
        return f"model {model!r} is not offered by {cli}"
    if cli_options.effort_mode == "model":
        return "" if not effort else f"{cli} encodes effort in the model; effort must be empty"
    if effort not in cli_options.efforts_for(model):
        return f"effort {effort!r} is not allowed for {model}"
    return ""


def route_is_valid(route: Route, options: dict[str, CliOptions]) -> bool:
    """A missing catalog never invalidates a route; only a known gap does."""
    if not options:
        return True
    return route_problem(route.cli, route.model, route.effort, options) == ""


# ── decision validation ─────────────────────────────────────────────────────

def _clean(value: str) -> str:
    return value.strip().lower()


def _role_set_errors(decisions: list[Any], expected_roles: set[str]) -> list[str]:
    roles = [_clean(str(d.get("role", ""))) for d in decisions if isinstance(d, dict)]
    duplicates = sorted({role for role in roles if roles.count(role) > 1})
    missing = sorted(expected_roles - set(roles))
    extra = sorted(set(roles) - expected_roles)
    errors = []
    if duplicates:
        errors.append(f"duplicate roles: {', '.join(duplicates)}")
    if missing:
        errors.append(f"missing roles: {', '.join(missing)}")
    if extra:
        errors.append(f"roles not in this pass: {', '.join(extra)}")
    return errors


def _decision_problems(decision: Any, options: dict[str, CliOptions]) -> list[str]:
    if not isinstance(decision, dict):
        return ["must be an object"]
    missing = [f for f in _DECISION_FIELDS if not isinstance(decision.get(f), str)]
    if missing:
        return [f"missing string fields: {', '.join(missing)}"]
    problems = []
    if _clean(decision["role"]) == ROUTER_ROLE:
        problems.append("the model router cannot route itself")
    triple_problem = route_problem(
        _clean(decision["cli"]), decision["model"].strip(),
        _clean(decision["effort"]), options,
    )
    if triple_problem:
        problems.append(triple_problem)
    if not 1 <= len(decision["reason"].strip()) <= MAX_REASON_CHARS:
        problems.append(f"reason must be 1-{MAX_REASON_CHARS} characters")
    return problems


def decision_errors(decisions: list[Any], expected_roles: set[str],
                    options: dict[str, CliOptions]) -> list[str]:
    """Every reason the decision set cannot be recorded; [] when it can."""
    if not options:
        return ["model catalog is unavailable; cannot validate decisions"]
    errors = _role_set_errors(decisions, expected_roles)
    for index, decision in enumerate(decisions):
        errors.extend(
            f"decision {index}: {problem}"
            for problem in _decision_problems(decision, options)
        )
    return errors


# ── routes ──────────────────────────────────────────────────────────────────

def latest_route(conn: sqlite3.Connection, role: str) -> Optional[Route]:
    row = conn.execute(
        f"SELECT {_ROUTE_COLUMNS} FROM model_routes WHERE role=? "
        "ORDER BY decided_at DESC, id DESC LIMIT 1",
        (role,),
    ).fetchone()
    return Route(*row) if row else None


def recent_routes(conn: sqlite3.Connection, role: str, limit: int = 3) -> list[Route]:
    rows = conn.execute(
        f"SELECT {_ROUTE_COLUMNS} FROM model_routes WHERE role=? "
        "ORDER BY decided_at DESC, id DESC LIMIT ?",
        (role, limit),
    ).fetchall()
    return [Route(*row) for row in rows]


def _normalized(decision: dict[str, str]) -> dict[str, str]:
    return {
        "role": _clean(decision["role"]),
        "cli": _clean(decision["cli"]),
        "model": decision["model"].strip(),
        "effort": _clean(decision["effort"]),
        "reason": decision["reason"].strip(),
    }


def insert_routes(conn: sqlite3.Connection, decisions: list[dict[str, str]],
                  pass_id: str, now: int) -> list[Route]:
    """Insert validated decisions. The caller owns the commit."""
    routes = [
        Route(pass_id=pass_id, decided_at=now, **_normalized(d)) for d in decisions
    ]
    conn.executemany(
        f"INSERT INTO model_routes ({_ROUTE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(r.role, r.cli, r.model, r.effort, r.reason, r.pass_id, r.decided_at)
         for r in routes],
    )
    return routes


def _router_enabled() -> bool:
    from . import config

    return float(getattr(config, "MODEL_ROUTER_INTERVAL_S", 0) or 0) > 0


def _stored_route(role: str) -> Optional[Route]:
    if role == ROUTER_ROLE or not _router_enabled():
        return None
    try:
        from .db import get_db

        return latest_route(get_db(), role)
    except sqlite3.Error:
        return None


def active_route(role: str) -> Optional[Route]:
    """Current runnable route for `role`; None when the router is off, has not
    decided yet, or the catalog no longer offers the decided triple."""
    route = _stored_route(role)
    if route is None or not route_is_valid(route, cached_options()):
        return None
    return route


def stale_route(role: str) -> Optional[Route]:
    """The latest decision for `role` when the catalog no longer offers it."""
    route = _stored_route(role)
    if route is None or route_is_valid(route, cached_options()):
        return None
    return route


def triple(cli: str, model: str, effort: str) -> str:
    return f"{cli} {model or '(default)'} {effort or '(default)'}"


def routing_summary(routes: list[Route], previous: dict[str, str]) -> str:
    changes = [
        f"{route.role}: {previous.get(route.role, '?')} → "
        f"{triple(route.cli, route.model, route.effort)}"
        for route in routes
        if previous.get(route.role) != triple(route.cli, route.model, route.effort)
    ]
    head = f"routed {len(changes)}/{len(routes)}"
    return head + (": " + "; ".join(changes) if changes else " (all kept)")


# ── passes ──────────────────────────────────────────────────────────────────

def _pass_from_row(row: sqlite3.Row) -> RouterPass:
    return RouterPass(
        pass_id=row["pass_id"],
        roles=tuple(json.loads(row["roles_json"])),
        attempt=row["attempt"],
        task_id=row["task_id"],
        status=row["status"],
        started_at=int(row["started_at"]),
        ended_at=row["ended_at"],
    )


def get_pass(conn: sqlite3.Connection, pass_id: str) -> Optional[RouterPass]:
    row = conn.execute(
        f"SELECT {_PASS_COLUMNS} FROM model_router_passes WHERE pass_id=?",
        (pass_id,),
    ).fetchone()
    return _pass_from_row(row) if row else None


def latest_pass(conn: sqlite3.Connection) -> Optional[RouterPass]:
    row = conn.execute(
        f"SELECT {_PASS_COLUMNS} FROM model_router_passes "
        "ORDER BY started_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return _pass_from_row(row) if row else None


def open_pass(conn: sqlite3.Connection, roles: list[str], now: int) -> RouterPass:
    pass_id = f"mr-{now}-{secrets.token_hex(3)}"
    conn.execute(
        "INSERT INTO model_router_passes (pass_id, roles_json, attempt, started_at) "
        "VALUES (?, ?, 'primary', ?)",
        (pass_id, json.dumps(sorted(roles)), now),
    )
    conn.commit()
    return get_pass(conn, pass_id)


def set_attempt(conn: sqlite3.Connection, pass_id: str, attempt: str,
                task_id: str) -> None:
    conn.execute(
        "UPDATE model_router_passes SET attempt=?, task_id=? WHERE pass_id=?",
        (attempt, task_id, pass_id),
    )


def finish_pass(conn: sqlite3.Connection, pass_id: str, status: str, now: int) -> None:
    conn.execute(
        "UPDATE model_router_passes SET status=?, ended_at=? "
        "WHERE pass_id=? AND status='running'",
        (status, now, pass_id),
    )


def last_complete_at(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute(
        "SELECT MAX(ended_at) FROM model_router_passes WHERE status='complete'"
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None
