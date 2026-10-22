"""Conformance coverage for the durable MCP Tasks extension."""
from __future__ import annotations

from types import SimpleNamespace

import anyio
import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolRequestParams, CallToolResult, ClientCapabilities, TextContent
from mcp_types.version import LATEST_MODERN_VERSION


class _Context:
    def __init__(self, capabilities, principal: str = "principal-a"):
        self.protocol_version = LATEST_MODERN_VERSION
        self.session = SimpleNamespace(client_capabilities=capabilities)
        self.principal = principal


def _task_capabilities():
    from threadkeeper.mcp_tasks import TASKS_EXTENSION_ID

    return ClientCapabilities(extensions={TASKS_EXTENSION_ID: {}})


def _insert_running_task(pkg, task_id: str) -> None:
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at) VALUES (?, 0, ?, ?, 1)",
        (task_id, str(pkg["tmp"]), "durable task test"),
    )
    conn.commit()


def test_mcp_tasks_advertises_get_and_cancel_without_enumeration(fresh_mp):
    from threadkeeper.mcp_tasks import TASKS_EXTENSION_ID, TaskRequestParams

    lowlevel = fresh_mp["mcp"]._lowlevel_server
    assert lowlevel.extensions[TASKS_EXTENSION_ID] == {}
    assert "tasks/get" in lowlevel._request_handlers
    assert "tasks/cancel" in lowlevel._request_handlers
    assert "tasks/list" not in lowlevel._request_handlers
    assert "tasks/update" not in lowlevel._request_handlers
    get_handler = lowlevel._request_handlers["tasks/get"].handler
    assert get_handler.__name__ == "gated"

    legacy = _Context(_task_capabilities())
    legacy.protocol_version = "2025-11-25"
    with pytest.raises(MCPError, match="Method not found"):
        anyio.run(get_handler, legacy, TaskRequestParams(taskId="opaque"))


def test_task_eligible_spawn_returns_opaque_durable_handle_and_reconnects(
    fresh_mp, monkeypatch
):
    from threadkeeper import mcp_tasks

    _insert_running_task(fresh_mp, "tk_internal")
    monkeypatch.setattr(mcp_tasks, "authenticated_principal", lambda ctx: ctx.principal)
    extension = mcp_tasks.MCPTasksExtension()
    context = _Context(_task_capabilities())
    request = CallToolRequestParams(name="spawn", arguments={"visible": False})
    launch = CallToolResult(
        content=[TextContent(type="text", text="ok task=tk_internal pid=1")]
    )

    async def call_next(_ctx):
        return launch

    created = anyio.run(extension.intercept_tool_call, request, context, call_next)
    public_id = created["taskId"]
    assert created["resultType"] == "task"
    assert created["status"] == "working"
    assert created["pollIntervalMs"] == mcp_tasks.POLL_INTERVAL_MS
    assert public_id != "tk_internal"
    assert len(public_id) >= 40

    reconnected = mcp_tasks.MCPTasksExtension()
    task = anyio.run(reconnected.get_task, context, mcp_tasks.TaskRequestParams(taskId=public_id))
    assert task["resultType"] == "complete"
    assert task["taskId"] == public_id
    assert task["status"] == "working"
    assert task["pollIntervalMs"] == mcp_tasks.POLL_INTERVAL_MS


def test_legacy_or_visible_spawn_keeps_existing_result(fresh_mp):
    from threadkeeper import mcp_tasks

    extension = mcp_tasks.MCPTasksExtension()
    legacy_context = _Context(None)
    launch = CallToolResult(content=[TextContent(type="text", text="legacy result")])

    async def call_next(_ctx):
        return launch

    request = CallToolRequestParams(name="spawn", arguments={"visible": False})
    assert anyio.run(extension.intercept_tool_call, request, legacy_context, call_next) is launch

    modern_context = _Context(_task_capabilities())
    visible = CallToolRequestParams(name="spawn", arguments={"visible": True})
    assert anyio.run(extension.intercept_tool_call, visible, modern_context, call_next) is launch

    legacy_protocol = _Context(_task_capabilities())
    legacy_protocol.protocol_version = "2025-11-25"
    assert anyio.run(extension.intercept_tool_call, request, legacy_protocol, call_next) is launch


def test_task_access_is_authorization_bound_and_cancel_is_idempotent(
    fresh_mp, monkeypatch
):
    from threadkeeper import mcp_tasks
    from threadkeeper.tools import spawn

    _insert_running_task(fresh_mp, "tk_cancel")
    monkeypatch.setattr(mcp_tasks, "authenticated_principal", lambda ctx: ctx.principal)
    extension = mcp_tasks.MCPTasksExtension()
    owner = _Context(_task_capabilities(), principal="principal-a")
    other = _Context(_task_capabilities(), principal="principal-b")
    created = extension._create_task("tk_cancel", mcp_tasks._owner_key(owner))
    public_id = created["taskId"]

    with pytest.raises(MCPError, match="Unknown task"):
        anyio.run(extension.get_task, other, mcp_tasks.TaskRequestParams(taskId=public_id))

    calls: list[str] = []
    monkeypatch.setattr(
        spawn,
        "task_kill",
        lambda task_id: calls.append(task_id) or "signal=SIGTERM sent",
    )
    params = mcp_tasks.TaskRequestParams(taskId=public_id)
    assert anyio.run(extension.cancel_task, owner, params) == {}
    assert anyio.run(extension.cancel_task, owner, params) == {}
    assert calls == ["tk_cancel"]

    conn = fresh_mp["db"].get_db()
    conn.execute("UPDATE tasks SET ended_at=2 WHERE id='tk_cancel'")
    conn.commit()
    task = anyio.run(extension.get_task, owner, params)
    assert task["status"] == "cancelled"


def test_task_methods_require_the_extension_capability(fresh_mp, monkeypatch):
    from threadkeeper import mcp_tasks

    _insert_running_task(fresh_mp, "tk_capability")
    monkeypatch.setattr(mcp_tasks, "authenticated_principal", lambda ctx: ctx.principal)
    extension = mcp_tasks.MCPTasksExtension()
    owner = _Context(_task_capabilities())
    created = extension._create_task("tk_capability", mcp_tasks._owner_key(owner))
    unsupported = _Context(None)

    with pytest.raises(MCPError, match="required extension"):
        anyio.run(
            extension.get_task,
            unsupported,
            mcp_tasks.TaskRequestParams(taskId=created["taskId"]),
        )
