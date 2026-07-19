"""Hot-config reload daemon (issue #2 + #133).

Problem: the MCP server receives its env at stdio-spawn time (the host CLI
injects env into the process). When the user edits an env knob — e.g.
`THREADKEEPER_SHADOW_REVIEW_INTERVAL_S` — the running server never re-reads it,
so a full CLI restart is the only way the change takes effect.

This daemon closes that gap. It polls two independent targets (a single mtime
stat per target per tick — cheap):

1. **The universal env-file** (`~/.threadkeeper/.env`, overridable via
   `THREADKEEPER_ENV_FILE`). Every host's `Settings()` reads this file, so it
   is the one layer that hot-reloads config for all six registered clients (Claude Code /
   Desktop, Codex, Antigravity, Copilot, VS Code) — not just Claude.
   On change we call `config.reload_settings()` with NO os.environ mirroring:
   pydantic re-reads the file natively and real spawn-time env vars keep their
   precedence (env var > .env file > default). No precedence inversion.

2. **The host CLI's own env-block file**, resolved from the active-CLI
   identity (Claude Code → `~/.claude/settings.json`). Its `env` block is
   mirrored into `os.environ` and republished — MINUS any key a
   higher-priority layer already pinned at spawn (see below). CLIs whose env
   block we cannot yet parse as a flat map rely on target 1 alone (they still
   hot-reload, just via the universal `.env`).

`THREADKEEPER_CONFIG_WATCH_PATH` is an escape hatch / test seam: when set it
pins ONE file (watched as a CLI-settings target), and the universal env-file
target is skipped — legacy single-file behavior.

On a real change the daemon:

1. parses the changed file (target 2) or re-reads natively (target 1),
2. for target 2, mirrors the threadkeeper-relevant keys into `os.environ`
   (applying new values and dropping keys the user deleted), excluding keys a
   higher-priority scope pinned at spawn (issue #133 precedence guard),
3. calls `config.reload_settings()` which re-instantiates `Settings`,
   re-publishes the module constants, and propagates every changed value into
   the loaded `threadkeeper.*` modules that imported a copy, and
4. starts any daemon that was just enabled (interval 0 → >0). Daemons that
   were already running pick up a changed interval on their next tick (their
   `_serve_loop` reads the interval module-global dynamically); a daemon
   disabled at runtime (interval → 0) idles via `daemon_sleep`.

Caveats (from the roadmap item):
- Does NOT help host-CLI hooks — those are read by the CLI, not by us.
- For target 2 (Claude), only the USER-level settings file is read; a knob set
  at a higher scope (project/local/managed) is honored via the spawn-time
  precedence guard but its later edits are not tracked. Put runtime-tunable
  knobs in the universal `.env` for unambiguous cross-scope hot-reload.
- Racy multi-writer edits are handled by debounce (poll granularity) + a
  JSON-parse guard: a half-written file is skipped and retried next tick, and
  the mtime cursor is only advanced on a clean read.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from . import config
from .db import get_db
from .helpers import daemon_sleep
from . import identity

logger = logging.getLogger(__name__)

_started = False

# Per-process watcher state. Each MCP server watches and reloads its OWN
# in-process config — this is intentionally not shared via the DB.
#
# Two independent watch targets, each with its own mtime cursor:
#   * env-file (universal ~/.threadkeeper/.env) — reloaded natively, no
#     os.environ mirroring, so real spawn-time env keeps precedence.
#   * cli-settings (host CLI env-block file via identity) — mirrored into
#     os.environ, minus keys a higher-priority layer pinned at spawn.
_env_last_mtime: Optional[float] = None      # env-file target cursor
_last_mtime: Optional[float] = None          # cli-settings target cursor
_applied_keys: set[str] = set()              # keys mirrored from the cli file
_shadowed_keys: set[str] = set()             # cli-file keys a higher layer pins

# Unprefixed env names threadkeeper honors (see config.AliasChoices). Pulled
# from settings.json alongside every THREADKEEPER_* key.
_UNPREFIXED_KEYS: frozenset[str] = frozenset(
    {"CLAUDE_SKILLS_DIR", "CLAUDE_PROJECTS_DIR"}
)

# Host CLI -> its env-block settings file (relative to $HOME). Only CLIs whose
# env block we can parse as a flat {key: value} JSON map appear here. Others
# rely on the universal env-file target — they still hot-reload, just via
# ~/.threadkeeper/.env rather than their own config file.
_CLI_SETTINGS_FILE: dict[str, str] = {
    "claude": "~/.claude/settings.json",
}

# Interval constant -> (module, start-fn). After a reload, a daemon whose
# interval crossed 0 → >0 is (re)started here; the rest self-adjust on their
# next tick. Kept in sync with identity._ensure_session's daemon wiring.
_DAEMON_FOR_INTERVAL: dict[str, tuple[str, str]] = {
    "SHADOW_REVIEW_INTERVAL_S": ("shadow_review", "start_shadow_daemon"),
    "CURATOR_INTERVAL_S": ("curator", "start_curator_daemon"),
    "EXTRACT_INTERVAL_S": ("extract_daemon", "start_extract_daemon"),
    "CANDIDATE_REVIEW_INTERVAL_S": (
        "candidate_reviewer", "start_candidate_reviewer_daemon"),
    "PROBE_INTERVAL_S": ("probe_daemon", "start_probe_daemon"),
    "EVOLVE_REVIEW_INTERVAL_S": ("evolve_daemon", "start_evolve_daemon"),
    "EVOLVE_APPLY_INTERVAL_S": ("evolve_applier", "start_evolve_applier_daemon"),
    "THREAD_JANITOR_INTERVAL_S": ("thread_janitor", "start_thread_janitor"),
    "DIALECTIC_MINE_INTERVAL_S": (
        "dialectic_miner", "start_dialectic_miner_daemon"),
    "DIALECTIC_VALIDATE_INTERVAL_S": (
        "dialectic_validator", "start_dialectic_validator_daemon"),
    "RETENTION_INTERVAL_S": ("retention", "start_retention_daemon"),
}


def _env_file_path() -> Path:
    """The universal env-file every host's `Settings()` reads (precedence:
    real env > this file > defaults). Path comes from `config._ENV_FILE`, which
    already honors the `THREADKEEPER_ENV_FILE` override."""
    return Path(config._ENV_FILE).expanduser()


def _cli_settings_path() -> Optional[Path]:
    """The host CLI's own env-block file, resolved from the active-CLI
    identity. `THREADKEEPER_CONFIG_WATCH_PATH` overrides it (and, when set, is
    the ONLY file watched — see run_config_watch_pass). Returns None when the
    host CLI has no env-block file we parse here."""
    override = config.CONFIG_WATCH_PATH
    if override:
        return Path(override).expanduser()
    cli = identity.active_cli()
    rel = _CLI_SETTINGS_FILE.get(cli or "")
    return Path(rel).expanduser() if rel else None


def _is_relevant(key: str) -> bool:
    return key.startswith("THREADKEEPER_") or key in _UNPREFIXED_KEYS


def _relevant_env(path: Path) -> dict[str, str]:
    """Parse the settings file's `env` block down to the threadkeeper knobs.

    Raises `json.JSONDecodeError` on a malformed (e.g. half-written) file so
    the caller can skip and retry. A missing `env` block yields `{}`.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    env = data.get("env") if isinstance(data, dict) else None
    if not isinstance(env, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in env.items()
        if _is_relevant(str(k)) and v is not None
    }


def _record_pass(conn: sqlite3.Connection, mtime: float, outcome: str) -> None:
    """Write a `config_watch_pass` event for observability (status tool +
    dashboards). `target` carries the mtime cursor, `summary` the outcome."""
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, 'config_watch_pass', ?, ?, ?)",
            (identity._session_id or "", f"{mtime:.3f}",
             outcome[:300], int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("config_watch: failed to record pass", exc_info=True)


def _restart_changed_daemons(changed: dict) -> list[str]:
    """Start daemons whose interval just crossed 0 → >0. Returns the names of
    daemons (re)started. Already-running daemons need nothing: their loop
    reads the interval global dynamically each tick."""
    started: list[str] = []
    for const, (mod_name, fn_name) in _DAEMON_FOR_INTERVAL.items():
        delta = changed.get(const)
        if not delta:
            continue
        old = delta.get("old") or 0
        new = delta.get("new") or 0
        try:
            became_enabled = float(old) <= 0 < float(new)
        except (TypeError, ValueError):
            continue
        if not became_enabled:
            continue
        try:
            mod = __import__(f"threadkeeper.{mod_name}", fromlist=[fn_name])
            getattr(mod, fn_name)()
            started.append(mod_name)
        except Exception:
            logger.debug("config_watch: start %s failed", mod_name, exc_info=True)
    return started


def _tick_env_file(force: bool) -> Optional[str]:
    """Watch the universal env-file. On change, reload `Settings()` natively —
    NO os.environ mirroring, so the real spawn-time env still wins (pydantic
    precedence: env vars > .env file). Returns a per-target status string, or
    None when the file is absent (nothing to watch)."""
    global _env_last_mtime
    path = _env_file_path()
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"stat_error: {e}"

    if not force and _env_last_mtime is not None and mtime == _env_last_mtime:
        return "unchanged"

    # First sighting (cold process): the file was already loaded by Settings()
    # at spawn, so just record the baseline. No reload — nothing changed yet.
    if not force and _env_last_mtime is None:
        _env_last_mtime = mtime
        return "initialized"

    # Native re-read: reload_settings() re-instantiates Settings(), which reads
    # os.environ + the .env file fresh. Passing no env/remove means we never
    # mirror the (lower-priority) file into os.environ.
    changed = config.reload_settings()
    _env_last_mtime = mtime
    started = _restart_changed_daemons(changed)
    return f"reloaded changed={len(changed)} started={started}"


def _tick_cli_settings(force: bool) -> Optional[str]:
    """Watch the host CLI's env-block file and mirror its keys into os.environ.

    Keys a higher-priority scope pinned at spawn (os.environ disagrees with
    this file's value at first sighting) are excluded so the lower-priority
    user-level file never silently overrides them (issue #133 problem 1).

    Returns a per-target status string, or None when there is no CLI env-block
    file for this host (the universal env-file target covers it instead).
    """
    global _last_mtime, _applied_keys, _shadowed_keys
    path = _cli_settings_path()
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return "no_file"
    except OSError as e:
        return f"stat_error: {e}"

    if not force and _last_mtime is not None and mtime == _last_mtime:
        return "unchanged"

    # First sighting (cold process): the env was already applied by the host
    # CLI at spawn. Record the baseline + which keys are present (so a later
    # deletion can be reverted) + which keys a HIGHER-priority scope pins
    # (os.environ already holds a different value) so we never mirror the
    # lower-priority file value over them. No reload — nothing changed yet.
    if not force and _last_mtime is None:
        try:
            present = _relevant_env(path)
        except (json.JSONDecodeError, OSError):
            return "parse_error: malformed settings.json (will retry)"
        _shadowed_keys = {
            k for k, v in present.items()
            if os.environ.get(k) not in (None, v)
        }
        _applied_keys = set(present) - _shadowed_keys
        _last_mtime = mtime
        return "initialized"

    try:
        present = _relevant_env(path)
    except (json.JSONDecodeError, OSError) as e:
        # Do NOT advance the cursor — retry on the next tick once the writer
        # has finished. This is the debounce/lock guard for racy writers.
        return f"parse_error: {e}"

    desired = {k: v for k, v in present.items() if k not in _shadowed_keys}
    remove = sorted(_applied_keys - set(desired))
    changed = config.reload_settings(env=desired, remove=remove)
    _applied_keys = set(desired)
    _last_mtime = mtime

    started = _restart_changed_daemons(changed)
    return f"reloaded changed={len(changed)} started={started}"


def run_config_watch_pass(force: bool = False) -> str:
    """Execute one watch tick synchronously (also the MCP manual trigger).

    Ticks the watch targets and returns a status string. In the default
    (hybrid) mode it ticks BOTH the universal env-file and the host CLI's
    env-block file and returns a combined string (``env=… cli=…``); each
    target keeps its own mtime cursor, so an edit to either is picked up
    independently. When `THREADKEEPER_CONFIG_WATCH_PATH` pins one file, that
    single file is watched and its status is returned verbatim (legacy mode).

    Per-target status vocabulary:
      - 'disabled'      — watcher off (interval<=0) and not forced
      - 'no_file'       — the CLI settings file does not exist
      - 'unchanged'     — mtime has not moved since the last pass
      - 'initialized'   — first sighting; baseline recorded, no reload
      - 'parse_error: …'— file unreadable/half-written; retried next tick
      - 'reloaded changed=N started=[…]' — config re-read and republished
    """
    if config.CONFIG_WATCH_INTERVAL_S <= 0 and not force:
        return "disabled"

    # Escape hatch / test seam: CONFIG_WATCH_PATH pins ONE file. Watch only it
    # (as a cli-settings target) and return its status verbatim.
    if config.CONFIG_WATCH_PATH:
        outcome = _tick_cli_settings(force) or "no_file"
        if "reloaded" in outcome:
            _record_outcome(outcome, _last_mtime)
        return outcome

    # Default (production): dual watch — universal env-file + host CLI file.
    parts: list[str] = []
    env_status = _tick_env_file(force)
    if env_status is not None:
        parts.append(f"env={env_status}")
    cli_status = _tick_cli_settings(force)
    if cli_status is not None:
        parts.append(f"cli={cli_status}")

    outcome = " ".join(parts) if parts else "no_targets"
    if "reloaded" in outcome:
        _record_outcome(outcome, max(_env_last_mtime or 0.0, _last_mtime or 0.0))
    return outcome


def _record_outcome(outcome: str, mtime: Optional[float]) -> None:
    """Persist a reload outcome, swallowing DB errors (observability only)."""
    try:
        _record_pass(get_db(), mtime or 0.0, outcome)
    except Exception:
        logger.debug("config_watch: record failed", exc_info=True)


def _serve_loop() -> None:
    """Daemon body. Sleep → tick → sleep, until process dies."""
    while True:
        try:
            run_config_watch_pass()
        except Exception:
            logger.debug("config_watcher tick failed", exc_info=True)
        daemon_sleep(config.CONFIG_WATCH_INTERVAL_S)


def start_config_watcher() -> None:
    """Idempotent starter. No-op when CONFIG_WATCH_INTERVAL_S<=0 or in a
    non-foreground process (spawned children must not hot-reload config)."""
    global _started
    if _started:
        return
    if config.CONFIG_WATCH_INTERVAL_S <= 0:
        return
    if not config.BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(
        target=_serve_loop, name="config_watcher", daemon=True,
    )
    t.start()
    _started = True
