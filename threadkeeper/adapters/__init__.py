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
from .antigravity import ADAPTER as _ANTIGRAVITY
from .copilot import ADAPTER as _COPILOT
from .vscode import ADAPTER as _VSCODE

ADAPTERS: list[CLIAdapter] = [
    _CLAUDE_CODE,
    _CLAUDE_DESKTOP,
    _CODEX,
    _ANTIGRAVITY,
    _COPILOT,
    _VSCODE,
]


def installed_adapters() -> list[CLIAdapter]:
    """Return adapters whose CLI is detected on this machine."""
    return [a for a in ADAPTERS if a.is_installed()]


def get_adapter(name: str) -> CLIAdapter | None:
    """Lookup adapter by short name ('claude' / 'codex' / 'antigravity' /
    'agy' / 'copilot' / 'claude-desktop' / 'vscode').
    Returns None on unknown name. Used by spawn() dispatcher and the
    startup validator."""
    aliases = {
        "claude": "claude-code",      # short name for the spawn adapter
        "claude-code": "claude-code",
        "claude-desktop": "claude-desktop",
        "codex": "codex",
        "antigravity": "antigravity",
        "agy": "antigravity",
        "copilot": "copilot",
        "vscode": "vscode",
    }
    canonical = aliases.get(name.strip().lower())
    if not canonical:
        return None
    for a in ADAPTERS:
        if a.name == canonical:
            return a
    return None


__all__ = [
    "CLIAdapter", "NormalizedMessage", "ADAPTERS",
    "installed_adapters", "get_adapter",
]
