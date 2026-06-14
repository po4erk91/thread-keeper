"""VS Code adapter — registers thread-keeper in VS Code's user-level
MCP host (Code 1.93+).

Modern VS Code ships built-in MCP host support, consumed by every
MCP-aware extension running in the same window: GitHub Copilot Chat,
the Claude IDE extension, the OpenAI Codex extension, Continue, Cline,
and others. All of them read the same user-level file:

    macOS:   ~/Library/Application Support/Code/User/mcp.json
    Linux:   ~/.config/Code/User/mcp.json
    Windows: %APPDATA%/Code/User/mcp.json

Schema (note `servers`, NOT `mcpServers` — VS Code chose its own key):

    {
      "inputs":  [ … prompt-string inputs for secrets … ],
      "servers": {
        "<name>": {
          "type":    "stdio" | "http" | "sse",
          "command": "...",
          "args":    [...],
          "env":     {...}
        }
      }
    }

Hooks: VS Code has no SessionStart-style shell hook the way Claude Code
or Gemini legacy does. Instructions file: per-workspace `.github/instructions/*`
or `copilot-instructions.md`, not a single global file we can write. So
this adapter is MCP-registration-only — no hooks, no instructions, no
transcript ingest.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Iterator

from .base import CLIAdapter, NormalizedMessage


def _default_config_path() -> Path:
    """Per-OS default location for VS Code's user-level mcp.json.

    Overridable via VSCODE_MCP_JSON env var (used by tests)."""
    env = os.environ.get("VSCODE_MCP_JSON")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path(
            "~/Library/Application Support/Code/User/mcp.json"
        ).expanduser()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or "~/AppData/Roaming"
        return Path(appdata).expanduser() / "Code" / "User" / "mcp.json"
    # linux / freebsd / etc.
    return Path("~/.config/Code/User/mcp.json").expanduser()


def _vscode_user_dir() -> Path:
    """The User/ directory itself — used to probe install presence even
    when mcp.json hasn't been created yet."""
    return _default_config_path().parent


class VSCodeAdapter(CLIAdapter):
    name = "vscode"

    def __init__(self) -> None:
        self.config_path = _default_config_path()

    # ----------------------------- detection -----------------------------
    def is_installed(self) -> bool:
        # mcp.json already exists OR VS Code is on disk in some form
        # (User/ profile dir, .app bundle, `code` on PATH).
        if self.config_path.exists() or _vscode_user_dir().exists():
            return True
        if sys.platform == "darwin":
            if Path("/Applications/Visual Studio Code.app").exists():
                return True
        return shutil.which("code") is not None

    # ----------------------------- mcp -----------------------------------
    def register_mcp_server(
        self, name, command, args, env, dry_run=False
    ) -> str:
        cfg: dict
        if self.config_path.exists():
            try:
                cfg = json.loads(self.config_path.read_text())
            except json.JSONDecodeError:
                return "vscode: malformed mcp.json — refused"
        else:
            cfg = {}
        servers = cfg.setdefault("servers", {})
        entry: dict = {
            "type": "stdio",
            "command": command,
            "args": list(args),
        }
        if env:
            entry["env"] = dict(env)
        existing = servers.get(name)
        if existing == entry:
            return "vscode: already current"
        servers[name] = entry
        if not dry_run:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(cfg, indent=2))
        return f"vscode: {'would ' if dry_run else ''}{'update' if existing else 'add'}"

    def unregister_mcp_server(self, name, dry_run=False) -> str:
        if not self.config_path.exists():
            return "vscode: nothing to remove"
        try:
            cfg = json.loads(self.config_path.read_text())
        except json.JSONDecodeError:
            return "vscode: malformed mcp.json — refused"
        servers = (cfg.get("servers") or {})
        if name not in servers:
            return "vscode: not present"
        if dry_run:
            return f"vscode: would remove {name}"
        servers.pop(name)
        self.config_path.write_text(json.dumps(cfg, indent=2))
        return f"vscode: removed {name}"

    # ----------------------------- transcripts ---------------------------
    # VS Code itself doesn't keep a unified MCP transcript on disk —
    # individual extensions handle chat history their own way (Copilot
    # via GitHub services; Claude IDE via ~/.claude/projects; etc.).
    # The Claude-IDE side is already covered by the claude_code adapter,
    # so there's nothing for the vscode adapter to ingest on top.
    def transcript_files(self) -> list[Path]:
        return []

    def iter_messages(self, fp: Path) -> Iterator[NormalizedMessage]:
        return iter(())


ADAPTER = VSCodeAdapter()
