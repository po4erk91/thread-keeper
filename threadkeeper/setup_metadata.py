"""Server registration metadata shared by setup and portable packaging."""
from __future__ import annotations

from pathlib import Path

SERVER_NAME = "thread-keeper"
SERVER_ARGS = ("-m", "threadkeeper.server")


def server_environment() -> dict[str, str]:
    return {
        # Keep a client's working checkout off Python's import path.
        "PYTHONSAFEPATH": "1",
        "THREADKEEPER_TOOL_PROFILE": "core",
    }


def installed_server(command: str, repo_root: Path) -> dict:
    return {
        "name": SERVER_NAME,
        "command": command,
        "args": list(SERVER_ARGS),
        "env": {"PYTHONPATH": str(repo_root), **server_environment()},
    }


def portable_server(version: str) -> dict:
    return {
        "type": "stdio",
        "command": "uvx",
        "args": [
            "--from", f"threadkeeper[semantic]=={version}",
            "python", *SERVER_ARGS,
        ],
        "env": {
            **server_environment(),
            "PYTHONPATH": "",
            # The host upgrades the pinned plugin and its server together.
            "THREADKEEPER_AUTO_UPDATE_INTERVAL_S": "0",
        },
    }
