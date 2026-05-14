"""GitHub Copilot CLI adapter.

Config: ~/.copilot/mcp-config.json — JSON with a top-level `mcpServers`
section (same key as Claude / Gemini, contrary to older bundles that
shipped an unused `servers` key).

Instructions: Copilot doesn't have a stable single "global" file. Its
conventionPaths table (from the CLI bundle):

    AGENTS.md                     in cwd and walked-up parents
    CLAUDE.md                     in cwd, parents, and `.claude/` subdir
    GEMINI.md                     in cwd and parents
    copilot-instructions.md       in `.github/` subdir, walked up

Walking up from any project under `~`, Copilot reaches `~/.claude/CLAUDE.md`
(which our setup manages) and picks it up automatically. We also write
the canonically-named `~/.copilot/copilot-instructions.md` as a stable
location user-level symlinks/projects can point at.

Transcripts: ~/.copilot/session-store.db — a sqlite database. Schema:
  sessions(id, cwd, repository, host_type, branch, summary, created_at, updated_at)
  turns(id, session_id, turn_index, user_message, assistant_response, timestamp)

Each row in `turns` corresponds to ONE user/assistant exchange. We
split it into two NormalizedMessage records during ingest so the rest
of the pipeline (FTS, semantic, skill scan) doesn't have to know
about the merged-turn shape.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .base import CLIAdapter, NormalizedMessage


def _ts(s: str) -> int:
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        try:
            # Copilot stores timestamps via sqlite's datetime('now')
            # which yields 'YYYY-MM-DD HH:MM:SS' (no T, no tz).
            return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp())
        except Exception:
            import time
            return int(time.time())


class CopilotAdapter(CLIAdapter):
    name = "copilot"

    def __init__(self) -> None:
        self.config_path = Path("~/.copilot/mcp-config.json").expanduser()
        self.session_db = Path("~/.copilot/session-store.db").expanduser()
        self._instructions = Path("~/.copilot/copilot-instructions.md").expanduser()
        # Copilot loads hooks from `~/.copilot/hooks.json` (or
        # `~/.copilot/hooks/hooks.json`). The bundle's schema accepts
        # the same Claude-Code-compatible shape (`_vsCodeCompat` mode),
        # so we reuse the helper but write to a dedicated file rather
        # than into mcp-config.json.
        self._hooks_path = Path("~/.copilot/hooks.json").expanduser()

    def instructions_path(self):
        return self._instructions

    def hooks_supported(self) -> bool:
        return True

    def register_hooks(self, specs, dry_run=False) -> str:
        from ._hook_helpers import install_claude_style_hooks
        return install_claude_style_hooks(
            self._hooks_path, specs, dry_run=dry_run,
        )

    def is_installed(self) -> bool:
        if self.config_path.exists() or self.session_db.exists():
            return True
        return (
            shutil.which("copilot") is not None
            or shutil.which("gh") is not None
        )

    # ----- MCP registration ---------------------------------------------
    def register_mcp_server(
        self, name, command, args, env, dry_run=False
    ) -> str:
        cfg: dict
        if self.config_path.exists():
            try:
                cfg = json.loads(self.config_path.read_text())
            except json.JSONDecodeError:
                return "copilot: malformed mcp-config.json — refused"
        else:
            cfg = {}
        # Copilot v1.0.43+ schema validates the top-level `mcpServers`
        # key (same as Claude/Gemini). Older bundles shipped with
        # `servers` documented; the validator now rejects that file.
        # If we see a legacy `servers` block, migrate its contents into
        # `mcpServers` AND drop the legacy key so the file is valid.
        legacy = cfg.pop("servers", None)
        if isinstance(legacy, dict):
            cfg.setdefault("mcpServers", {})
            for k, v in legacy.items():
                cfg["mcpServers"].setdefault(k, v)
        servers = cfg.setdefault("mcpServers", {})
        entry = {
            "command": command,
            "args": list(args),
        }
        if env:
            entry["env"] = dict(env)
        existing = servers.get(name)
        if existing == entry and legacy is None:
            return "copilot: already current"
        servers[name] = entry
        if not dry_run:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(cfg, indent=2))
        if legacy is not None:
            return f"copilot: migrated legacy 'servers' → 'mcpServers' + {'added' if not existing else 'updated'} {name}"
        return f"copilot: {'would ' if dry_run else ''}{'update' if existing else 'add'}"

    def unregister_mcp_server(self, name, dry_run=False) -> str:
        if not self.config_path.exists():
            return "copilot: nothing to remove"
        cfg = json.loads(self.config_path.read_text())
        servers = (cfg.get("mcpServers") or cfg.get("servers") or {})
        if name not in servers:
            return "copilot: not present"
        if dry_run:
            return f"copilot: would remove {name}"
        servers.pop(name)
        self.config_path.write_text(json.dumps(cfg, indent=2))
        return f"copilot: removed {name}"

    # ----- Transcript ingestion -----------------------------------------
    # Copilot stores transcripts in a single sqlite DB rather than per-
    # session jsonl files. We treat the DB itself as the single
    # "transcript file" — ingest's mtime-based incremental scheme works
    # because Copilot updates the file as new turns land.
    def session_dir(self):
        return self.session_db.parent

    def transcript_files(self) -> list[Path]:
        if self.session_db.exists():
            return [self.session_db]
        return []

    def iter_messages(self, fp: Path) -> Iterator[NormalizedMessage]:
        if not fp.exists():
            return
        try:
            conn = sqlite3.connect(f"file:{fp}?mode=ro&immutable=0", uri=True)
        except sqlite3.OperationalError:
            return
        try:
            rows = conn.execute(
                "SELECT session_id, turn_index, user_message, "
                "assistant_response, timestamp FROM turns "
                "ORDER BY session_id, turn_index"
            ).fetchall()
        except sqlite3.OperationalError:
            conn.close()
            return
        conn.close()
        for sess_id, turn_idx, user_msg, asst_msg, ts in rows:
            created = _ts(ts or "")
            if user_msg:
                yield NormalizedMessage(
                    uuid=f"copilot:{sess_id}:{turn_idx}:u",
                    session_id=sess_id,
                    role="user",
                    content=user_msg,
                    model="",
                    created_at=created,
                    raw={"turn_index": turn_idx},
                )
            if asst_msg:
                yield NormalizedMessage(
                    uuid=f"copilot:{sess_id}:{turn_idx}:a",
                    session_id=sess_id,
                    role="assistant",
                    content=asst_msg,
                    model="",
                    created_at=created,
                    raw={"turn_index": turn_idx},
                )


ADAPTER = CopilotAdapter()
