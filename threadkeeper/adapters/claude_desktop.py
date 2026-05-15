"""Claude Desktop adapter.

Claude Desktop is the Electron app — distinct from Claude Code (the CLI).
The two share a vendor but not a config or transcript location:

  * Claude Code (CLI):  ~/.claude.json , ~/.claude/projects/**/*.jsonl
  * Claude Desktop:     ~/Library/Application Support/Claude/
                        claude_desktop_config.json on macOS;
                        %APPDATA%/Claude/... on Windows;
                        ~/.config/Claude/... on Linux.

Config shape mirrors Gemini/Copilot:

    {"mcpServers": {"<name>": {"command": "...", "args": [...], "env": {...}}}}

Claude Desktop has no shell-style hook mechanism and no global per-user
instructions file analogous to ~/.claude/CLAUDE.md (style + memory live
inside the app's GUI settings, not on disk). Conversations are stored
in Electron's IndexedDB (a leveldb on disk), which is fragile to parse
without browser tooling — we skip transcript ingest. MCP registration
alone gets thread-keeper's tools available inside Claude Desktop chats,
which is the integration users actually ask for.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Iterator

from .base import CLIAdapter, NormalizedMessage


def _default_config_path() -> Path:
    """Per-OS default location for claude_desktop_config.json.

    Overridable via CLAUDE_DESKTOP_CONFIG env var (used by tests)."""
    env = os.environ.get("CLAUDE_DESKTOP_CONFIG")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path(
            "~/Library/Application Support/Claude/claude_desktop_config.json"
        ).expanduser()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or "~/AppData/Roaming"
        return Path(appdata).expanduser() / "Claude" / "claude_desktop_config.json"
    # linux / freebsd / others — follow XDG-ish convention used by other
    # Electron apps shipped under "Claude".
    return Path("~/.config/Claude/claude_desktop_config.json").expanduser()


def _app_bundle_present() -> bool:
    """On macOS, detect Claude Desktop without requiring its config file
    to exist yet (fresh install hasn't launched once)."""
    if sys.platform == "darwin":
        return Path("/Applications/Claude.app").exists()
    return False


class ClaudeDesktopAdapter(CLIAdapter):
    name = "claude-desktop"

    def __init__(self) -> None:
        self.config_path = _default_config_path()

    # ----------------------------- detection -----------------------------
    def is_installed(self) -> bool:
        return self.config_path.exists() or _app_bundle_present()

    # ----------------------------- mcp -----------------------------------
    def register_mcp_server(
        self, name, command, args, env, dry_run=False
    ) -> str:
        cfg: dict
        if self.config_path.exists():
            try:
                cfg = json.loads(self.config_path.read_text())
            except json.JSONDecodeError:
                return "claude-desktop: malformed config — refused"
        else:
            cfg = {}
        servers = cfg.setdefault("mcpServers", {})
        entry: dict = {
            "command": command,
            "args": list(args),
        }
        if env:
            entry["env"] = dict(env)
        existing = servers.get(name)
        if existing == entry:
            return "claude-desktop: already current"
        servers[name] = entry
        if not dry_run:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(cfg, indent=2))
        return f"claude-desktop: {'would ' if dry_run else ''}{'update' if existing else 'add'}"

    def unregister_mcp_server(self, name, dry_run=False) -> str:
        if not self.config_path.exists():
            return "claude-desktop: nothing to remove"
        try:
            cfg = json.loads(self.config_path.read_text())
        except json.JSONDecodeError:
            return "claude-desktop: malformed config — refused"
        servers = (cfg.get("mcpServers") or {})
        if name not in servers:
            return "claude-desktop: not present"
        if dry_run:
            return f"claude-desktop: would remove {name}"
        servers.pop(name)
        self.config_path.write_text(json.dumps(cfg, indent=2))
        return f"claude-desktop: removed {name}"

    # ----------------------------- transcripts ---------------------------
    # Claude Desktop stores chats inside Electron IndexedDB (leveldb on
    # disk). Parsing that without Chromium/Electron tooling is brittle,
    # so we don't expose any transcripts here — MCP registration alone is
    # the win. dialog_search() across other CLIs still works normally.
    def transcript_files(self) -> list[Path]:
        return []

    def iter_messages(self, fp: Path) -> Iterator[NormalizedMessage]:
        return iter(())


ADAPTER = ClaudeDesktopAdapter()
