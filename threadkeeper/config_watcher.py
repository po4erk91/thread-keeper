"""Hot-config reload daemon (issue #2).

Problem: the MCP server receives its env at stdio-spawn time (Claude Code
injects the `env` block of `~/.claude/settings.json` into the process). When
the user edits an env knob — e.g. `THREADKEEPER_SHADOW_REVIEW_INTERVAL_S` —
the running server never re-reads it, so a full Claude Code restart is the
only way the change takes effect.

This daemon closes that gap. It polls `~/.claude/settings.json` (a single
mtime stat per tick — cheap), and when the file changes it:

1. parses the file's `env` block,
2. mirrors the threadkeeper-relevant keys into `os.environ` (applying new
   values and dropping keys the user deleted),
3. calls `config.reload_settings()` which re-instantiates `Settings`,
   re-publishes the module constants, and propagates every changed value
   into the loaded `threadkeeper.*` modules that imported a copy, and
4. starts any daemon that was just enabled (interval 0 → >0). Daemons that
   were already running pick up a changed interval on their next tick
   (their `_serve_loop` reads the interval module-global dynamically); a
   daemon disabled at runtime (interval → 0) idles via `daemon_sleep`.

Caveats (from the roadmap item):
- Does NOT help host-CLI hooks — those are read by the CLI, not by us.
- Racy multi-writer edits are handled by debounce (poll granularity) + a
  JSON-parse guard: a half-written file is skipped and retried next tick,
  and the mtime cursor is only advanced on a clean read.
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
_last_mtime: Optional[float] = None
_applied_keys: set[str] = set()

# Unprefixed env names threadkeeper honors (see config.AliasChoices). Pulled
# from settings.json alongside every THREADKEEPER_* key.
_UNPREFIXED_KEYS: frozenset[str] = frozenset(
    {"CLAUDE_SKILLS_DIR", "CLAUDE_PROJECTS_DIR"}
)

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


def _settings_path() -> Path:
    """The watched file. `THREADKEEPER_CONFIG_WATCH_PATH` overrides; default
    is the Claude Code per-user settings (`~/.claude/settings.json`)."""
    override = config.CONFIG_WATCH_PATH
    if override:
        return Path(override).expanduser()
    return Path("~/.claude/settings.json").expanduser()


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


def run_config_watch_pass(force: bool = False) -> str:
    """Execute one watch tick synchronously (also the MCP manual trigger).

    Returns a short status string:
      - 'disabled'      — watcher off (interval<=0) and not forced
      - 'no_file'       — the settings file does not exist
      - 'unchanged'     — mtime has not moved since the last pass
      - 'initialized'   — first sighting; baseline recorded, no reload
      - 'parse_error: …'— file unreadable/half-written; retried next tick
      - 'reloaded changed=N started=[…]' — config re-read and republished
    """
    global _last_mtime, _applied_keys
    if config.CONFIG_WATCH_INTERVAL_S <= 0 and not force:
        return "disabled"

    path = _settings_path()
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return "no_file"
    except OSError as e:
        return f"stat_error: {e}"

    if not force and _last_mtime is not None and mtime == _last_mtime:
        return "unchanged"

    # First sighting (cold process): the env was already applied by the host
    # CLI at spawn, so just record the baseline + which keys are present (so a
    # later deletion can be reverted). No reload — nothing changed yet.
    if not force and _last_mtime is None:
        try:
            _applied_keys = set(_relevant_env(path))
        except (json.JSONDecodeError, OSError):
            return "parse_error: malformed settings.json (will retry)"
        _last_mtime = mtime
        return "initialized"

    try:
        desired = _relevant_env(path)
    except (json.JSONDecodeError, OSError) as e:
        # Do NOT advance the cursor — retry on the next tick once the writer
        # has finished. This is the debounce/lock guard for racy writers.
        return f"parse_error: {e}"

    remove = sorted(_applied_keys - set(desired))
    changed = config.reload_settings(env=desired, remove=remove)
    _applied_keys = set(desired)
    _last_mtime = mtime

    started = _restart_changed_daemons(changed)
    outcome = f"reloaded changed={len(changed)} started={started}"
    try:
        _record_pass(get_db(), mtime, outcome)
    except Exception:
        logger.debug("config_watch: record failed", exc_info=True)
    return outcome


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
