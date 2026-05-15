"""CLI adapter registry.

thread-keeper is CLI-agnostic: it can attach to any agent CLI that
(a) supports MCP servers in its config and (b) writes conversation
history to disk in a parseable format. Each supported CLI has its
own adapter under this package, and the registry below enumerates
them in load order.

To add support for a new CLI:
  1. Create `threadkeeper/adapters/<name>.py` exporting `ADAPTER`
     (an instance of CLIAdapter).
  2. Append it to `ADAPTERS` below.
  3. That's it. ingest, _setup, and brief will pick it up.
"""
from __future__ import annotations

from .base import CLIAdapter, NormalizedMessage
from .claude_code import ADAPTER as _CLAUDE_CODE
from .claude_desktop import ADAPTER as _CLAUDE_DESKTOP
from .codex import ADAPTER as _CODEX
from .gemini import ADAPTER as _GEMINI
from .copilot import ADAPTER as _COPILOT
from .vscode import ADAPTER as _VSCODE

ADAPTERS: list[CLIAdapter] = [
    _CLAUDE_CODE,
    _CLAUDE_DESKTOP,
    _CODEX,
    _GEMINI,
    _COPILOT,
    _VSCODE,
]


def installed_adapters() -> list[CLIAdapter]:
    """Return adapters whose CLI is detected on this machine."""
    return [a for a in ADAPTERS if a.is_installed()]


__all__ = ["CLIAdapter", "NormalizedMessage", "ADAPTERS", "installed_adapters"]
