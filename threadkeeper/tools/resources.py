"""Read-only MCP **Resources** for thread-keeper (roadmap #78).

MCP defines three core server primitives. thread-keeper historically exposed
only **tools** (model-controlled, may act). This module adds the second
primitive — **Resources** (application-controlled, read-only, safe for a host to
pull automatically) — for the genuinely read-only memory snapshots:

  * ``memory://brief``        — the session-start memory brief (``render_brief``)
  * ``memory://context``      — runtime context (session id, age, thread counts)
  * ``memory://dashboard``    — whole-system telemetry rollup (``mp_dashboard``)
  * ``memory://agent-status`` — autonomous-loop status snapshot

Why resources and not just the existing tools: on hookless CLIs (Codex /
Antigravity / Gemini-legacy / Copilot) the managed instructions block asks the
agent to *remember* to call ``brief()`` before answering, and the project's own
docs note agents focused on their task often skip such calls. A Resource lets
the host surface the brief as attachable / ``@``-mentionable read-only context
through a mechanical channel, independent of whether the agent calls a tool. The
hook-injected brief and the ``brief()`` tool remain the fallback for hosts that
don't advertise the ``resources`` capability — nothing here changes the tool
surface (no tool is added, removed, or altered).

These resources are deliberately **side-effect-free**. ``memory://brief`` renders
with ``lean=True`` so the behavioral nudge/hint blocks (which ``INSERT``
``*_hint_shown`` events) never fire on an automatic host pull — a resource a host
refreshes on a timer must not mutate the escalation counters. The static memory
(core_memory, style, verbatim, user_model) is still rendered. ``memory://
agent-status`` uses ``refresh=False`` so a pull never triggers a process re-scan.

URIs are static on purpose: the spec's resource *templates* (``{param}``) are
still unevenly implemented across hosts, so parameterized URIs are left as a
later, host-gated step (see roadmap #78).
"""
from __future__ import annotations

from .._mcp import mcp
from ..db import get_db
from ..identity import _ensure_session
from ..brief import render_brief, render_context
from .dashboard import mp_dashboard
from ..agent_status import agent_status_snapshot, format_agent_status


@mcp.resource(
    "memory://brief",
    name="brief",
    title="thread-keeper memory brief",
    description="Session-start memory brief: core memory, open/idle/closed "
    "threads, live peers, style, verbatim, user-model. Read-only, rendered "
    "lean (no behavioral nudges, no side effects). Mirrors the brief() tool.",
    mime_type="text/plain",
)
def brief_resource() -> str:
    conn = get_db()
    _ensure_session(conn)
    # lean=True keeps the pull side-effect-free: the spawn/thread/skill hint
    # blocks (which write *_hint_shown events) are all gated on `not eff_lean`.
    return render_brief(conn, scope="full", lean=True)


@mcp.resource(
    "memory://context",
    name="context",
    title="thread-keeper runtime context",
    description="Runtime context: session id, age, semantic on/off, db path, "
    "thread counts. Read-only. Mirrors the context() tool's text block.",
    mime_type="text/plain",
)
def context_resource() -> str:
    conn = get_db()
    _ensure_session(conn)
    text, _ = render_context(conn)
    return text


@mcp.resource(
    "memory://dashboard",
    name="dashboard",
    title="thread-keeper system dashboard",
    description="One-call rollup: store sizes, autonomous-loop fire counts, and "
    "what those loops produced. Read-only. Mirrors the mp_dashboard() tool.",
    mime_type="text/plain",
)
def dashboard_resource() -> str:
    # mp_dashboard() is the read_tool() function; FastMCP leaves it directly
    # callable. It opens its own db handle and is defensive on partial schemas.
    return mp_dashboard()


@mcp.resource(
    "memory://agent-status",
    name="agent-status",
    title="thread-keeper autonomous-loop status",
    description="Autonomous learning loops: state, backlog, last pass, RSS. "
    "Read-only cached snapshot (refresh=False, no process re-scan). Mirrors "
    "the agent_status() tool's formatted summary.",
    mime_type="text/plain",
)
def agent_status_resource() -> str:
    return format_agent_status(agent_status_snapshot(refresh=False))
