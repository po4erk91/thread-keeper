"""Model router loop — weekly CLI/model/effort routing for `auto` roles.

Each hourly tick resolves to exactly one action (see `decide`). A pass spawns
the router on its primary settings. If that child exits without recording a
decision, the same pass reruns once on the fallback settings; a pass that
fails both ways backs off before the primary is tried again.
"""
from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from . import config, daemon_state, identity
from . import model_routes as routes
from .agent_metadata import ROUTER_ROLE
from .db import get_db
from .helpers import alive, daemon_sleep, fmt_age, single_flight_lock
from .identity import _ensure_session
from .spawn_result import parse_spawn_result

logger = logging.getLogger(__name__)

_started = False
_TICK_S = 3600
_BACKOFF_S = 6 * 3600
_RECORDED_ACTIONS = frozenset({"start", "fallback", "fail"})
ROUTER_TOOL = "mcp__thread-keeper__model_route_write"
ROUTER_PROMPT_PREFIX = "You are a MODEL ROUTER"

ROUTER_PROMPT = ROUTER_PROMPT_PREFIX + """ for thread-keeper's background agents.

Each role below runs as a spawned child on one CLI, model and reasoning effort.
Choose the best CLI + model + effort for every listed role for the next week.

Weigh three things:
  1. Price: tokens per run, reasoning effort, and model tier. Cheaper is
     better when quality holds.
  2. Speed and reliability: run time, success rate, and provider failures
     such as "Selected model is at capacity".
  3. Result quality: the recent outcome lines of each role.

Rules:
  - Keeping the current setting is a valid and expected answer. Switch only
    when the data gives a concrete reason, and name that reason.
  - With fewer than 5 runs on the current setting, prefer keeping it.
  - Roles with impact code_write or external_write weigh quality above price.
  - Choose only from AVAILABLE OPTIONS. The effort must be listed for that
    model; for a CLI that encodes effort in the model, use an empty effort.

When you are done, call model_route_write exactly once with
  pass_id = "%(pass_id)s"
  decisions = a list with one object per role:
              {"role", "cli", "model", "effort", "reason"}
  roles to decide: %(roles)s
If it returns ERR, fix every listed problem and call it again.
Finish with one line: "Routed N of M roles."

%(fence)s

%(digest)s
"""


@dataclass(frozen=True)
class TickState:
    roles: tuple[str, ...]
    last_pass: Optional[routes.RouterPass]
    child_running: bool
    last_complete_at: Optional[int]
    missing_route: bool
    now: int
    interval_s: float
    force: bool = False


def _pending_action(pending: routes.RouterPass, child_running: bool) -> str:
    if child_running:
        return "running"
    return "fallback" if pending.attempt == "primary" else "fail"


def _in_backoff(state: TickState) -> bool:
    last = state.last_pass
    if last is None or last.status != "failed":
        return False
    return state.now - int(last.ended_at or 0) < _BACKOFF_S


def _due(state: TickState) -> bool:
    if state.missing_route or state.last_complete_at is None:
        return True
    return state.now - state.last_complete_at >= state.interval_s


def _retry_in(state: TickState) -> int:
    return max(0, int(state.last_pass.ended_at or 0) + _BACKOFF_S - state.now)


def _due_in(state: TickState) -> int:
    next_due = int(state.last_complete_at or state.now) + int(state.interval_s)
    return max(0, next_due - state.now)


def decide(state: TickState) -> str:
    """Name the single action this tick takes."""
    if not state.roles:
        return "skip"
    last = state.last_pass
    if last is not None and last.status == "running":
        return _pending_action(last, state.child_running)
    if state.force:
        return "start"
    if _in_backoff(state):
        return "backoff"
    return "start" if _due(state) else "idle"


def _child_running(conn: sqlite3.Connection, task_id: str, now: int) -> bool:
    """True while the pass's child is alive; reaps a dead row on the way."""
    if not task_id:
        return False
    row = conn.execute(
        "SELECT pid, ended_at FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None or row["ended_at"] is not None:
        return False
    if alive(int(row["pid"] or 0)):
        return True
    conn.execute(
        "UPDATE tasks SET ended_at=? WHERE id=? AND ended_at IS NULL", (now, task_id)
    )
    conn.commit()
    return False


def read_state(conn: sqlite3.Connection, now: int, interval_s: float,
               force: bool = False) -> TickState:
    """Tick inputs without probing (or reaping) the pass's child process."""
    from .spawn_config import roles_on_auto

    roles = tuple(roles_on_auto())
    return TickState(
        roles=roles,
        last_pass=routes.latest_pass(conn),
        child_running=False,
        last_complete_at=routes.last_complete_at(conn),
        missing_route=any(routes.active_route(role) is None for role in roles),
        now=now,
        interval_s=interval_s,
        force=force,
    )


def _tick_state(conn: sqlite3.Connection, now: int, interval_s: float,
                force: bool) -> TickState:
    state = read_state(conn, now, interval_s, force)
    last = state.last_pass
    pending_task = last.task_id if last and last.status == "running" else ""
    return dataclasses.replace(
        state, child_running=_child_running(conn, pending_task, now)
    )


def describe_next_pass(state: TickState) -> str:
    """Operator wording for when the next pass runs."""
    if state.interval_s <= 0:
        return "disabled"
    if not state.roles:
        return "none (no auto roles)"
    last = state.last_pass
    if last is not None and last.status == "running":
        return f"running (attempt {last.attempt})"
    if _in_backoff(state):
        return f"backoff, retry in {fmt_age(_retry_in(state))}"
    if _due(state):
        return "due now"
    return f"in {fmt_age(_due_in(state))}"


def build_prompt(conn: sqlite3.Connection, router_pass: routes.RouterPass) -> str:
    from .model_router_digest import build_digest
    from .review_prompts import DATA_FENCE, fence_observed
    from .spawn_config import resolution_details

    active = identity.active_cli()
    current = {role: resolution_details(role, active) for role in router_pass.roles}
    digest = build_digest(
        conn, list(router_pass.roles), routes.cached_options(), current,
        int(time.time()),
    )
    return ROUTER_PROMPT % {
        "pass_id": router_pass.pass_id,
        "roles": ", ".join(router_pass.roles),
        "fence": DATA_FENCE,
        # Outcome lines echo child output, which can carry untrusted text.
        "digest": fence_observed(digest, "router telemetry"),
    }


def _spawn_child(prompt: str, cli: str = "", model: str = "", effort: str = "") -> str:
    from .tools.spawn import _spawn_impl  # type: ignore

    return _spawn_impl(
        prompt=prompt,
        visible=False,
        capture_output=True,
        permission_mode="auto",
        role=ROUTER_ROLE,
        write_origin=ROUTER_ROLE,
        memory_access="read_only",
        slim=True,
        allowed_tools_override=(ROUTER_TOOL,),
        cli=cli,
        model=model,
        effort=effort,
    )


@contextlib.contextmanager
def _pass_env(pass_id: str) -> Iterator[None]:
    """Export the pass id only around the launch, never to other loops."""
    previous = os.environ.get(routes.PASS_ID_ENV)
    os.environ[routes.PASS_ID_ENV] = pass_id
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(routes.PASS_ID_ENV, None)
        else:
            os.environ[routes.PASS_ID_ENV] = previous


def _attempt_settings(attempt: str) -> dict[str, str]:
    if attempt == "primary":
        return {}
    return {
        "cli": config.MODEL_ROUTER_FALLBACK_CLI,
        "model": config.MODEL_ROUTER_FALLBACK_MODEL,
        "effort": config.MODEL_ROUTER_FALLBACK_EFFORT,
    }


def _launch(conn: sqlite3.Connection, router_pass: routes.RouterPass, attempt: str) -> str:
    prompt = build_prompt(conn, router_pass)
    with _pass_env(router_pass.pass_id):
        result = parse_spawn_result(_spawn_child(prompt, **_attempt_settings(attempt)))
    routes.set_attempt(conn, router_pass.pass_id, attempt, result.task_id or "")
    conn.commit()
    if not result.ok:
        return (
            f"spawn_error attempt={attempt} pass={router_pass.pass_id} "
            f":: {result.text[:160]}"
        )
    return f"spawned attempt={attempt} pass={router_pass.pass_id} task={result.task_id}"


def _skip(conn: sqlite3.Connection, state: TickState) -> str:
    return "skip no_auto_roles"


def _running(conn: sqlite3.Connection, state: TickState) -> str:
    return f"running pass={state.last_pass.pass_id} attempt={state.last_pass.attempt}"


def _fallback(conn: sqlite3.Connection, state: TickState) -> str:
    return _launch(conn, state.last_pass, "fallback")


def _fail(conn: sqlite3.Connection, state: TickState) -> str:
    routes.finish_pass(conn, state.last_pass.pass_id, "failed", state.now)
    conn.commit()
    return f"spawn_failed pass={state.last_pass.pass_id} attempts=primary,fallback"


def _backoff(conn: sqlite3.Connection, state: TickState) -> str:
    return f"backoff pass={state.last_pass.pass_id} retry_in={_retry_in(state)}s"


def _refreshed_options() -> dict[str, routes.CliOptions]:
    """Refresh the TTL-bounded catalog cache, then read it: nothing else in a
    headless host writes the cache the router validates against."""
    from . import model_catalog

    try:
        model_catalog.cli_catalog(refresh=False)
    except Exception:
        logger.warning("model_router: catalog refresh failed", exc_info=True)
    return routes.cached_options()


def _start(conn: sqlite3.Connection, state: TickState) -> str:
    if not _refreshed_options():
        return "skip catalog_unavailable"
    return _launch(conn, routes.open_pass(conn, list(state.roles), state.now), "primary")


def _idle(conn: sqlite3.Connection, state: TickState) -> str:
    return f"idle next_due_in={_due_in(state)}s"


_ACTIONS: dict[str, Callable[[sqlite3.Connection, TickState], str]] = {
    "skip": _skip,
    "running": _running,
    "fallback": _fallback,
    "fail": _fail,
    "backoff": _backoff,
    "start": _start,
    "idle": _idle,
}


def _record_tick(conn: sqlite3.Connection, outcome: str) -> None:
    try:
        identity._emit(
            conn, "model_router_pass", target=str(int(time.time())),
            summary=outcome[:300],
        )
        conn.commit()
    except sqlite3.Error:
        logger.debug("model_router: record_tick failed", exc_info=True)


def run_router_pass(force: bool = False, *, scheduled: bool = False) -> str:
    """One router tick. `force` starts a pass even when none is due."""
    interval_s = float(config.MODEL_ROUTER_INTERVAL_S)
    if interval_s <= 0 and not force:
        return "disabled"
    conn = get_db()
    if not daemon_state.claim_pass(
        "model_router", _TICK_S, scheduled=scheduled, conn=conn,
    ):
        return "not_due"
    _ensure_session(conn)
    with single_flight_lock("model-router") as locked:
        if not locked:
            return "router_tick_running (single-flight lock)"
        state = _tick_state(conn, int(time.time()), interval_s, force)
        action = decide(state)
        outcome = _ACTIONS[action](conn, state)
        if action in _RECORDED_ACTIONS:
            _record_tick(conn, outcome)
        return outcome


def _serve_loop() -> None:
    while True:
        try:
            run_router_pass(scheduled=True)
        except Exception:
            logger.debug("model_router tick failed", exc_info=True)
        daemon_sleep(_TICK_S)


def start_model_router_daemon() -> None:
    """Idempotent. Same cascade guards as the other spawning loops."""
    global _started
    from .daemon_liveness import daemon_thread_alive, start_daemon_thread

    if _started and daemon_thread_alive("model_router"):
        return
    if float(config.MODEL_ROUTER_INTERVAL_S) <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED, SEMANTIC_AVAILABLE

    if not BACKGROUND_DAEMONS_ALLOWED or not SEMANTIC_AVAILABLE:
        return
    start_daemon_thread("model_router", _serve_loop)
    _started = True
