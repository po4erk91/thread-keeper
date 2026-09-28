"""MCP SDK compatibility and stdio-server smoke coverage.

The server keeps one narrow import adapter for MCP SDK 1.x and 2.x. This test
starts the real ``python -m threadkeeper.server`` process and uses the legacy
stdio handshake shared by both majors, then verifies the public tools registry
is available. CI runs it once per supported SDK-major/Python combination.
"""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read_response(process: subprocess.Popen[str], request_id: int) -> dict:
    """Read one JSON-RPC response without letting a wedged server hang pytest."""
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            events = selector.select(timeout=0.25)
            if not events:
                continue
            line = process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                return message
    finally:
        selector.close()

    stderr = ""
    if process.poll() is not None and process.stderr is not None:
        stderr = process.stderr.read()
    raise AssertionError(
        f"MCP server did not answer request id {request_id}; stderr={stderr!r}"
    )


def _send(process: subprocess.Popen[str], message: dict) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def test_stdio_server_starts_and_lists_tools_under_active_mcp_sdk(tmp_path):
    """The real server starts and serves the unchanged tool registry over stdio."""
    home = tmp_path / "home"
    projects = tmp_path / "projects"
    home.mkdir()
    projects.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "CLAUDE_PROJECTS_DIR": str(projects),
            "THREADKEEPER_DB": str(tmp_path / "threadkeeper.sqlite"),
            "THREADKEEPER_NO_EMBEDDINGS": "1",
            "THREADKEEPER_DISABLE_BG_DAEMONS": "1",
            "THREADKEEPER_AUTO_UPDATE_INTERVAL_S": "0",
            "THREADKEEPER_MENUBAR_AUTO_LAUNCH": "0",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "threadkeeper.server"],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "threadkeeper-test", "version": "1"},
                },
            },
        )
        initialized = _read_response(process, 1)
        assert "result" in initialized, initialized

        _send(
            process,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        _send(
            process,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        listed = _read_response(process, 2)
        assert "result" in listed, listed
        tools = listed["result"]["tools"]
        tool_names = {tool["name"] for tool in tools}
        assert {"brief", "context", "dialectic_supersede"} <= tool_names
        context_tool = next(tool for tool in tools if tool["name"] == "context")
        assert context_tool["annotations"]["readOnlyHint"] is True
        assert "outputSchema" in context_tool

        _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "context", "arguments": {}},
            },
        )
        called = _read_response(process, 3)
        assert "result" in called, called
        assert called["result"]["structuredContent"]
        assert any(
            content.get("type") == "text" and content.get("text")
            for content in called["result"]["content"]
        )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
