"""CLIAdapter abstract base — contract every adapter implements.

Each adapter knows three things:
  1. How to detect that this CLI is installed on the user's machine
  2. How to register/unregister thread-keeper in that CLI's MCP config
  3. How to enumerate + parse the conversation transcripts the CLI
     writes to disk

Adapters return data through a single normalized shape
(`NormalizedMessage`) so ingest doesn't have to special-case any CLI.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


@dataclass
class NormalizedMessage:
    """Adapter output: a single user/assistant turn, normalized.

    Fields:
      uuid        — stable per-message id from the transcript.
      session_id  — opaque identifier for the conversation/session.
      role        — 'user' | 'assistant'.
      content     — extracted text (concatenated text/thinking blocks,
                    capped tool_result blocks).
      model       — model name if known, else "".
      created_at  — unix epoch seconds.
      raw         — the original parsed dict, in case downstream code
                    needs to peek into adapter-specific fields (e.g.
                    Skill tool_use detection in ingest).
    """
    uuid: str
    session_id: str
    role: str
    content: str
    model: str
    created_at: int
    raw: dict


class CLIAdapter(ABC):
    """A pluggable target CLI integration."""

    # Stable, lowercase-hyphen identifier ('claude-code', 'codex', etc).
    # Used in dialog_messages.source and elsewhere as a provenance tag.
    name: str = ""

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------
    @abstractmethod
    def is_installed(self) -> bool:
        """Return True iff this CLI is present on the system (the
        adapter checks for whatever combination of executable + config
        dir + log dir is meaningful for that CLI)."""

    # ------------------------------------------------------------------
    # MCP registration
    # ------------------------------------------------------------------
    @abstractmethod
    def register_mcp_server(
        self,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str],
        dry_run: bool = False,
    ) -> str:
        """Add thread-keeper to the CLI's MCP server config (idempotent).

        Return a one-line human status: 'created', 'updated', 'already
        current', or 'unsupported: <reason>'.
        """

    @abstractmethod
    def unregister_mcp_server(self, name: str, dry_run: bool = False) -> str:
        """Remove an MCP server entry by name (idempotent)."""

    # ------------------------------------------------------------------
    # Transcript ingestion
    # ------------------------------------------------------------------
    @abstractmethod
    def transcript_files(self) -> list[Path]:
        """Return every transcript file this adapter knows about, in
        any order. ingest will sort/filter by mtime."""

    @abstractmethod
    def iter_messages(self, fp: Path) -> Iterator[NormalizedMessage]:
        """Yield NormalizedMessage from one transcript file, in file
        order. Skip malformed lines silently."""

    # ------------------------------------------------------------------
    # Optional hooks (default: no-op)
    # ------------------------------------------------------------------
    def project_label(self, fp: Path) -> str:
        """Project tag stored as dialog_messages.project. Default:
        parent directory name."""
        return fp.parent.name

    def session_dir(self) -> Optional[Path]:
        """Root directory under which transcripts live. Used by ingest
        to decide whether this adapter has anything to scan."""
        return None

    def instructions_path(self) -> Optional[Path]:
        """Path to the per-user system-prompt-style file this CLI reads
        at session start (e.g. Claude's CLAUDE.md, Codex's AGENTS.md).
        Return None when the CLI has no such global file (e.g. Copilot
        only supports per-repo instructions)."""
        return None

    # ------------------------------------------------------------------
    # Spawn — per-CLI headless invocation
    # ------------------------------------------------------------------
    def supports_spawn(self) -> bool:
        """True iff this CLI can be spawned non-interactively from a
        thread-keeper daemon. Implies a working headless invocation
        (`claude -p` / `codex exec` / `gemini -p` / `copilot -p`) AND
        a way to inject our MCP server config so the spawned child
        can call back into thread-keeper. Default: False (loops will
        gracefully skip this CLI)."""
        return False

    def spawn_argv(self, prompt: str, *,
                   model: str = "",
                   permission_mode: str = "auto",
                   extra_allowed_tools: str = "",
                   mcp_config_path: Optional[Path] = None,
                   ) -> Optional[list[str]]:
        """Construct the argv list to launch a non-interactive child of
        this CLI with the given prompt. Returns None when the adapter
        doesn't support spawn (caller should skip).

        Default returns None — concrete adapters override."""
        return None

    def skills_dir(self) -> Optional[Path]:
        """Root directory under which this CLI auto-discovers Skill.md
        files (Anthropic-style skill format: YAML frontmatter +
        description-based auto-trigger). Examples:

            Claude (Code/Desktop/IDE) → ~/.claude/skills/
            Codex (CLI/desktop)       → ~/.codex/skills/

        Return None when the CLI doesn't natively consume Skills
        (Gemini, Copilot, generic MCP clients) — those fall back to
        the CLI-agnostic ~/.threadkeeper/lessons.md store.

        Multi-mirror writes in skill_manage use this to propagate one
        SKILL.md across every native skills-store on the machine so a
        single materialization reaches every detected CLI.
        """
        return None

    # ------------------------------------------------------------------
    # Hooks (optional)
    # ------------------------------------------------------------------
    def hooks_supported(self) -> bool:
        """True iff this CLI honors shell-style lifecycle hooks
        (SessionStart, PostToolUse, etc.)."""
        return False

    def register_hooks(
        self,
        specs: list[dict],
        dry_run: bool = False,
    ) -> str:
        """Install all `specs` into the CLI's hook config. Each spec is
        `{event, command, matcher?}` — adapter translates to whatever
        local format the CLI uses. Idempotent: existing entries for the
        same event + command are left in place; matchers updated if
        changed. Default: 'unsupported'."""
        return f"{self.name}: hooks unsupported"
