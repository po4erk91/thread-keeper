"""The daemon-host (Phase 1): one headless process per machine that owns the
background loops + the warm ONNX model + the embed socket. Elected via a flock;
always-on (the loops must run with no active CLI session)."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config
from . import host_embed
from .helpers import single_flight_lock

logger = logging.getLogger(__name__)

_DAEMON_STARTERS = [
    ("retention", "start_retention_daemon"),
    ("search_proxy", "start_search_proxy"),
    ("spawn_budget", "start_budget_daemon"),
    ("memory_guard", "start_memory_guard_daemon"),
    ("auto_update", "start_auto_update_daemon"),
    ("skill_watcher", "start_skill_watcher"),
    ("skill_updater", "start_skill_update_daemon"),
    ("config_watcher", "start_config_watcher"),
    ("shadow_review", "start_shadow_daemon"),
    ("curator", "start_curator_daemon"),
    ("extract_daemon", "start_extract_daemon"),
    ("candidate_reviewer", "start_candidate_reviewer_daemon"),
    ("probe_daemon", "start_probe_daemon"),
    ("evolve_daemon", "start_evolve_daemon"),
    ("evolve_applier", "start_evolve_applier_daemon"),
    ("thread_janitor", "start_thread_janitor"),
    ("dialectic_miner", "start_dialectic_miner_daemon"),
    ("dialectic_validator", "start_dialectic_validator_daemon"),
]


def start_daemons() -> list[str]:
    """Start every background loop once, in THIS process. Mirrors the block
    that used to live in identity._ensure_session; each starter is idempotent
    and single-flight-guarded, so a double call is safe."""
    started: list[str] = []
    # periodic background ingester (moved from _ensure_session)
    try:
        from . import ingest
        ingest._start_background_ingester()
        started.append("ingest")
    except Exception:
        logger.debug("host: ingest start failed", exc_info=True)
    for modname, fn in _DAEMON_STARTERS:
        try:
            mod = __import__(f"threadkeeper.{modname}", fromlist=[fn])
            getattr(mod, fn)()
            started.append(modname)
        except Exception:
            logger.debug("host: start %s failed", modname, exc_info=True)
    return started


def start_embed_server():
    """Bind the embed socket, wiring the LOCAL encoder (host role)."""
    from . import embeddings

    def _encode_texts(texts):
        arr = embeddings._encode(list(texts))
        return None if arr is None else [list(map(float, row)) for row in arr]

    return host_embed.serve_embed_socket(config.HOST_SOCK_PATH, _encode_texts)


def main() -> int:
    """Elected headless host. Exits 0 immediately if another host holds the
    lock (idempotent spawn)."""
    os.environ["THREADKEEPER_ROLE"] = "host"
    config.reload_settings()  # re-derive PROCESS_ROLE == "host"
    # Lock exactly `config.HOST_LOCK_PATH` (Task 1's shared host-election
    # path) rather than a separately-named lock, so a future liveness check
    # (Task 5's ensure_host_running()) that inspects HOST_LOCK_PATH observes
    # the same file this process holds.
    with single_flight_lock(config.HOST_LOCK_PATH.stem,
                            lock_dir=config.HOST_LOCK_PATH.parent) as locked:
        if not locked:
            return 0
        start_daemons()
        start_embed_server()
        # threading.Event (not a plain flag) so SIGTERM wakes the loop
        # immediately: Event.set() inside the handler releases the same
        # condition Event.wait() is blocked on, so the PEP-475 EINTR-retry
        # resumes against an already-satisfied wait instead of sleeping out
        # the rest of the interval (a bare time.sleep() + flag would not
        # return until the full sleep duration elapsed — up to ~30s here,
        # confirmed empirically while building this).
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        while not stop.is_set():
            _heartbeat()
            stop.wait(min(30.0, config.HOST_HEARTBEAT_TTL_S / 2))
    return 0


def _heartbeat() -> None:
    from .db import get_db
    from . import identity
    try:
        # `_ensure_session(conn, client=)` (identity.py:83) registers/refreshes
        # a presence row under the given client label — used here to stamp the
        # host's own row that `_host_alive()` (Task 5) reads back.
        identity._ensure_session(get_db(), client="daemon-host")
    except Exception:
        logger.debug("host: heartbeat failed", exc_info=True)


def ensure_host_running() -> bool:
    """Called by a thin server at session start. If no live host, spawn one
    detached and return True; else False. Idempotent under the host lock."""
    if config.PROCESS_ROLE == "host":
        return False
    if _host_alive():
        return False
    with single_flight_lock("daemon-host-spawn") as locked:
        if not locked or _host_alive():
            return False
        log = open(config.HOST_LOCK_PATH.parent / "host.log", "ab", buffering=0)
        subprocess.Popen(
            [sys.executable, "-m", "threadkeeper.host"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True,
        )
        return True


def _host_alive() -> bool:
    """A live host heartbeat within the TTL."""
    from .db import get_db
    try:
        row = get_db().execute(
            "SELECT heartbeat_at FROM presence WHERE client='daemon-host' "
            "ORDER BY heartbeat_at DESC LIMIT 1"
        ).fetchone()
    except Exception:
        return False
    if not row or row["heartbeat_at"] is None:
        return False
    return (time.time() - int(row["heartbeat_at"])) < config.HOST_HEARTBEAT_TTL_S


if __name__ == "__main__":
    raise SystemExit(main())
