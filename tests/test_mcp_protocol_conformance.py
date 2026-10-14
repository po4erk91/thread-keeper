"""Wire-level conformance for the supported MCP protocol eras."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest
from mcp.server.context import ServerRequestContext
from mcp.server.request_state import RequestStateBoundary, RequestStateSecurity
from mcp.shared.exceptions import MCPError
from mcp.types import InputRequiredResult

from threadkeeper.protocol import (
    LEGACY_PROTOCOL_VERSION,
    MODERN_PROTOCOL_VERSION,
    caller_principal,
)


ROOT = Path(__file__).resolve().parents[1]


class StdioMcp:
    """Small raw JSON-RPC client so each assertion exercises stdio wire shapes."""

    def __init__(self, db_path: Path) -> None:
        env = os.environ | {
            "PYTHONPATH": str(ROOT),
            "THREADKEEPER_DB": str(db_path),
            "THREADKEEPER_DISABLE_BG_DAEMONS": "1",
            "THREADKEEPER_NO_EMBEDDINGS": "1",
            "THREADKEEPER_MENUBAR_AUTO_LAUNCH": "0",
        }
        self.process = subprocess.Popen(
            [sys.executable, "-m", "threadkeeper.server"],
            cwd=ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.responses: queue.Queue[dict] = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.responses.put(json.loads(line))

    def request(self, request_id: int, method: str, params: dict) -> dict:
        assert self.process.stdin is not None
        self.process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            + "\n"
        )
        self.process.stdin.flush()
        while True:
            response = self.responses.get(timeout=10)
            if response.get("id") == request_id:
                return response

    def notify(self, method: str, params: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
        self.process.stdin.flush()

    def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


@pytest.fixture()
def stdio_mcp(tmp_path):
    client = StdioMcp(tmp_path / "protocol.sqlite")
    try:
        yield client, tmp_path / "protocol.sqlite"
    finally:
        client.close()


def _modern_meta(client_name: str, capabilities: dict | None = None) -> dict:
    return {
        "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": {"name": client_name, "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": capabilities or {},
    }


def test_legacy_stdio_initialize_discovery_and_core_paths(stdio_mcp):
    client, _ = stdio_mcp
    initialize = client.request(
        1,
        "initialize",
        {
            "protocolVersion": LEGACY_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "legacy-conformance", "version": "1"},
        },
    )
    assert initialize["result"]["protocolVersion"] == LEGACY_PROTOCOL_VERSION
    client.notify("notifications/initialized", {})

    tools = client.request(2, "tools/list", {})["result"]
    resources = client.request(3, "resources/list", {})["result"]
    prompts = client.request(4, "prompts/list", {})["result"]
    resource = client.request(5, "resources/read", {"uri": "memory://context"})["result"]
    called = client.request(6, "tools/call", {"name": "open_thread", "arguments": {"question": "legacy"}})

    assert "ttlMs" not in tools  # 2026 cache hints never leak onto legacy wire.
    assert any(tool["name"] == "open_thread" for tool in tools["tools"])
    assert any(item["uri"] == "memory://context" for item in resources["resources"])
    assert any(item["name"] == "review_recent_threads" for item in prompts["prompts"])
    assert resource["contents"]
    assert called["result"]["isError"] is False

    modern_only = client.request(7, "server/discover", {})
    assert modern_only["error"]["code"] == -32601


def test_modern_stdio_discovery_catalogs_and_replay_are_stable(stdio_mcp):
    client, db_path = stdio_mcp
    meta = _modern_meta("modern-conformance")
    discover = client.request(1, "server/discover", {"_meta": meta})["result"]
    assert discover["supportedVersions"] == [MODERN_PROTOCOL_VERSION]
    assert discover["ttlMs"] == 3_600_000
    assert discover["cacheScope"] == "public"

    first_catalogs = [
        client.request(2, "tools/list", {"_meta": meta})["result"],
        client.request(3, "resources/list", {"_meta": meta})["result"],
        client.request(4, "prompts/list", {"_meta": meta})["result"],
    ]
    second_catalogs = [
        client.request(5, "tools/list", {"_meta": meta})["result"],
        client.request(6, "resources/list", {"_meta": meta})["result"],
        client.request(7, "prompts/list", {"_meta": meta})["result"],
    ]
    assert first_catalogs == second_catalogs
    assert all(catalog["ttlMs"] == 3_600_000 for catalog in first_catalogs)
    assert all(catalog["cacheScope"] == "public" for catalog in first_catalogs)

    resource = client.request(8, "resources/read", {"uri": "memory://context", "_meta": meta})
    assert resource["result"]["contents"]

    params = {"name": "open_thread", "arguments": {"question": "modern once"}, "_meta": meta}
    first = client.request(9, "tools/call", params)
    replay = client.request(9, "tools/call", params)
    assert replay["result"] == first["result"]

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM threads WHERE question='modern once'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM mcp_replay_ledger").fetchone()[0] == 1


def _context(meta: dict) -> ServerRequestContext:
    session = SimpleNamespace(client_params=None, client_capabilities=None)
    return ServerRequestContext(
        session=session,
        lifespan_context={},
        protocol_version=MODERN_PROTOCOL_VERSION,
        method="tools/call",
        params={"name": "open_thread", "arguments": {"question": "bound"}},
        meta=meta,
    )


def test_sealed_request_state_is_integrity_checked_caller_bound_and_expires():
    security = RequestStateSecurity(
        keys=[b"a" * 32],
        ttl=0.01,
        bind_principal=caller_principal,
    )
    boundary = RequestStateBoundary(security, default_audience="thread-keeper")
    first_context = _context(_modern_meta("caller-a", {"elicitation": {}}))

    async def issue(_):
        return InputRequiredResult(request_state="continuation")

    async def exercise():
        issued = await boundary(first_context, issue)
        sealed = issued.request_state
        assert sealed and sealed != "continuation"

        async def resume(ctx):
            assert ctx.params["requestState"] == "continuation"
            return {"ok": True}

        resumed = await boundary(
            replace(first_context, params={**first_context.params, "requestState": sealed}), resume
        )
        assert resumed == {"ok": True}

        with pytest.raises(MCPError):
            await boundary(
                replace(
                    first_context,
                    params={**first_context.params, "requestState": sealed[:-1] + ("0" if sealed[-1] != "0" else "1")},
                ),
                resume,
            )
        with pytest.raises(MCPError):
            await boundary(
                replace(
                    _context(_modern_meta("caller-a")),
                    params={**first_context.params, "requestState": sealed},
                ),
                resume,
            )

        time.sleep(0.03)
        with pytest.raises(MCPError):
            await boundary(
                replace(first_context, params={**first_context.params, "requestState": sealed}), resume
            )

    asyncio.run(exercise())
