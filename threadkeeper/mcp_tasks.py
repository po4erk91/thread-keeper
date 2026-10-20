"""MCP Tasks extension for durable, headless ``spawn`` work.

The public MCP task ID is an authorization-bound opaque handle. It is never
the short ``tasks.id`` display ID used by thread-keeper's existing tools.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from mcp.server.extension import Extension, MethodBinding
from mcp.server.mcpserver import require_client_extension
from mcp.server.request_state import authenticated_principal
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolRequestParams, RequestParams
from mcp_types.version import LATEST_MODERN_VERSION
from pydantic import Field

from .config import TASK_LOG_DIR
from .db import get_db
from .task_spool import open_spool_binary_read

TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
POLL_INTERVAL_MS = 5_000
_TASK_ID_RE = re.compile(r"(?:^|\s)task=(tk_[A-Za-z0-9_.-]+)(?:\s|$)")


class TaskRequestParams(RequestParams):
    """Parameters shared by the extension's public task methods."""

    task_id: str = Field(alias="taskId", min_length=1)


def _iso8601(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _owner_key(ctx: Any) -> str:
    """Return a stable authorization boundary without trusting client metadata."""
    principal = authenticated_principal(ctx)
    if principal:
        digest = hashlib.sha256(principal.encode("utf-8")).hexdigest()
        return f"auth:{digest}"
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return f"local-user:{uid}"


def _task_id_from_result(result: Any) -> str | None:
    """Extract the internal ID only from thread-keeper's successful spawn result."""
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text")
        if not isinstance(text, str):
            continue
        match = _TASK_ID_RE.search(text)
        if match:
            return match.group(1)
    return None


def _task_result(internal_task_id: str, return_code: int | None) -> dict[str, Any]:
    """Build a bounded CallToolResult without exposing the internal task ID."""
    log_text = ""
    try:
        with open_spool_binary_read(TASK_LOG_DIR / f"{internal_task_id}.log") as log:
            log_text = log.read().decode("utf-8", errors="replace")
    except OSError:
        pass
    if len(log_text) > 16_000:
        log_text = log_text[-16_000:]
    if return_code in (None, 0):
        heading = "Agent work completed."
    else:
        heading = f"Agent work exited with code {return_code}."
    text = heading if not log_text.strip() else f"{heading}\n\n{log_text.strip()}"
    return {
        "content": [{"type": "text", "text": text}],
        "isError": return_code not in (None, 0),
    }


def _not_found() -> MCPError:
    """Do not reveal whether another caller owns a requested task ID."""
    return MCPError(code=INVALID_PARAMS, message="Unknown task")


class MCPTasksExtension(Extension):
    """Expose headless ``spawn`` calls through ``io.modelcontextprotocol/tasks``."""

    identifier = TASKS_EXTENSION_ID

    def methods(self) -> tuple[MethodBinding, ...]:
        versions = frozenset({LATEST_MODERN_VERSION})
        return (
            MethodBinding("tasks/get", TaskRequestParams, self.get_task, versions),
            MethodBinding("tasks/cancel", TaskRequestParams, self.cancel_task, versions),
        )

    async def intercept_tool_call(self, params, ctx, call_next):
        if not self._is_eligible_spawn(params, ctx):
            return await call_next(ctx)

        result = await call_next(ctx)
        internal_task_id = _task_id_from_result(result)
        if internal_task_id is None:
            return result
        return self._create_task(internal_task_id, _owner_key(ctx))

    @staticmethod
    def _is_eligible_spawn(params: CallToolRequestParams, ctx: Any) -> bool:
        if params.name != "spawn" or ctx.protocol_version != LATEST_MODERN_VERSION:
            return False
        capabilities = ctx.session.client_capabilities
        extensions = capabilities.extensions if capabilities else None
        if not extensions or TASKS_EXTENSION_ID not in extensions:
            return False
        arguments = params.arguments or {}
        # A visible Terminal.app child is not tied to a controllable process
        # group, so only durable, headless spawns use the standard task handle.
        return arguments.get("visible") is False

    def _create_task(self, internal_task_id: str, owner_key: str) -> dict[str, Any]:
        now = int(time.time())
        conn = get_db()
        row = conn.execute(
            "SELECT id FROM tasks WHERE id=?", (internal_task_id,)
        ).fetchone()
        if row is None:
            raise MCPError(code=INVALID_PARAMS, message="Spawn task was not recorded")
        for _ in range(3):
            task_id = secrets.token_urlsafe(32)
            try:
                conn.execute(
                    "INSERT INTO mcp_task_handles "
                    "(task_id, internal_task_id, owner_key, status, created_at, last_updated_at) "
                    "VALUES (?, ?, ?, 'working', ?, ?)",
                    (task_id, internal_task_id, owner_key, now, now),
                )
                conn.commit()
                return self._task_wire(
                    task_id=task_id,
                    status="working",
                    created_at=now,
                    last_updated_at=now,
                    status_message="Agent work is running.",
                    result_type="task",
                )
            except sqlite3.IntegrityError:
                conn.rollback()
        raise MCPError(code=INVALID_PARAMS, message="Could not create task handle")

    async def get_task(self, ctx, params: TaskRequestParams) -> dict[str, Any]:
        require_client_extension(ctx, TASKS_EXTENSION_ID)
        return self._read_task(params.task_id, _owner_key(ctx))

    async def cancel_task(self, ctx, params: TaskRequestParams) -> dict[str, Any]:
        require_client_extension(ctx, TASKS_EXTENSION_ID)
        self._request_cancel(params.task_id, _owner_key(ctx))
        return {}

    def _read_task(self, task_id: str, owner_key: str) -> dict[str, Any]:
        conn = get_db()
        self._refresh_internal_task(conn)
        row = self._owned_row(conn, task_id, owner_key)
        task = self._sync_status(conn, row)
        return self._task_wire(**task)

    @staticmethod
    def _refresh_internal_task(conn: sqlite3.Connection) -> None:
        from .tools.spawn import _refresh_tasks

        _refresh_tasks(conn)

    @staticmethod
    def _owned_row(conn: sqlite3.Connection, task_id: str, owner_key: str):
        row = conn.execute(
            "SELECT * FROM mcp_task_handles WHERE task_id=? AND owner_key=?",
            (task_id, owner_key),
        ).fetchone()
        if row is None:
            raise _not_found()
        return row

    def _sync_status(self, conn: sqlite3.Connection, row) -> dict[str, Any]:
        internal = conn.execute(
            "SELECT ended_at, return_code FROM tasks WHERE id=?",
            (row["internal_task_id"],),
        ).fetchone()
        if internal is None:
            return self._update_failed(conn, row, "Underlying task is no longer retained.")
        if row["status"] == "cancelled":
            return self._wire_values(row, status="cancelled")
        if internal["ended_at"] is None:
            message = (
                "Cancellation requested."
                if row["cancel_requested_at"] is not None
                else "Agent work is running."
            )
            return self._wire_values(row, status="working", status_message=message)
        if row["cancel_accepted_at"] is not None:
            return self._update_terminal(conn, row, "cancelled", "Agent work was cancelled.")
        result = row["result_json"]
        if result is None:
            result = json.dumps(
                _task_result(row["internal_task_id"], internal["return_code"]),
                separators=(",", ":"),
            )
        message = (
            "Agent work completed."
            if internal["return_code"] in (None, 0)
            else f"Agent work exited with code {internal['return_code']}."
        )
        return self._update_terminal(conn, row, "completed", message, result)

    def _update_failed(self, conn: sqlite3.Connection, row, message: str) -> dict[str, Any]:
        return self._update_terminal(
            conn,
            row,
            "failed",
            message,
            error={"code": -32603, "message": message},
        )

    def _update_terminal(
        self,
        conn: sqlite3.Connection,
        row,
        status: str,
        message: str,
        result_json: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if row["status"] != status or row["result_json"] != result_json:
            now = int(time.time())
            conn.execute(
                "UPDATE mcp_task_handles SET status=?, status_message=?, "
                "last_updated_at=?, result_json=COALESCE(?, result_json) WHERE task_id=?",
                (status, message, now, result_json, row["task_id"]),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM mcp_task_handles WHERE task_id=?", (row["task_id"],)
            ).fetchone()
        return self._wire_values(row, status=status, status_message=message, error=error)

    @staticmethod
    def _wire_values(row, **overrides: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "task_id": row["task_id"],
            "status": overrides.get("status", row["status"]),
            "created_at": row["created_at"],
            "last_updated_at": row["last_updated_at"],
            "status_message": overrides.get("status_message", row["status_message"]),
            "result_type": "complete",
        }
        if result["status"] == "completed":
            result["result"] = json.loads(row["result_json"] or "{}")
        if result["status"] == "failed":
            result["error"] = overrides.get("error") or {
                "code": -32603,
                "message": result["status_message"] or "Task failed.",
            }
        return result

    @staticmethod
    def _task_wire(
        *,
        task_id: str,
        status: str,
        created_at: int,
        last_updated_at: int,
        status_message: str | None,
        result_type: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "resultType": result_type,
            "taskId": task_id,
            "status": status,
            "createdAt": _iso8601(created_at),
            "lastUpdatedAt": _iso8601(last_updated_at),
            "ttlMs": None,
        }
        if status_message:
            wire["statusMessage"] = status_message
        if status == "working":
            wire["pollIntervalMs"] = POLL_INTERVAL_MS
        if result is not None:
            wire["result"] = result
        if error is not None:
            wire["error"] = error
        return wire

    def _request_cancel(self, task_id: str, owner_key: str) -> None:
        conn = get_db()
        row = self._owned_row(conn, task_id, owner_key)
        if row["cancel_requested_at"] is not None or row["status"] in {
            "completed", "cancelled", "failed",
        }:
            return
        now = int(time.time())
        from .tools.spawn import task_kill

        outcome = task_kill(row["internal_task_id"])
        accepted_at = now if outcome.startswith(("signal=", "already_dead")) else None
        conn.execute(
            "UPDATE mcp_task_handles SET cancel_requested_at=?, cancel_accepted_at=?, "
            "status_message=?, last_updated_at=? WHERE task_id=?",
            (now, accepted_at, "Cancellation requested.", now, task_id),
        )
        conn.commit()
