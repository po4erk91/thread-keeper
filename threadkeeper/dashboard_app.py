"""Portable, display-only MCP App for the ThreadKeeper status dashboard.

The App intentionally opens the SQLite file in ``mode=ro`` instead of using
``get_db()``. A host may load or refresh a dashboard at any time, so doing so
must not create a session, run migrations, start daemons, refresh child
processes, or write telemetry. The public projection below also deliberately
omits prompts, task logs, CIDs, process IDs, and filesystem paths.
"""
from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

from mcp.server.mcpserver import Context
from pydantic import BaseModel, Field

from ._mcp import mcp, read_tool, structured_result
from .agent_metadata import AGENT_ROLES
from .agent_status import agent_status_snapshot
from .config import DB_PATH


DASHBOARD_URI = "ui://thread-keeper/dashboard"
_APP_CAPABILITY = "io.modelcontextprotocol/ui"
_APP_MIME_TYPE = "text/html;profile=mcp-app"
_CLIENT_CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"
_TASK_TIMEOUT_RETURN_CODE = 124
_ROLE_NAMES = {item["role"]: item["name"] for item in AGENT_ROLES}
_MATERIALIZATION_LABELS = {
    "skill_materialized": "Skill materialized",
    "lesson_append": "Lesson recorded",
    "curator_report_applied": "Curator report applied",
    "roadmap_issue_applied": "Roadmap issue applied",
    "evolve_applied": "Evolve suggestion applied",
}


class DashboardAppSnapshot(BaseModel):
    """Privacy-filtered, JSON-ready data consumed by the dashboard App."""

    available: bool = False
    generated_at: int = 0
    summary: dict[str, Any] = Field(default_factory=dict)
    loops: list[dict[str, Any]] = Field(default_factory=list)
    running_children: list[dict[str, Any]] = Field(default_factory=list)
    task_history: list[dict[str, Any]] = Field(default_factory=list)
    recent_failures: list[dict[str, Any]] = Field(default_factory=list)
    recent_materializations: list[dict[str, Any]] = Field(default_factory=list)


def _empty_snapshot() -> dict[str, Any]:
    return {
        "available": False,
        "generated_at": int(time.time()),
        "summary": {},
        "loops": [],
        "running_children": [],
        "task_history": [],
        "recent_failures": [],
        "recent_materializations": [],
    }


@contextmanager
def _read_existing_db() -> Iterator[sqlite3.Connection | None]:
    """Open an existing database read-only without invoking bootstrap code."""
    path = Path(DB_PATH)
    if not path.is_file():
        yield None
        return

    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        yield None
        return

    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def _safe_role(role: Any) -> str:
    return _ROLE_NAMES.get(str(role or ""), "Background agent")


def _task_state(return_code: Any) -> str:
    if return_code == 0:
        return "succeeded"
    if return_code == _TASK_TIMEOUT_RETURN_CODE:
        return "timed_out"
    return "failed"


def _task_history(conn: sqlite3.Connection, now: int) -> list[dict[str, Any]]:
    rows = _rows(
        conn,
        "SELECT role, started_at, ended_at, return_code FROM tasks "
        "WHERE ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 20",
    )
    return [
        {
            "role": _safe_role(row["role"]),
            "state": _task_state(row["return_code"]),
            "ended_at": int(row["ended_at"] or now),
            "duration_s": max(0, int(row["ended_at"] or now) - int(row["started_at"] or now)),
        }
        for row in rows
    ]


def _recent_failures(conn: sqlite3.Connection, now: int) -> list[dict[str, Any]]:
    rows = _rows(
        conn,
        "SELECT role, ended_at, return_code FROM tasks "
        "WHERE ended_at IS NOT NULL AND return_code IS NOT NULL "
        "AND return_code != 0 ORDER BY ended_at DESC LIMIT 10",
    )
    return [
        {
            "role": _safe_role(row["role"]),
            "state": _task_state(row["return_code"]),
            "ended_at": int(row["ended_at"] or now),
        }
        for row in rows
    ]


def _recent_materializations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in _MATERIALIZATION_LABELS)
    rows = _rows(
        conn,
        f"SELECT kind, created_at FROM events WHERE kind IN ({placeholders}) "
        "ORDER BY created_at DESC LIMIT 10",
        tuple(_MATERIALIZATION_LABELS),
    )
    return [
        {
            "kind": row["kind"],
            "label": _MATERIALIZATION_LABELS[row["kind"]],
            "created_at": int(row["created_at"] or 0),
        }
        for row in rows
    ]


def _public_loops(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    fields = (
        "id", "name", "status", "enabled", "due", "interval_s", "last_at",
        "last_age_s", "backlog_count", "backlog_label", "running_agent_count",
        "rss_mb", "verdict", "thread_alive",
    )
    return [
        {field: loop.get(field) for field in fields}
        for loop in snapshot.get("loops", [])
    ]


def _public_children(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "role": _safe_role(agent.get("role")),
            "elapsed_s": int(agent.get("elapsed_s") or 0),
            "rss_mb": int(agent.get("rss_mb") or 0),
        }
        for agent in snapshot.get("agents", [])
    ]


def _public_budget(snapshot: dict[str, Any]) -> dict[str, Any]:
    budget = snapshot.get("github_budget") or {}
    return {
        "remaining": budget.get("remaining"),
        "reset_at": budget.get("reset_at"),
        "cooldown_active": bool(budget.get("cooldown_active")),
        "cooldown_left_s": int(budget.get("cooldown_left_s") or 0),
    }


def dashboard_snapshot() -> dict[str, Any]:
    """Return a read-only, privacy-filtered status projection for the App."""
    snapshot = _empty_snapshot()
    with _read_existing_db() as conn:
        if conn is None:
            return snapshot

        status = agent_status_snapshot(
            refresh=False,
            conn=conn,
            include_recent=False,
            allow_external=False,
        )
        now = int(status["generated_at"])
        children = _public_children(status)
        loops = _public_loops(status)
        snapshot.update(
            available=True,
            generated_at=now,
            summary={
                "enabled_loops": int(status["enabled_loop_count"]),
                "running_loops": int(status["running_loop_count"]),
                "ready_loops": int(status["ready_loop_count"]),
                "running_children": len(children),
                "child_rss_mb": int(status["total_rss_mb"]),
                "github_budget": _public_budget(status),
            },
            loops=loops,
            running_children=children,
            task_history=_task_history(conn, now),
            recent_failures=_recent_failures(conn, now),
            recent_materializations=_recent_materializations(conn),
        )
    return snapshot


def _extra_get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, dict) and key in extra:
        return extra[key]
    return getattr(obj, key, None)


def supports_mcp_apps(ctx: Context | None) -> bool:
    """Return true only when the caller advertises the App MIME type."""
    if ctx is None:
        return False
    request_context = getattr(ctx, "request_context", None)
    meta = getattr(request_context, "meta", None)
    capabilities = _extra_get(meta, _CLIENT_CAPABILITIES_META)
    if capabilities is None:
        session = getattr(request_context, "session", None)
        params = getattr(session, "_client_params", None)
        capabilities = getattr(params, "capabilities", None)
    extensions = _extra_get(capabilities, "extensions")
    settings = _extra_get(extensions, _APP_CAPABILITY)
    mime_types = _extra_get(settings, "mimeTypes")
    return isinstance(mime_types, (list, tuple)) and _APP_MIME_TYPE in mime_types


def _dashboard_text(app_supported: bool) -> str:
    if app_supported:
        return "ThreadKeeper dashboard opened. The App receives privacy-filtered structured status data."
    return (
        "This host does not advertise MCP Apps support. Use agent_status(json_output=True) "
        "or mp_dashboard() for the unchanged text/JSON fallback."
    )


@read_tool(
    title="Open ThreadKeeper dashboard",
    meta={
        "ui": {
            "resourceUri": DASHBOARD_URI,
            "visibility": ["model", "app"],
        }
    },
)
def open_dashboard(ctx: Context | None = None) -> DashboardAppSnapshot:
    """Open the portable, display-only status dashboard when the host supports MCP Apps."""
    snapshot = DashboardAppSnapshot(**dashboard_snapshot())
    return structured_result(_dashboard_text(supports_mcp_apps(ctx)), snapshot)


_DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src 'none'; font-src 'none'; base-uri 'none'; form-action 'none'">
  <title>ThreadKeeper dashboard</title>
  <style>
    :root { color: var(--color-text-primary, #172033); background: var(--color-background-primary, #fff); font: 14px/1.45 system-ui, sans-serif; }
    body { margin: 0; } main { padding: 16px; max-width: 1100px; margin: auto; }
    header { display: flex; justify-content: space-between; align-items: center; gap: 12px; } h1 { font-size: 20px; margin: 0; } h2 { font-size: 15px; margin: 0 0 8px; }
    button { border: 1px solid var(--color-border, #bcc4d4); border-radius: 6px; background: var(--color-background-secondary, #f5f7fb); color: inherit; padding: 6px 10px; cursor: pointer; }
    .cards, .panels { display: grid; gap: 12px; margin-top: 16px; } .cards { grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); } .panels { grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
    .card, section { border: 1px solid var(--color-border, #d9dee9); border-radius: 8px; padding: 12px; background: var(--color-background-secondary, #fafbfe); }
    .label, .empty, #status { color: var(--color-text-secondary, #5d6779); font-size: 12px; } .value { font-size: 22px; font-weight: 650; }
    ul { list-style: none; padding: 0; margin: 0; } li { border-top: 1px solid var(--color-border, #e6e9f0); padding: 7px 0; } li:first-child { border-top: 0; padding-top: 0; }
    .row { display: flex; justify-content: space-between; gap: 12px; } .state { text-transform: capitalize; font-weight: 600; } @media (max-width: 520px) { main { padding: 12px; } }
  </style>
</head>
<body>
  <main>
    <header><div><h1>ThreadKeeper dashboard</h1><div id="status">Waiting for status data…</div></div><button id="refresh" type="button">Refresh</button></header>
    <div id="summary" class="cards"></div>
    <div class="panels">
      <section><h2>Autonomous loops</h2><div id="loops"></div></section>
      <section><h2>Running children</h2><div id="children"></div></section>
      <section><h2>Task history</h2><div id="history"></div></section>
      <section><h2>Recent failures</h2><div id="failures"></div></section>
      <section><h2>Recent materializations</h2><div id="materializations"></div></section>
    </div>
  </main>
  <script>
    (() => {
      "use strict";
      const pending = new Map(); let nextId = 1; let initialized = false;
      const byId = (id) => document.getElementById(id);
      const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
      const text = (value, fallback = "—") => typeof value === "string" ? value.slice(0, 120) : fallback;
      const number = (value, fallback = 0) => Number.isFinite(value) ? value : fallback;
      const list = (value) => Array.isArray(value) ? value : [];
      const stamp = (seconds) => seconds ? new Date(seconds * 1000).toLocaleString() : "—";
      const duration = (seconds) => seconds < 60 ? `${seconds}s` : `${Math.round(seconds / 60)}m`;

      function send(method, params) {
        const id = nextId++; const request = { jsonrpc: "2.0", id, method, params };
        window.parent.postMessage(request, "*");
        return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
      }

      function item(left, right) {
        const li = document.createElement("li"), row = document.createElement("div"), a = document.createElement("span"), b = document.createElement("span");
        row.className = "row"; a.textContent = left; b.className = "state"; b.textContent = right; row.append(a, b); li.append(row); return li;
      }

      function renderList(id, values, render) {
        const target = byId(id); target.replaceChildren();
        if (!values.length) { const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "No data yet."; target.append(empty); return; }
        const ul = document.createElement("ul"); values.forEach((value) => ul.append(render(value))); target.append(ul);
      }

      function snapshot(value) {
        if (!isObject(value) || !isObject(value.summary)) return null;
        return {
          available: value.available === true, generatedAt: number(value.generated_at), summary: value.summary,
          loops: list(value.loops), children: list(value.running_children), history: list(value.task_history),
          failures: list(value.recent_failures), materializations: list(value.recent_materializations)
        };
      }

      function render(raw) {
        const data = snapshot(raw); if (!data) return;
        byId("status").textContent = data.available ? `Updated ${stamp(data.generatedAt)}` : "No existing ThreadKeeper database is available yet.";
        const budget = isObject(data.summary.github_budget) ? data.summary.github_budget : {};
        const cards = [["Enabled loops", number(data.summary.enabled_loops)], ["Running children", number(data.summary.running_children)], ["Child RSS", `${number(data.summary.child_rss_mb)} MB`], ["GitHub budget", budget.cooldown_active === true ? `Paused ${number(budget.cooldown_left_s)}s` : (budget.remaining == null ? "Unknown" : `${number(budget.remaining)} left`)]];
        const summary = byId("summary"); summary.replaceChildren(); cards.forEach(([label, value]) => { const card = document.createElement("div"), l = document.createElement("div"), v = document.createElement("div"); card.className = "card"; l.className = "label"; v.className = "value"; l.textContent = label; v.textContent = value; card.append(l, v); summary.append(card); });
        renderList("loops", data.loops, (loop) => item(`${text(loop.name)} · ${number(loop.backlog_count)} ${text(loop.backlog_label, "items")}`, text(loop.status)));
        renderList("children", data.children, (child) => item(`${text(child.role)} · ${duration(number(child.elapsed_s))}`, `${number(child.rss_mb)} MB`));
        renderList("history", data.history, (task) => item(`${text(task.role)} · ${duration(number(task.duration_s))}`, text(task.state)));
        renderList("failures", data.failures, (failure) => item(`${text(failure.role)} · ${stamp(number(failure.ended_at))}`, text(failure.state)));
        renderList("materializations", data.materializations, (event) => item(text(event.label), stamp(number(event.created_at))));
      }

      function fromResult(result) {
        if (!isObject(result)) return null;
        return isObject(result.structuredContent) ? result.structuredContent : (isObject(result.result) ? result.result.structuredContent : null);
      }

      window.addEventListener("message", (event) => {
        if (event.source !== window.parent || !isObject(event.data) || event.data.jsonrpc !== "2.0") return;
        const message = event.data;
        if (message.method === "ui/notifications/tool-result" && isObject(message.params)) { render(fromResult(message.params)); return; }
        if (typeof message.id !== "number" || !pending.has(message.id)) return;
        const request = pending.get(message.id); pending.delete(message.id);
        if (isObject(message.error)) { request.reject(new Error("Host request failed")); return; }
        request.resolve(message.result);
        if (!initialized) { initialized = true; window.parent.postMessage({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} }, "*"); }
      });

      byId("refresh").addEventListener("click", () => {
        byId("status").textContent = "Refreshing…";
        send("tools/call", { name: "open_dashboard", arguments: {} }).then((result) => render(fromResult(result))).catch(() => { byId("status").textContent = "Refresh was unavailable."; });
      });
      // Sandboxed App iframes can have opaque origins. Inbound messages are
      // therefore source-checked above; the CSP blocks every network connection.
      send("ui/initialize", { capabilities: {} }).catch(() => { byId("status").textContent = "Waiting for host initialization…"; });
    })();
  </script>
</body>
</html>"""


@mcp.resource(
    DASHBOARD_URI,
    name="thread-keeper-dashboard",
    title="ThreadKeeper portable dashboard",
    description="Display-only MCP App for privacy-filtered autonomous status.",
    mime_type=_APP_MIME_TYPE,
    meta={
        "ui": {
            "csp": {
                "connectDomains": [],
                "resourceDomains": [],
                "frameDomains": [],
                "baseUriDomains": [],
            },
            "permissions": {},
            "prefersBorder": True,
        }
    },
)
def dashboard_app_resource() -> str:
    """Return the static, sandboxed App shell without touching local state."""
    return _DASHBOARD_HTML
