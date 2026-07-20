"""The daemon-host (Phase 1): one headless process per machine that owns the
background loops + the warm ONNX model + the embed socket. Elected via a flock;
always-on (the loops must run with no active CLI session)."""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import config
from . import host_embed
from .helpers import single_flight_lock

logger = logging.getLogger(__name__)

# SIGTERM → wait this long for a wedged host to release the election flock
# before escalating to SIGKILL (issue #223). Module-level so tests can shrink.
_WEDGE_TERM_GRACE_S = 10.0
_WEDGE_KILL_GRACE_S = 2.0

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
    # Notifier: surfaces silent loop/spawn failures + materialization (issue #257).
    ("notify", "start_notify_daemon"),
    # Cross-machine sync: HTTP server (peers pull/push) + client reconcile loop.
    # Both no-op unless the DB is migrated and peers/token/listen are configured.
    # Centralized here so that with DAEMON_HOST_ENABLED only the host runs them —
    # otherwise every thin MCP server races to bind the listen port.
    ("sync.server", "start_server"),
    ("sync.daemon", "start_sync_daemon"),
]


def start_daemons() -> list[str]:
    """Start every background loop once, in THIS process. Mirrors the block
    that used to live in identity._ensure_session; each starter is idempotent
    and single-flight-guarded, so a double call is safe."""
    started: list[str] = []
    # periodic background ingester (moved from _ensure_session)
    try:
        from . import ingest
        ingest._start_initial_ingest()
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
        # Record pid + boot time next to the election lock so a supervisor
        # can later verify and SIGTERM a wedged-but-alive holder (#223).
        _write_host_pidfile()
        try:
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
        finally:
            _clear_host_pidfile(only_pid=os.getpid())
    return 0


def _heartbeat() -> None:
    from .db import run_write
    from . import identity
    try:
        identity.ensure_session_started(client="daemon-host")
        # Session initialization and recurring heartbeat are intentionally
        # separate: ordinary read tools no longer turn into writers.
        run_write("host-heartbeat", identity._heartbeat, deadline_s=2.0)
    except Exception:
        logger.debug("host: heartbeat failed", exc_info=True)


def ensure_host_running() -> bool:
    """Called by a thin server at session start. If no live host, spawn one
    detached and return True; else False. Idempotent under the host lock."""
    if config.PROCESS_ROLE == "host":
        return False
    if config.DISABLE_BG_DAEMONS:
        # Explicit operator pause (menu-bar power button, tests). With the
        # loops disabled there is nothing for a host to do, so don't spawn a
        # loop-less one. This gate must live HERE, before the spawn: the
        # sanitized env below deliberately clears the flag for the spawned
        # host (so a child-initiated spawn still gets a full host), which
        # would otherwise turn the pause into a no-op under host mode.
        return False
    if _host_alive():
        return False
    with single_flight_lock("daemon-host-spawn") as locked:
        if not locked or _host_alive():
            return False
        if not _recover_wedged_host():
            # A live holder remains (booting, or a verified-wedged one that
            # survived SIGKILL): a spawn could not win the election anyway.
            return False
        log = open(config.HOST_LOCK_PATH.parent / "host.log", "ab", buffering=0)
        # Sanitize the env for the detached host. If a spawned review child
        # (THREADKEEPER_SPAWNED_CHILD=1, non-foreground THREADKEEPER_WRITE_ORIGIN)
        # is the first process to call ensure_host_running(), a bare env
        # inheritance would carry those markers into the host. Since
        # config.BACKGROUND_DAEMONS_ALLOWED = not SPAWNED_CHILD and
        # WRITE_ORIGIN=="foreground" and not DISABLE_BG_DAEMONS, that would make
        # ~13 of the 18 daemon starters self-gate off in the host process — it
        # would still bind the embed socket and heartbeat (looks alive, holds
        # the election lock) while running almost none of its loops. Force a
        # clean foreground-equivalent env instead, regardless of who spawned it.
        env = os.environ.copy()
        env.pop("THREADKEEPER_SPAWNED_CHILD", None)
        env["THREADKEEPER_WRITE_ORIGIN"] = "foreground"
        env.pop("THREADKEEPER_DISABLE_BG_DAEMONS", None)
        env.pop("THREADKEEPER_NO_EMBEDDINGS", None)
        env["THREADKEEPER_ROLE"] = "host"  # belt-and-suspenders; main() also sets this
        subprocess.Popen(
            [sys.executable, "-m", "threadkeeper.host"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True,
            env=env,
        )
        return True


def _host_alive() -> bool:
    """A live host heartbeat within the TTL."""
    age = _host_heartbeat_age()
    return age is not None and age < config.HOST_HEARTBEAT_TTL_S


def _host_heartbeat_age() -> float | None:
    """Seconds since the newest daemon-host heartbeat; None when there is no
    heartbeat row (fresh install, wiped DB) or the DB is unreadable."""
    from .db import read_db
    try:
        with read_db() as conn:
            row = conn.execute(
                "SELECT heartbeat_at FROM presence WHERE client='daemon-host' "
                "ORDER BY heartbeat_at DESC LIMIT 1"
            ).fetchone()
    except Exception:
        return None
    if not row or row["heartbeat_at"] is None:
        return None
    return max(0.0, time.time() - int(row["heartbeat_at"]))


# ── Wedged-host recovery (#223) ──────────────────────────────────────────────
# A host can wedge with its heartbeat loop stuck while the process stays up
# and keeps the election flock; a plain replacement spawn then exits 0
# forever. The pidfile written by main() lets ensure_host_running() verify
# the holder and SIGTERM/SIGKILL it before spawning.


def _host_pid_path() -> Path:
    return config.HOST_LOCK_PATH.parent / "host.pid"


def _write_host_pidfile() -> None:
    path = _host_pid_path()
    payload = {"pid": os.getpid(), "started_at": int(time.time())}
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".host.pid.")
    except OSError:
        return
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError:
        logger.debug("host: pidfile write failed", exc_info=True)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _read_host_pidfile() -> dict | None:
    try:
        data = json.loads(_host_pid_path().read_text())
        pid = int(data.get("pid") or 0)
        started_at = int(data.get("started_at") or 0)
    except (OSError, ValueError, AttributeError):
        return None
    if pid <= 0:
        return None
    return {"pid": pid, "started_at": started_at}


def _clear_host_pidfile(only_pid: int | None = None) -> None:
    """Remove the pidfile; with only_pid, only when it still records that pid
    (a replacement host may have already overwritten it)."""
    try:
        if only_pid is not None:
            info = _read_host_pidfile()
            if info and info["pid"] != only_pid:
                return
        _host_pid_path().unlink(missing_ok=True)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _pid_is_host(pid: int) -> bool:
    """Guard against pid recycling: only ever signal a process whose command
    line is recognizably `python -m threadkeeper.host`. On any doubt (ps
    missing, timeout, empty output) say no — never kill unverified."""
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        return False
    return "threadkeeper.host" in (out.stdout or "")


def _wait_pid_death(pid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


def _emit_host_event(kind: str, target: int | str, summary: str) -> None:
    try:
        from .db import get_db
        from . import identity
        conn = get_db()
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?,?,?,?,?)",
            (identity._session_id or "", kind, str(target), summary[:200],
             int(time.time())),
        )
        conn.commit()
    except Exception:
        logger.debug("host: failed to emit %s", kind, exc_info=True)


def _recover_wedged_host() -> bool:
    """Clear the way for a fresh host spawn when the recorded holder is
    wedged-but-alive (#223). Returns True when a spawn attempt is worthwhile
    (nothing verified-wedged remains); False when a live holder should keep
    the election (still booting) or survived even SIGKILL.

    The kill path is deliberately narrow: heartbeat stale (or absent) beyond
    HOST_WEDGE_KILL_AFTER_S, pidfile older than that same threshold (a fresh
    boot must get time to write its first heartbeat), the process alive, and
    its command line verified as a threadkeeper host. Every other shape falls
    through to the legacy behavior — an idempotent spawn attempt that exits 0
    while a healthy holder keeps the flock. Frequency is naturally bounded:
    each replacement host resets the pidfile age, so another kill needs a
    further HOST_WEDGE_KILL_AFTER_S of proven silence."""
    wedge_s = float(getattr(config, "HOST_WEDGE_KILL_AFTER_S", 0.0) or 0.0)
    if wedge_s <= 0:
        return True
    info = _read_host_pidfile()
    if not info:
        return True
    pid = info["pid"]
    if pid == os.getpid():
        return False  # never self-kill (host role does not reach here)
    if not _pid_alive(pid):
        _clear_host_pidfile()  # crashed host left a stale pidfile behind
        return True
    age = _host_heartbeat_age()
    if age is not None and age < wedge_s:
        return True  # stale-ish, not wedge-old: let the normal spawn race
    if time.time() - info["started_at"] < wedge_s:
        return False  # holder still booting; give it time to heartbeat
    if not _pid_is_host(pid):
        return True  # pid recycled by an unrelated process: hands off it
    hb = "never" if age is None else f"{int(age)}s ago"
    _emit_host_event("host_wedged_sigterm", pid,
                     f"daemon-host pid {pid} alive with heartbeat {hb}; SIGTERM")
    logger.warning("host: SIGTERM wedged daemon-host pid %s (heartbeat %s)",
                   pid, hb)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return not _pid_alive(pid)
    if not _wait_pid_death(pid, _WEDGE_TERM_GRACE_S):
        _emit_host_event("host_wedged_sigkill", pid,
                         f"daemon-host pid {pid} survived SIGTERM; SIGKILL")
        logger.warning("host: SIGKILL wedged daemon-host pid %s", pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        if not _wait_pid_death(pid, _WEDGE_KILL_GRACE_S):
            return False
    _clear_host_pidfile(only_pid=pid)
    return True


if __name__ == "__main__":
    raise SystemExit(main())
