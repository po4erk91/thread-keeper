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

_FORCED_CID_RE = re.compile(
    r"Your own cid is ([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


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


def _forced_cid_from_text(text: str) -> str:
    if "You were spawned in the background by parent conversation" not in text:
        return ""
    m = _FORCED_CID_RE.search(text)
    return m.group(1) if m else ""


# --- minimal TOML R/W ---------------------------------------------------
# We don't want to depend on tomllib for writes (Python's stdlib has
# tomllib for reads only). The shape we touch is one section:
# `[mcp_servers.<name>]` with key=value lines. Implement just enough.

_THREAD_KEEPER_AUTO_APPROVED_TOOLS = (
    "brief",
    "context",
    "open_thread",
    "close_thread",
    "note",
    "search",
    "dialog_search",
    "broadcast",
    "whisper",
    "wait",
    "inbox",
    "accept_candidate",
    "reject_candidate",
    "lesson_list",
    "lesson_get",
    "lesson_append",
    "lesson_remove",
    "lesson_restore",
    "skill_list",
    "skill_manage",
    "evolve_review",
    "evolve_decide",
    "evolve_issue_create",
    "evolve_apply",
    "evolve_apply_curator_report",
    "evolve_apply_roadmap_issue",
    "evolve_mark_applied",
    "evolve_mark_curator_report_applied",
    "evolve_mark_roadmap_issue_applied",
    "dialectic_claim",
    "dialectic_evidence",
    "dialectic_review",
    "dialectic_synthesis",
    "dialectic_supersede",
    "dialectic_observation_resolve",
)


def _thread_keeper_tools_config() -> dict:
    return {
        "tools": {
            tool: {"approval_mode": "approve"}
            for tool in _THREAD_KEEPER_AUTO_APPROVED_TOOLS
        }
    }


def _approval_blocks(name: str) -> str:
    if name != "thread-keeper":
        return ""
    lines: list[str] = []
    for tool in _THREAD_KEEPER_AUTO_APPROVED_TOOLS:
        lines.append("")
        lines.append(f"[mcp_servers.{name}.tools.{tool}]")
        lines.append('approval_mode = "approve"')
    return "\n".join(lines) + "\n"


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
    return "\n".join(lines) + "\n" + _approval_blocks(name)


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
    uses_stdin_prompt = True

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
        """Codex non-interactive: `codex exec --skip-git-repo-check [-m MODEL] -`.
        Codex reads MCP servers from ~/.codex/config.toml which the
        thread-keeper-setup installer already wires up — no
        per-invocation MCP config file needed. Prompt text is supplied via
        stdin so large autonomous-loop inventories do not hit ARG_MAX.

        `--skip-git-repo-check` is required because `codex exec` refuses to run
        ("Not inside a trusted directory and --skip-git-repo-check was not
        specified") whenever the spawn cwd is not a trusted git worktree. The
        cwd is inherited from the spawning server, so without this flag the
        autonomous loops fail intermittently — depending on where the host
        server happened to launch — while `git`-rooted hosts pass. The child
        is already isolated by the sandbox flag below and by the thread-keeper
        permission model, so the repo-trust gate adds nothing here.

        Code-evolve children need to create branches/commits. Codex's default
        sandbox can write ordinary workspace files but blocks `.git` refs, so
        map Claude's `bypassPermissions` request to Codex's explicit
        no-sandbox flag.
        """
        bin_path = shutil.which("codex")
        if not bin_path:
            return None
        argv = [bin_path, "exec", "--skip-git-repo-check"]
        if model:
            argv += ["-m", model]
        if permission_mode == "bypassPermissions":
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            argv += ["--sandbox", "workspace-write"]
        argv.append("-")
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
            if name == "thread-keeper":
                want.update(_thread_keeper_tools_config())
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

    def _forced_session_id(self, fp: Path) -> str:
        """Return THREADKEEPER_FORCE_CID encoded in our spawn preamble.

        Codex records its own rollout UUID in session_meta.payload.id even
        when the child process has THREADKEEPER_FORCE_CID. For thread-keeper
        provenance we want every message from that spawned transcript grouped
        under the forced child cid, matching tasks.spawned_cid and Claude's
        --session-id behavior.
        """
        try:
            with fp.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "You were spawned in the background" not in line:
                        continue
                    try:
                        env = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = env.get("payload") or {}
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("type") != "message":
                        continue
                    cid = _forced_cid_from_text(_extract_text(payload))
                    if cid:
                        return cid
        except OSError:
            return ""
        return ""

    def iter_messages(self, fp: Path) -> Iterator[NormalizedMessage]:
        sess_id = ""
        forced_session_id = self._forced_session_id(fp)
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
                        session_id=forced_session_id or sess_id,
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
