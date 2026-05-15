"""Claude Code adapter.

Claude Code stores conversation transcripts as JSONL files under
~/.claude/projects/<slug>/<conversation-id>.jsonl. MCP servers are
registered in ~/.claude.json under "mcpServers".
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .base import CLIAdapter, NormalizedMessage


def _ts(s: str) -> int:
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        import time
        return int(time.time())


def _extract_text(msg: dict) -> str:
    """Pull searchable text from a message; skip tool_use args,
    cap tool_results. Matches the legacy behavior pre-adapter."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text":
            parts.append(block.get("text", ""))
        elif t == "thinking":
            parts.append(f"[thinking] {block.get('thinking', '')}")
        elif t == "tool_result":
            tr = block.get("content", "")
            if isinstance(tr, list):
                tr = " ".join(b.get("text", "") for b in tr if isinstance(b, dict))
            if isinstance(tr, str) and tr:
                parts.append(f"[tool_result] {tr[:800]}")
    return "\n".join(p for p in parts if p)


class ClaudeCodeAdapter(CLIAdapter):
    name = "claude-code"

    def __init__(self) -> None:
        self.projects_dir = Path(
            os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects")
        ).expanduser()
        self.config_path = Path("~/.claude.json").expanduser()
        self._instructions = Path("~/.claude/CLAUDE.md").expanduser()
        # Hooks live in the same settings.json that controls other
        # editor preferences. Each entry under "hooks" is keyed by event
        # name (SessionStart, PostToolUse, ...).
        self._settings_path = Path("~/.claude/settings.json").expanduser()
        # Claude auto-discovers SKILL.md files under this directory via
        # frontmatter description scanning at session start. The canonical
        # Anthropic skills format.
        self._skills_dir = Path(
            os.environ.get("CLAUDE_SKILLS_DIR", "~/.claude/skills")
        ).expanduser()

    def skills_dir(self):
        return self._skills_dir

    def instructions_path(self):
        return self._instructions

    def hooks_supported(self) -> bool:
        return True

    def register_hooks(self, specs, dry_run=False) -> str:
        from ._hook_helpers import install_claude_style_hooks
        return install_claude_style_hooks(
            self._settings_path, specs, dry_run=dry_run,
        )

    # ----------------------------- detection -----------------------------
    def is_installed(self) -> bool:
        # Either the projects dir exists (user has used Claude Code at
        # least once) OR the executable is on PATH.
        if self.projects_dir.exists():
            return True
        return shutil.which("claude") is not None

    # ----------------------------- mcp -----------------------------------
    def register_mcp_server(
        self, name, command, args, env, dry_run=False
    ) -> str:
        cfg: dict
        if self.config_path.exists():
            try:
                cfg = json.loads(self.config_path.read_text())
            except json.JSONDecodeError:
                return "claude-code: malformed ~/.claude.json — refused"
        else:
            cfg = {}
        servers = cfg.setdefault("mcpServers", {})
        entry = {
            "type": "stdio",
            "command": command,
            "args": list(args),
            "env": dict(env),
        }
        existing = servers.get(name)
        if existing == entry:
            return "claude-code: already current"
        servers[name] = entry
        if not dry_run:
            self.config_path.write_text(json.dumps(cfg, indent=2))
        return f"claude-code: {'would ' if dry_run else ''}{'update' if existing else 'add'}"

    def unregister_mcp_server(self, name, dry_run=False) -> str:
        if not self.config_path.exists():
            return "claude-code: nothing to remove"
        cfg = json.loads(self.config_path.read_text())
        servers = (cfg.get("mcpServers") or {})
        if name not in servers:
            return "claude-code: not present"
        if dry_run:
            return f"claude-code: would remove {name}"
        servers.pop(name)
        self.config_path.write_text(json.dumps(cfg, indent=2))
        return f"claude-code: removed {name}"

    # ----------------------------- transcripts ---------------------------
    def session_dir(self):
        return self.projects_dir

    def transcript_files(self) -> list[Path]:
        if not self.projects_dir.exists():
            return []
        return list(self.projects_dir.glob("**/*.jsonl"))

    def iter_messages(self, fp: Path) -> Iterator[NormalizedMessage]:
        try:
            with fp.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    uuid = obj.get("uuid")
                    if not uuid:
                        continue
                    msg = obj.get("message", {})
                    role = msg.get("role") or obj.get("type")
                    if role not in ("user", "assistant"):
                        continue
                    text = _extract_text(msg)
                    created = _ts(obj.get("timestamp", ""))
                    yield NormalizedMessage(
                        uuid=uuid,
                        session_id=obj.get("sessionId") or "",
                        role=role,
                        content=text,
                        model=msg.get("model") or "",
                        created_at=created,
                        raw=msg,
                    )
        except OSError:
            return


ADAPTER = ClaudeCodeAdapter()
