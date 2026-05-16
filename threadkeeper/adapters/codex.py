"""OpenAI Codex CLI adapter.

Codex stores configuration in ~/.codex/config.toml with sections
`[mcp_servers.<name>]`. Conversation transcripts are JSONL files at
~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl with envelopes like:

  {"timestamp": "...", "type": "session_meta", "payload": {...}}
  {"timestamp": "...", "type": "event_msg",    "payload": {...}}
  {"timestamp": "...", "type": "response_item","payload": {"type": "message", "role": ..., "content": [...]}}

We pick `type=response_item` and `payload.type=message` as turns.
"""
from __future__ import annotations

import json
import os
import re
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


def _extract_text(payload: dict) -> str:
    """Codex content blocks: input_text/output_text/tool_call/etc.
    We collect the text-flavored ones, cap tool_call payloads."""
    content = payload.get("content", [])
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t in ("input_text", "output_text", "text"):
            parts.append(block.get("text", ""))
        elif t == "thinking":
            parts.append(f"[thinking] {block.get('text', '')}")
        elif t == "tool_call_output":
            out = block.get("output", "")
            if isinstance(out, str) and out:
                parts.append(f"[tool_result] {out[:800]}")
    return "\n".join(p for p in parts if p)


# --- minimal TOML R/W ---------------------------------------------------
# We don't want to depend on tomllib for writes (Python's stdlib has
# tomllib for reads only). The shape we touch is one section:
# `[mcp_servers.<name>]` with key=value lines. Implement just enough.

def _read_toml(fp: Path) -> dict:
    if not fp.exists():
        return {}
    try:
        import tomllib  # py3.11+
    except ImportError:
        # Fallback: VERY narrow parser — only used in environments
        # without tomllib. Returns empty (caller treats as "no MCP").
        return {}
    try:
        return tomllib.loads(fp.read_text())
    except Exception:
        return {}


def _serialize_mcp_section(name: str, command: str,
                           args: list[str], env: dict[str, str]) -> str:
    """Produce the `[mcp_servers.<name>]` TOML block as a string."""
    lines = [f"[mcp_servers.{name}]"]
    lines.append(f"command = {json.dumps(command)}")
    args_str = "[" + ", ".join(json.dumps(a) for a in args) + "]"
    lines.append(f"args = {args_str}")
    if env:
        lines.append("[mcp_servers." + name + ".env]")
        for k, v in env.items():
            lines.append(f"{k} = {json.dumps(v)}")
    return "\n".join(lines) + "\n"


_SECTION_HEADER_RE = re.compile(
    r"^\[(mcp_servers\.[A-Za-z0-9_\-]+)(?:\.[A-Za-z0-9_\-]+)?\]\s*$",
    re.MULTILINE,
)


def _replace_or_append_mcp_block(
    body: str, name: str, new_block: str
) -> str:
    """Strip every TOML section beginning with `[mcp_servers.<name>...]`
    (including nested `.env`), then append the new block at end.
    Other sections are preserved as-is."""
    out: list[str] = []
    current_section = ""
    target_prefix = f"mcp_servers.{name}"
    skip_current = False
    for line in body.splitlines(keepends=True):
        m = _SECTION_HEADER_RE.match(line.rstrip("\n"))
        if m:
            section_full = m.group(0).strip("[]")
            current_section = section_full
            skip_current = (
                section_full == target_prefix
                or section_full.startswith(target_prefix + ".")
            )
            if skip_current:
                continue
        if skip_current:
            # still inside the target section — drop the line
            continue
        out.append(line)
    result = "".join(out).rstrip() + "\n\n" + new_block
    return result


# --- adapter ------------------------------------------------------------

class CodexAdapter(CLIAdapter):
    name = "codex"

    def __init__(self) -> None:
        self.config_path = Path("~/.codex/config.toml").expanduser()
        self.sessions_dir = Path("~/.codex/sessions").expanduser()
        # Codex loads AGENTS.md from cwd → parents → ~. We manage the
        # home-level fallback so it's always present even outside a
        # project tree.
        self._instructions = Path("~/.codex/AGENTS.md").expanduser()
        # Codex auto-discovers skills under $CODEX_HOME/skills/ — same
        # Anthropic-style SKILL.md format Claude uses. Multi-mirror in
        # skill_manage propagates SKILL.md here so the same skill is
        # available in Codex's own session.
        self._skills_dir = Path(
            os.environ.get("CODEX_HOME", "~/.codex")
        ).expanduser() / "skills"

    def skills_dir(self):
        return self._skills_dir

    def supports_spawn(self) -> bool:
        return True

    def spawn_argv(self, prompt, *, model="", permission_mode="auto",
                   extra_allowed_tools="", mcp_config_path=None):
        """Codex non-interactive: `codex exec [-m MODEL] <prompt>`.
        Codex reads MCP servers from ~/.codex/config.toml which the
        thread-keeper-setup installer already wires up — no
        per-invocation MCP config file needed."""
        bin_path = shutil.which("codex")
        if not bin_path:
            return None
        argv = [bin_path, "exec"]
        if model:
            argv += ["-m", model]
        argv.append(prompt)
        return argv

    def instructions_path(self):
        return self._instructions

    def is_installed(self) -> bool:
        if self.config_path.exists() or self.sessions_dir.exists():
            return True
        return shutil.which("codex") is not None

    # ----- MCP registration ---------------------------------------------
    def register_mcp_server(
        self, name, command, args, env, dry_run=False
    ) -> str:
        block = _serialize_mcp_section(name, command, list(args), dict(env))
        if not self.config_path.exists():
            if dry_run:
                return "codex: would create config.toml with mcp section"
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(block)
            return "codex: created config.toml"
        body = self.config_path.read_text()
        # Check if already current (cheap normalization compare)
        already = _read_toml(self.config_path).get("mcp_servers", {}).get(name)
        if isinstance(already, dict):
            want = {"command": command, "args": list(args)}
            if env:
                want["env"] = dict(env)
            if already == want:
                return "codex: already current"
        new_body = _replace_or_append_mcp_block(body, name, block)
        if new_body == body:
            return "codex: already current"
        if dry_run:
            return "codex: would update config.toml"
        self.config_path.write_text(new_body)
        return "codex: updated config.toml"

    def unregister_mcp_server(self, name, dry_run=False) -> str:
        if not self.config_path.exists():
            return "codex: nothing to remove"
        body = self.config_path.read_text()
        new_body = _replace_or_append_mcp_block(body, name, "").rstrip() + "\n"
        if new_body.rstrip() == body.rstrip():
            return "codex: not present"
        if dry_run:
            return f"codex: would remove {name}"
        self.config_path.write_text(new_body)
        return f"codex: removed {name}"

    # ----- Transcript ingestion -----------------------------------------
    def session_dir(self):
        return self.sessions_dir

    def transcript_files(self) -> list[Path]:
        if not self.sessions_dir.exists():
            return []
        return list(self.sessions_dir.glob("**/rollout-*.jsonl"))

    def iter_messages(self, fp: Path) -> Iterator[NormalizedMessage]:
        sess_id = ""
        try:
            with fp.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        env = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    typ = env.get("type")
                    payload = env.get("payload") or {}
                    if typ == "session_meta" and isinstance(payload, dict):
                        sess_id = payload.get("id") or sess_id
                        continue
                    if typ != "response_item":
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("type") != "message":
                        continue
                    role = payload.get("role")
                    if role == "developer":
                        # Codex injects a developer turn with permission
                        # instructions etc. Skip — not user dialog.
                        continue
                    if role not in ("user", "assistant"):
                        continue
                    text = _extract_text(payload)
                    # Stable per-line id: use payload.id when present,
                    # else fall back to timestamp+offset.
                    uuid = payload.get("id") or f"codex:{fp.name}:{env.get('timestamp', '')}"
                    yield NormalizedMessage(
                        uuid=uuid,
                        session_id=sess_id,
                        role=role,
                        content=text,
                        model=payload.get("model") or "",
                        created_at=_ts(env.get("timestamp", "")),
                        raw=payload,
                    )
        except OSError:
            return

    def project_label(self, fp: Path) -> str:
        # rollout files are in YYYY/MM/DD subdirs — use the parent of
        # parent (year/month) for a coarse but meaningful label.
        return f"codex-{fp.parent.parent.parent.name}"  # year


ADAPTER = CodexAdapter()
