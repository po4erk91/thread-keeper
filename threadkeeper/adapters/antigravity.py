"""Google Antigravity CLI (agy) adapter.

Antigravity CLI is the successor path for consumer Gemini CLI users.

Config/customizations:
  ~/.gemini/config/mcp_config.json   MCP servers
  ~/.gemini/config/AGENTS.md         global rules/instructions
  ~/.gemini/config/skills/           global skills/customizations

Transcripts:
  Antigravity CLI 1.0.x stores conversations as sqlite/protobuf under
  ~/.gemini/antigravity-cli/conversations/*.db. thread-keeper does not
  parse that format yet, so this adapter registers MCP/instructions and
  supports headless spawn, but contributes no dialog ingest for now.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Iterator

from .base import CLIAdapter, NormalizedMessage


class AntigravityAdapter(CLIAdapter):
    name = "antigravity"

    def __init__(self) -> None:
        self.config_root = Path("~/.gemini/config").expanduser()
        self.config_path = self.config_root / "mcp_config.json"
        self._instructions = self.config_root / "AGENTS.md"
        self._skills_dir = self.config_root / "skills"
        self.conversations_root = Path(
            "~/.gemini/antigravity-cli/conversations"
        ).expanduser()

    def instructions_path(self):
        return self._instructions

    def skills_dir(self):
        return self._skills_dir

    def supports_spawn(self) -> bool:
        return True

    def spawn_argv(self, prompt, *, model="", permission_mode="auto",
                   extra_allowed_tools="", mcp_config_path=None):
        """Antigravity non-interactive: `agy -p <prompt> [--model X]`.

        Antigravity reads MCP servers from ~/.gemini/config/mcp_config.json,
        which thread-keeper-setup wires up.
        """
        bin_path = shutil.which("agy") or shutil.which("antigravity")
        if not bin_path:
            return None
        argv = [bin_path, "-p", prompt]
        if model:
            argv += ["--model", model]
        if permission_mode == "bypassPermissions":
            argv.append("--dangerously-skip-permissions")
        return argv

    def is_installed(self) -> bool:
        if (
            self.config_root.exists()
            or self.conversations_root.exists()
            or Path("~/.gemini/antigravity-cli").expanduser().exists()
        ):
            return True
        return (
            shutil.which("agy") is not None
            or shutil.which("antigravity") is not None
        )

    # ----- MCP registration ---------------------------------------------
    def _read_config(self) -> dict:
        if not self.config_path.exists():
            return {}
        raw = self.config_path.read_text().strip()
        if not raw:
            return {}
        return json.loads(raw)

    def register_mcp_server(
        self, name, command, args, env, dry_run=False
    ) -> str:
        try:
            cfg = self._read_config()
        except json.JSONDecodeError:
            return "antigravity: malformed mcp_config.json — refused"
        servers = cfg.setdefault("mcpServers", {})
        entry = {
            "command": command,
            "args": list(args),
        }
        if env:
            entry["env"] = dict(env)
        existing = servers.get(name)
        if existing == entry:
            return "antigravity: already current"
        servers[name] = entry
        if not dry_run:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(cfg, indent=2))
        return f"antigravity: {'would ' if dry_run else ''}{'update' if existing else 'add'}"

    def unregister_mcp_server(self, name, dry_run=False) -> str:
        if not self.config_path.exists():
            return "antigravity: nothing to remove"
        try:
            cfg = self._read_config()
        except json.JSONDecodeError:
            return "antigravity: malformed mcp_config.json — refused"
        servers = (cfg.get("mcpServers") or {})
        if name not in servers:
            return "antigravity: not present"
        if dry_run:
            return f"antigravity: would remove {name}"
        servers.pop(name)
        self.config_path.write_text(json.dumps(cfg, indent=2))
        return f"antigravity: removed {name}"

    # ----- Transcript ingestion -----------------------------------------
    def session_dir(self):
        return self.conversations_root

    def transcript_files(self) -> list[Path]:
        return []

    def iter_messages(self, fp: Path) -> Iterator[NormalizedMessage]:
        if False:
            yield  # pragma: no cover
        return


ADAPTER = AntigravityAdapter()
