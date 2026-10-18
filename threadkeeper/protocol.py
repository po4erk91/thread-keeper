"""MCP era adapter and replay guard.

The SDK owns protocol parsing and routes both the legacy initialize handshake
and the 2026 per-request envelope into ``ServerRequestContext``. This module
normalizes the facts thread-keeper needs from either shape. Business tools stay
unaware of protocol versions; only the transport boundary uses this adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
)
from pydantic import BaseModel

from .db import run_write


LEGACY_PROTOCOL_VERSION = "2025-11-25"
MODERN_PROTOCOL_VERSION = "2026-07-28"


@dataclass(frozen=True)
class RequestContext:
    """Protocol-neutral request facts used by boundary-only policy."""

    protocol_version: str
    caller_id: str
    capabilities: dict[str, Any]

    @property
    def is_modern(self) -> bool:
        return self.protocol_version == MODERN_PROTOCOL_VERSION


def _json(value: Any) -> str:
    """Canonical, total JSON for untrusted metadata and request arguments."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _legacy_client(ctx: ServerRequestContext[Any, Any]) -> tuple[Any, Any]:
    params = getattr(ctx.session, "client_params", None)
    if params is None:
        return None, getattr(ctx.session, "client_capabilities", None)
    return getattr(params, "client_info", None), getattr(params, "capabilities", None)


def request_context(ctx: ServerRequestContext[Any, Any]) -> RequestContext:
    """Normalize client identity/capabilities from this request, not globals.

    Modern MCP places both values in every request's ``_meta`` envelope. The
    legacy fallback reads the connection's completed initialize request. An
    anonymous envelope still receives a stable fingerprint of its declared
    shape, which lets the server reject a handle replayed by a different
    envelope; deployments requiring stronger identity should use MCP auth.
    """
    meta = getattr(ctx, "meta", None) or {}
    client_info = meta.get(CLIENT_INFO_META_KEY)
    capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
    if client_info is None and capabilities is None:
        client_info, capabilities = _legacy_client(ctx)
    if isinstance(client_info, BaseModel):
        client_info = client_info.model_dump(by_alias=True, mode="json")
    if isinstance(capabilities, BaseModel):
        capabilities = capabilities.model_dump(by_alias=True, mode="json")
    normalized_capabilities = capabilities if isinstance(capabilities, dict) else {}
    identity = _json({"clientInfo": client_info, "capabilities": normalized_capabilities})
    caller_id = hashlib.sha256(identity.encode()).hexdigest()
    return RequestContext(
        protocol_version=ctx.protocol_version,
        caller_id=caller_id,
        capabilities=normalized_capabilities,
    )


def caller_principal(ctx: ServerRequestContext[Any, Any]) -> str:
    """Bind SDK-sealed requestState handles to the normalized caller."""
    return request_context(ctx).caller_id


def _arguments_hash(arguments: dict[str, Any] | None) -> str:
    return hashlib.sha256(_json(arguments or {}).encode()).hexdigest()


def _replay_result(text: str) -> dict[str, Any]:
    return json.loads(text)


class ReplaySafeWrites:
    """Make re-entered modern writes once-only or safely reject ambiguity.

    JSON-RPC ids are a request handle in the 2026 stateless model. The ledger
    scopes each handle to the normalized caller and stores a digest of its tool
    and arguments. A completed replay returns its original result. A request
    that is still claimed (for example after a process died after a write) is
    rejected rather than executing an uncertain mutation again.
    """

    def __init__(self, write_tools: set[str]) -> None:
        self._write_tools = write_tools

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        request = request_context(ctx)
        raw = ctx.params or {}
        if ctx.method != "tools/call" or not request.is_modern:
            return await call_next(ctx)
        tool_name = raw.get("name")
        arguments = raw.get("arguments")
        # Let the SDK return its normal validation error for malformed calls;
        # only valid-looking write requests acquire a replay handle.
        if not isinstance(tool_name, str) or tool_name not in self._write_tools:
            return await call_next(ctx)
        if arguments is not None and not isinstance(arguments, dict):
            return await call_next(ctx)
        if ctx.request_id is None:
            raise MCPError(code=-32600, message="modern write requests require a JSON-RPC id")

        request_id = str(ctx.request_id)
        arguments_hash = _arguments_hash(arguments)
        now = int(time.time())

        def claim(conn):
            row = conn.execute(
                "SELECT tool_name, arguments_hash, response_json "
                "FROM mcp_replay_ledger WHERE caller_id=? AND request_id=?",
                (request.caller_id, request_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO mcp_replay_ledger "
                    "(caller_id, request_id, tool_name, arguments_hash, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (request.caller_id, request_id, tool_name, arguments_hash, now),
                )
                return "claimed", None
            if row["tool_name"] != tool_name or row["arguments_hash"] != arguments_hash:
                return "mismatch", None
            if row["response_json"] is None:
                return "unsafe", None
            return "replay", row["response_json"]

        state, stored = run_write("mcp_replay_claim", claim)
        if state == "replay":
            return _replay_result(stored)
        if state == "mismatch":
            raise MCPError(code=-32602, message="request id was already used with different tool arguments")
        if state == "unsafe":
            raise MCPError(code=-32602, message="unsafe replay rejected; original write outcome is unknown")

        result = await call_next(ctx)
        # Input-required results are not terminal and must not be cached. Drop
        # this provisional reservation so the SDK's sealed requestState can
        # carry the caller-bound continuation into its next round.
        if not isinstance(result, dict) or result.get("resultType") == "input_required":
            def release_claim(conn):
                conn.execute(
                    "DELETE FROM mcp_replay_ledger "
                    "WHERE caller_id=? AND request_id=? AND response_json IS NULL",
                    (request.caller_id, request_id),
                )

            run_write("mcp_replay_release", release_claim)
            return result
        serialized = _json(result)

        def complete(conn):
            conn.execute(
                "UPDATE mcp_replay_ledger SET response_json=?, completed_at=? "
                "WHERE caller_id=? AND request_id=? AND response_json IS NULL",
                (serialized, int(time.time()), request.caller_id, request_id),
            )

        run_write("mcp_replay_complete", complete)
        return result
