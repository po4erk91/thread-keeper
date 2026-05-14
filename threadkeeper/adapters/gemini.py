"""Google Gemini CLI adapter.

Config: ~/.gemini/settings.json with an `mcpServers` section (same
shape as Claude Code's, JSON).

Transcripts: ~/.gemini/tmp/<user>/chats/session-<ts>-<id>.jsonl
Each line is a JSON object. First line is session-meta
({"sessionId", "projectHash", "startTime", ...}). Subsequent lines:
  {"id": <msg-uuid>, "timestamp": ..., "type": "info"|"user"|"model"|...,
   "content": <string or structured>}
We pick `type in {"user", "model"}` as turns; map "model" → "assistant".
"""
from __future__ import annotations

import getpass
import json
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


def _coerce_text(content) -> str:
    """Gemini content may be: a plain string, a list of segments, or a
    dict with text/parts. Coerce to a single string."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if isinstance(content.get("parts"), list):
            return "\n".join(_coerce_text(p) for p in content["parts"])
    if isinstance(content, list):
        return "\n".join(_coerce_text(x) for x in content if x is not None)
    return ""


class GeminiAdapter(CLIAdapter):
    name = "gemini"

    def __init__(self) -> None:
        self.config_path = Path("~/.gemini/settings.json").expanduser()
        self._instructions = Path("~/.gemini/GEMINI.md").expanduser()
        # The "chats" tmp tree is per-OS-user; resolve at instance time.
        self.chats_root = Path(
            f"~/.gemini/tmp/{getpass.getuser()}/chats"
        ).expanduser()

    def instructions_path(self):
        return self._instructions

    def hooks_supported(self) -> bool:
        return True

    def register_hooks(self, specs, dry_run=False) -> str:
        # Gemini reads `settings.hooks` in the same shape as Claude
        # Code (the bundle even ships `gemini hooks migrate --from-claude`),
        # so the Claude-style helper Just Works for the same file.
        from ._hook_helpers import install_claude_style_hooks
        return install_claude_style_hooks(
            self.config_path, specs, dry_run=dry_run,
        )

    def is_installed(self) -> bool:
        if self.config_path.exists() or self.chats_root.parent.exists():
            return True
        return shutil.which("gemini") is not None

    # ----- MCP registration ---------------------------------------------
    def register_mcp_server(
        self, name, command, args, env, dry_run=False
    ) -> str:
        cfg: dict
        if self.config_path.exists():
            try:
                cfg = json.loads(self.config_path.read_text())
            except json.JSONDecodeError:
                return "gemini: malformed settings.json — refused"
        else:
            cfg = {}
        servers = cfg.setdefault("mcpServers", {})
        entry = {
            "command": command,
            "args": list(args),
        }
        if env:
            entry["env"] = dict(env)
        existing = servers.get(name)
        if existing == entry:
            return "gemini: already current"
        servers[name] = entry
        if not dry_run:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(cfg, indent=2))
        return f"gemini: {'would ' if dry_run else ''}{'update' if existing else 'add'}"

    def unregister_mcp_server(self, name, dry_run=False) -> str:
        if not self.config_path.exists():
            return "gemini: nothing to remove"
        cfg = json.loads(self.config_path.read_text())
        servers = (cfg.get("mcpServers") or {})
        if name not in servers:
            return "gemini: not present"
        if dry_run:
            return f"gemini: would remove {name}"
        servers.pop(name)
        self.config_path.write_text(json.dumps(cfg, indent=2))
        return f"gemini: removed {name}"

    # ----- Transcript ingestion -----------------------------------------
    def session_dir(self):
        return self.chats_root

    def transcript_files(self) -> list[Path]:
        if not self.chats_root.exists():
            return []
        return list(self.chats_root.glob("session-*.jsonl"))

    def iter_messages(self, fp: Path) -> Iterator[NormalizedMessage]:
        sess_id = ""
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
                    # session-meta line: first record carries sessionId.
                    if "sessionId" in obj and "projectHash" in obj:
                        sess_id = obj.get("sessionId") or ""
                        continue
                    typ = obj.get("type")
                    if typ == "model":
                        role = "assistant"
                    elif typ == "user":
                        role = "user"
                    else:
                        continue
                    uuid = obj.get("id")
                    if not uuid:
                        continue
                    text = _coerce_text(obj.get("content", ""))
                    yield NormalizedMessage(
                        uuid=uuid,
                        session_id=sess_id,
                        role=role,
                        content=text,
                        model=obj.get("model") or "",
                        created_at=_ts(obj.get("timestamp", "")),
                        raw=obj,
                    )
        except OSError:
            return


ADAPTER = GeminiAdapter()
