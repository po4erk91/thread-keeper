"""MCP initialization guidance and managed-instructions drift coverage."""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _send(process: subprocess.Popen[str], message: dict) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def _read_response(process: subprocess.Popen[str], request_id: int) -> dict:
    """Read one JSON-RPC response without allowing a wedged server to hang."""
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if not selector.select(timeout=0.25):
                continue
            line = process.stdout.readline()
            if not line:
                break
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if response.get("id") == request_id:
                return response
    finally:
        selector.close()

    stderr = process.stderr.read() if process.stderr is not None else ""
    raise AssertionError(
        f"MCP server did not answer request {request_id}; stderr={stderr!r}"
    )


def test_server_advertises_static_lifecycle_guidance_over_stdio(tmp_path):
    """The initialize result has instructions without reading user state."""
    home = tmp_path / "home"
    projects = tmp_path / "projects"
    home.mkdir()
    projects.mkdir()
    env = os.environ | {
        "HOME": str(home),
        "CLAUDE_PROJECTS_DIR": str(projects),
        "THREADKEEPER_DB": str(tmp_path / "threadkeeper.sqlite"),
        "THREADKEEPER_NO_EMBEDDINGS": "1",
        "THREADKEEPER_DISABLE_BG_DAEMONS": "1",
        "THREADKEEPER_AUTO_UPDATE_INTERVAL_S": "0",
        "THREADKEEPER_MENUBAR_AUTO_LAUNCH": "0",
        "PYTHONPATH": str(ROOT),
    }
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
        response = _read_response(process, 1)
        instructions = response["result"]["instructions"]

        from threadkeeper.instructions import SERVER_INSTRUCTIONS

        assert instructions == SERVER_INSTRUCTIONS
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def test_setup_and_mcp_share_every_lifecycle_fragment():
    """The setup block cannot silently drift from the MCP protocol text."""
    from threadkeeper._setup import CLAUDE_MD_BLOCK
    from threadkeeper.instructions import (
        LIFECYCLE_GUIDANCE,
        SERVER_INSTRUCTIONS,
        SERVER_INSTRUCTIONS_MAX_CHARS,
    )

    assert SERVER_INSTRUCTIONS
    assert len(SERVER_INSTRUCTIONS) <= SERVER_INSTRUCTIONS_MAX_CHARS
    assert "~" not in SERVER_INSTRUCTIONS
    assert all(fragment in SERVER_INSTRUCTIONS for fragment in LIFECYCLE_GUIDANCE)
    assert all(fragment in CLAUDE_MD_BLOCK for fragment in LIFECYCLE_GUIDANCE)


def test_setup_skips_clients_without_a_global_instruction_file(monkeypatch):
    """Server instructions cover hosts setup cannot reinforce with a file."""
    import threadkeeper.adapters as adapters
    from threadkeeper import _setup

    class NoInstructionFileAdapter:
        name = "hostless"

        def instructions_path(self):
            return None

    monkeypatch.setattr(
        adapters, "installed_adapters", lambda: [NoInstructionFileAdapter()]
    )

    assert _setup.install_instructions(dry_run=True) == [
        "instructions[hostless]: no global file (skip)"
    ]
