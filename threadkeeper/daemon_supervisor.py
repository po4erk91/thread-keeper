"""Watch enabled daemon threads and restart ones that exited.

Only this lightweight loop performs restarts.  It writes one mutable
``daemon_health`` row per daemon for cross-process status and emits an event
only when it actually restarts a thread, keeping normal telemetry quiet.
"""
from __future__ import annotations

import importlib
import logging
import time

from . import config
from .daemon_liveness import daemon_thread_alive, daemon_thread_status, start_daemon_thread
from .helpers import daemon_sleep

logger = logging.getLogger(__name__)
_started = False


def supervise_once(now: int | None = None) -> dict[str, int]:
    """Persist liveness for registered daemons and revive dead enabled ones."""
    from .agent_status import _LOOP_DEFS
    from .db import get_db

    at = int(time.time() if now is None else now)
    restarted = 0
    observed = 0
    conn = get_db()
    try:
        for loop in _LOOP_DEFS:
            interval_s = float(getattr(config, loop["interval"], 0) or 0)
            enabled = interval_s > 0
            before = daemon_thread_status(loop["thread_name"])
            after = before
            if enabled and not before["thread_alive"]:
                try:
                    module = importlib.import_module(
                        f"threadkeeper.{loop['starter_module']}"
                    )
                    getattr(module, loop["starter_name"])()
                    after = daemon_thread_status(loop["thread_name"])
                except Exception:
                    logger.warning(
                        "daemon supervisor could not restart %s", loop["id"],
                        exc_info=True,
                    )
                if after["thread_alive"]:
                    restarted += 1
                    conn.execute(
                        "INSERT INTO events (session_id, kind, target, summary, created_at) "
                        "VALUES (?, 'daemon_restarted', ?, ?, ?)",
                        (
                            "daemon-supervisor",
                            loop["id"],
                            f"thread={loop['thread_name']}",
                            at,
                        ),
                    )
            conn.execute(
                "INSERT INTO daemon_health "
                "(name, thread_alive, thread_started_at, observed_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "thread_alive=excluded.thread_alive, "
                "thread_started_at=excluded.thread_started_at, "
                "observed_at=excluded.observed_at",
                (
                    loop["id"],
                    int(bool(after["thread_alive"])),
                    after["thread_started_at"],
                    at,
                ),
            )
            observed += 1
        conn.commit()
    finally:
        conn.close()
    return {"observed": observed, "restarted": restarted}


def _serve_loop() -> None:
    while True:
        try:
            supervise_once()
        except Exception:
            logger.debug("daemon supervisor tick failed", exc_info=True)
        daemon_sleep(config.DAEMON_SUPERVISOR_INTERVAL_S)


def start_daemon_supervisor() -> None:
    """Start the opt-out daemon-thread watchdog in a foreground host."""
    global _started
    if _started and daemon_thread_alive("daemon_supervisor"):
        return
    if config.DAEMON_SUPERVISOR_INTERVAL_S <= 0:
        return
    if not config.BACKGROUND_DAEMONS_ALLOWED:
        return
    start_daemon_thread("daemon_supervisor", _serve_loop)
    _started = True
