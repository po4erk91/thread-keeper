"""Google Antigravity CLI (agy) adapter.

Antigravity CLI is the successor path for consumer Gemini CLI users.

Config/customizations:
  ~/.gemini/config/mcp_config.json   MCP servers
  ~/.gemini/config/AGENTS.md         global rules/instructions
  ~/.gemini/config/skills/           global skills/customizations

Transcripts:
  Antigravity CLI 1.0.x stores each conversation as one SQLite file,
  ~/.gemini/antigravity-cli/conversations/<cascade-id>.db. The `steps`
  table holds one protobuf `step_payload` per trajectory step; there is no
  published schema, so the reader decodes only the few fields it needs:

    step_type 14  user input      payload 19.2 (raw text), 19.3.1 fallback
    step_type 15  model response  payload 20.1 (response text), 20.8 fallback
    metadata      1.1 created-at seconds, 12 turn id (shared by a turn's steps)
    trajectory_metadata_blob  1.1 or 7: workspace `file://` URI

  Tool calls, thinking, and every other step type are skipped. A step that
  does not decode, or a file without that schema, is skipped silently.
  The files are opened read-only; see `_open_readonly` for the WAL rules.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import quote, unquote, urlparse

from .base import CLIAdapter, NormalizedMessage, find_cli_executable
from .codex import _forced_cid_from_text
from ..config_io import mutate_json_file

_USER_STEP = 14
_ASSISTANT_STEP = 15
# Terminal states seen in finished conversations: done, canceled, error. A
# step that is still generating is skipped until a later pass sees it final,
# because ingest dedupes by uuid and would never replace a partial answer.
_FINAL_STEP_STATUSES = frozenset({3, 6, 7})
# SQLite errors that mean "not an agy conversation we understand" rather than
# "try again later"; the file is skipped until it changes.
_PERMANENT_DB_ERRORS = ("no such table", "no such column", "file is not a database")


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise ValueError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _pb_fields(buf: bytes) -> dict[int, list]:
    """Decode one protobuf message level: field number -> values.

    Varints come back as ints and length-delimited values as bytes; fixed-width
    values are skipped. Raises ValueError on malformed input.
    """
    fields: dict[int, list] = {}
    pos = 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(buf, pos)
        elif wire == 2:
            size, pos = _varint(buf, pos)
            if pos + size > len(buf):
                raise ValueError("truncated field")
            value, pos = bytes(buf[pos:pos + size]), pos + size
        elif wire in (1, 5):
            pos += 8 if wire == 1 else 4
            if pos > len(buf):
                raise ValueError("truncated fixed field")
            continue
        else:
            raise ValueError(f"unsupported wire type {wire}")
        fields.setdefault(number, []).append(value)
    return fields


def _pb_get(buf, *path):
    """First value at a nested field path; None when absent or malformed."""
    value = buf
    for number in path:
        if not isinstance(value, (bytes, bytearray)):
            return None
        try:
            values = _pb_fields(value).get(number)
        except ValueError:
            return None
        if not values:
            return None
        value = values[0]
    return value


def _pb_text(buf, *path) -> str:
    value = _pb_get(buf, *path)
    if not isinstance(value, (bytes, bytearray)):
        return ""
    return value.decode("utf-8", errors="replace").strip()


def _workspace_path(uri: str) -> str:
    if not uri.startswith("file://"):
        return ""
    return unquote(urlparse(uri).path)


def _open_readonly(fp: Path) -> sqlite3.Connection:
    """Open an agy conversation without writing anything next to it.

    A running agy keeps -wal/-shm sidecars, and a read-only reader shares them
    to see the newest steps. Without both sidecars the file is at rest, so it
    is opened immutable: a plain read-only open of a WAL database would create
    fresh sidecars in agy's directory.
    """
    live = (
        fp.with_name(fp.name + "-wal").exists()
        and fp.with_name(fp.name + "-shm").exists()
    )
    mode = "mode=ro" if live else "immutable=1"
    return sqlite3.connect(
        f"file:{quote(str(fp))}?{mode}", uri=True, timeout=1.0,
    )


def _read_steps(fp: Path) -> tuple[list[tuple], bytes | None]:
    """User/model steps plus the trajectory metadata blob, read in one go.

    Rows are fetched before anything is yielded so no read transaction stays
    open while ingest embeds text. Transient SQLite errors become OSError so
    ingest keeps its cursor and retries the file on the next pass.
    """
    try:
        conn = _open_readonly(fp)
    except sqlite3.Error as exc:
        raise OSError(f"antigravity: cannot open {fp.name}: {exc}") from exc
    try:
        rows = conn.execute(
            "SELECT idx, step_type, status, metadata, step_payload FROM steps "
            "WHERE step_type IN (?, ?) ORDER BY idx",
            (_USER_STEP, _ASSISTANT_STEP),
        ).fetchall()
        try:
            meta = conn.execute(
                "SELECT data FROM trajectory_metadata_blob WHERE id='main'"
            ).fetchone()
        except sqlite3.OperationalError:
            meta = None
    except sqlite3.Error as exc:
        if str(exc).lower().startswith(_PERMANENT_DB_ERRORS):
            return [], None
        raise OSError(f"antigravity: cannot read {fp.name}: {exc}") from exc
    finally:
        conn.close()
    return rows, (meta[0] if meta else None)


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

    def discover_models(self, timeout_s: float = 5.0) -> dict:
        """Use Antigravity's native, account-aware ``agy models`` command."""
        bin_path = find_cli_executable("agy", "antigravity")
        if not bin_path:
            return {
                "models": [], "source": "agy models",
                "source_updated_at": None, "error": "Antigravity is not on PATH.",
            }
        try:
            result = subprocess.run(
                [bin_path, "models"], capture_output=True, text=True,
                timeout=max(0.5, timeout_s), check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "models": [], "source": "agy models",
                "source_updated_at": None, "error": f"Model refresh failed: {exc}",
            }
        models = []
        for line in result.stdout.splitlines():
            value = line.strip().lstrip("-*• ").strip()
            if value and value not in models:
                models.append(value)
        error = None
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "agy models failed").strip()[:300]
        elif not models:
            error = "agy models returned no models; use CLI default or custom entry."
        return {
            "models": models,
            "source": "agy models",
            "source_updated_at": int(time.time()),
            "error": error,
        }

    def spawn_argv(self, prompt, *, model="", permission_mode="auto",
                   effort="", extra_allowed_tools="", mcp_config_path=None):
        """Antigravity non-interactive: `agy -p <prompt> [--model X]`.

        Antigravity reads MCP servers from ~/.gemini/config/mcp_config.json,
        which thread-keeper-setup wires up.
        """
        bin_path = find_cli_executable("agy", "antigravity")
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
            bool(find_cli_executable("agy", "antigravity"))
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
        entry = {
            "command": command,
            "args": list(args),
        }
        if env:
            entry["env"] = dict(env)

        def update(cfg: dict) -> tuple[bool, object]:
            servers = cfg.setdefault("mcpServers", {})
            existing = servers.get(name)
            if existing == entry:
                return False, existing
            servers[name] = entry
            return True, existing

        try:
            if dry_run:
                _, existing = update(self._read_config())
            else:
                existing = mutate_json_file(
                    self.config_path, update, allow_empty=True,
                )
        except json.JSONDecodeError:
            return "antigravity: malformed mcp_config.json — refused"
        if existing == entry:
            return "antigravity: already current"
        return f"antigravity: {'would ' if dry_run else ''}{'update' if existing else 'add'}"

    def unregister_mcp_server(self, name, dry_run=False) -> str:
        def remove(cfg: dict) -> tuple[bool, bool]:
            servers = cfg.get("mcpServers") or {}
            if name not in servers:
                return False, False
            servers.pop(name)
            return True, True

        try:
            if dry_run:
                if not self.config_path.exists():
                    return "antigravity: nothing to remove"
                _, present = remove(self._read_config())
            else:
                if not self.config_path.exists():
                    return "antigravity: nothing to remove"
                present = mutate_json_file(
                    self.config_path, remove, allow_empty=True,
                )
        except json.JSONDecodeError:
            return "antigravity: malformed mcp_config.json — refused"
        if not present:
            return "antigravity: not present"
        if dry_run:
            return f"antigravity: would remove {name}"
        return f"antigravity: removed {name}"

    # ----- Transcript ingestion -----------------------------------------
    def session_dir(self):
        return self.conversations_root

    def transcript_files(self) -> list[Path]:
        if not self.conversations_root.is_dir():
            return []
        return sorted(self.conversations_root.glob("*.db"))

    def transcript_stat(self, fp: Path) -> tuple[float, int]:
        """Count the -wal sidecar: a running agy writes there first, and the
        main file changes only when SQLite checkpoints."""
        st = fp.stat()
        try:
            wal = fp.with_name(fp.name + "-wal").stat()
        except OSError:
            return st.st_mtime, st.st_size
        return max(st.st_mtime, wal.st_mtime), st.st_size + wal.st_size

    def project_label(self, fp: Path) -> str:
        return "antigravity"

    def iter_messages(self, fp: Path) -> Iterator[NormalizedMessage]:
        fallback_ts = int(fp.stat().st_mtime)
        rows, meta = _read_steps(fp)
        origin = _workspace_path(_pb_text(meta, 1, 1) or _pb_text(meta, 7))
        cascade_id = fp.stem
        session_id = cascade_id
        # spawn() inlines its preamble into an agy child's first prompt; the
        # forced cid there keeps the child's dialog joined to its task row.
        for _idx, step_type, _status, _metadata, payload in rows:
            if step_type == _USER_STEP:
                forced = _forced_cid_from_text(_pb_text(payload, 19, 2))
                if forced:
                    session_id = forced
                    break
        for idx, step_type, status, metadata, payload in rows:
            if status not in _FINAL_STEP_STATUSES:
                continue
            if step_type == _USER_STEP:
                role = "user"
                text = _pb_text(payload, 19, 2) or _pb_text(payload, 19, 3, 1)
            else:
                role = "assistant"
                text = _pb_text(payload, 20, 1) or _pb_text(payload, 20, 8)
            if not text:
                continue
            created_at = _pb_get(metadata, 1, 1)
            if not isinstance(created_at, int) or created_at <= 0:
                created_at = fallback_ts
            turn_id = _pb_text(metadata, 12)
            yield NormalizedMessage(
                uuid=f"antigravity:{cascade_id}:{idx}"
                     + (f":{turn_id}" if turn_id else ""),
                session_id=session_id,
                role=role,
                content=text,
                model="",
                created_at=created_at,
                raw={"source": "antigravity", "step_type": step_type, "idx": idx},
                origin_path=origin,
            )


ADAPTER = AntigravityAdapter()
