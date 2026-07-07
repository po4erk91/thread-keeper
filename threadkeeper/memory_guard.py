"""RSS guard for thread-keeper server processes.

The spawn budget protects child agents. This guard protects the MCP server
processes themselves: if a server's RSS grows past configured thresholds, it
notifies the user and can terminate the offending process.
"""
from __future__ import annotations

import logging
import gc
import importlib
import linecache
import os
import signal as _sig
import subprocess
import sys
import threading
import time

from . import identity
from .config import (
    BACKGROUND_DAEMONS_ALLOWED,
    MEMORY_GUARD_AGG_KILL_MB,
    MEMORY_GUARD_AGG_WARN_MB,
    MEMORY_GUARD_COOLDOWN_S,
    MEMORY_GUARD_EMBED_HOT_S,
    MEMORY_GUARD_KILL_MB,
    MEMORY_GUARD_NOTIFY,
    MEMORY_GUARD_POLL_S,
    MEMORY_GUARD_RECLAIM_MB,
    MEMORY_GUARD_RETIRE_IDLE_S,
    MEMORY_GUARD_RETIRE_LIVE,
    MEMORY_GUARD_TARGET_SERVERS,
    MEMORY_GUARD_WARN_MB,
    TASK_LOG_DIR,
)
from .db import get_db
from .helpers import daemon_sleep
from . import process_health

logger = logging.getLogger(__name__)

_started = False
_last_notify_at: dict[tuple[int, str], float] = {}

# Reclaim effectiveness back-off. On this allocator stack (macOS +
# fastembed/ONNX) an unload can be net-negative: gc.collect() re-faults
# OS-compressed pages back to resident, and with an active ingester the model
# reloads within seconds while the freed arenas are still mapped. Observed in
# production as every reclaim ADDING 200-330MB, 2000+ times a week. When a
# reclaim frees nothing, back off exponentially instead of thrash-repeating
# on every cooldown tick; an effective reclaim resets the streak.
_RECLAIM_BACKOFF_BASE_S = 1800.0
_RECLAIM_BACKOFF_MAX_S = 4 * 3600.0
_reclaim_backoff_until = 0.0
_reclaim_fail_streak = 0


def _rss_mb(p: dict) -> int:
    return int(p.get("rss_kb") or 0) // 1024


def _pid_rss_mb(pid: int) -> int:
    try:
        r = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.SubprocessError, OSError):
        return 0
    txt = (r.stdout or "").strip()
    if not txt:
        return 0
    try:
        return int(txt.split()[0]) // 1024
    except (ValueError, IndexError):
        return 0


def _log_line(line: str) -> None:
    try:
        TASK_LOG_DIR.mkdir(parents=True, exist_ok=True)
        fp = TASK_LOG_DIR / "memory-guard.log"
        with fp.open("a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except OSError:
        logger.debug("memory_guard: failed to append log", exc_info=True)


def _emit_event(kind: str, target: int | str, summary: str) -> None:
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?,?,?,?,?)",
            (
                identity._session_id or "",
                kind,
                str(target),
                summary[:200],
                int(time.time()),
            ),
        )
        conn.commit()
    except Exception:
        logger.debug("memory_guard: failed to emit event", exc_info=True)


def _notify_user(title: str, message: str) -> bool:
    """Best-effort desktop notification. Returns true when dispatched."""
    if not MEMORY_GUARD_NOTIFY:
        return False
    if os.uname().sysname != "Darwin":
        return False
    # osascript string literals need backslash and quote escaping.
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_msg}" with title "{safe_title}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        logger.debug("memory_guard: notification failed", exc_info=True)
        return False


def _prune_notify_state(now: float) -> None:
    """Bound `_last_notify_at`. It is only ever inserted into, so on the
    long-lived aggregate-guard coordinator each transient MCP `(pid, level)`
    that ever crossed a threshold would persist forever — slow unbounded
    growth in the one process that should stay lean (#86). An entry only
    matters until its cooldown lapses (after that `_maybe_notify` would notify
    again regardless), and an entry for a dead pid can never matter, so both
    are safe to drop. Window falls back to 1h when the cooldown is disabled."""
    window = MEMORY_GUARD_COOLDOWN_S if MEMORY_GUARD_COOLDOWN_S > 0 else 3600.0
    drop = []
    for (pid, level), last in _last_notify_at.items():
        if now - last >= window or not _pid_alive(pid):
            drop.append((pid, level))
    for key in drop:
        _last_notify_at.pop(key, None)


def _pid_alive(pid: int) -> bool:
    """Cheap liveness check (no subprocess) for notify-state pruning."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _maybe_notify(pid: int, level: str, message: str, *, force: bool = False) -> None:
    now = time.time()
    _prune_notify_state(now)
    key = (pid, level)
    last = _last_notify_at.get(key, 0)
    if not force and MEMORY_GUARD_COOLDOWN_S > 0 and now - last < MEMORY_GUARD_COOLDOWN_S:
        return
    _last_notify_at[key] = now
    _notify_user("thread-keeper memory guard", message)
    _log_line(f"{int(now)} {level} pid={pid} {message}")


def _allocator_pressure_relief() -> list[str]:
    """Ask the platform allocator to return free arenas where possible."""
    actions: list[str] = []
    try:
        import ctypes
        if sys.platform == "darwin":
            lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            fn = getattr(lib, "malloc_zone_pressure_relief", None)
            if fn is not None:
                fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
                fn.restype = ctypes.c_size_t
                fn(None, 0)
                actions.append("malloc_zone_pressure_relief")
        elif sys.platform.startswith("linux"):
            lib = ctypes.CDLL("libc.so.6")
            fn = getattr(lib, "malloc_trim", None)
            if fn is not None:
                fn.argtypes = [ctypes.c_size_t]
                fn.restype = ctypes.c_int
                fn(0)
                actions.append("malloc_trim")
    except Exception:
        logger.debug("memory_guard: allocator pressure relief failed", exc_info=True)
    return actions


def _empty_torch_caches() -> list[str]:
    actions: list[str] = []
    torch = sys.modules.get("torch")
    if torch is None:
        return actions
    try:
        cuda = getattr(torch, "cuda", None)
        if cuda is not None and callable(getattr(cuda, "empty_cache", None)):
            cuda.empty_cache()
            actions.append("torch.cuda.empty_cache")
    except Exception:
        logger.debug("memory_guard: torch cuda cache trim failed", exc_info=True)
    try:
        mps = getattr(torch, "mps", None)
        if mps is not None and callable(getattr(mps, "empty_cache", None)):
            mps.empty_cache()
            actions.append("torch.mps.empty_cache")
    except Exception:
        logger.debug("memory_guard: torch mps cache trim failed", exc_info=True)
    return actions


def reclaim_memory(reason: str = "manual", force: bool = False) -> dict:
    """Best-effort local trim: unload embeddings, clear caches, run GC.

    This cannot guarantee RSS shrinkage because Python/PyTorch allocators may
    retain arenas for reuse. It does make heavyweight objects collectible and
    asks the OS allocator to release free pages where supported.

    Two guards keep the trim from going net-negative: a HOT embedding model
    (used within MEMORY_GUARD_EMBED_HOT_S) is never unloaded — an active
    ingester would reload a fresh copy seconds later while the freed arenas
    are still resident — and after a reclaim that freed nothing the process
    backs off exponentially. `force=True` (manual tool calls) bypasses both.
    """
    global _reclaim_backoff_until, _reclaim_fail_streak
    now = time.time()
    if not force:
        skip = None
        if now < _reclaim_backoff_until:
            skip = (
                f"backoff({int(_reclaim_backoff_until - now)}s_left"
                f"_after_{_reclaim_fail_streak}_ineffective)"
            )
        else:
            try:
                from . import embeddings
                if embeddings.model_loaded() and (
                    now - embeddings.last_used_at() < MEMORY_GUARD_EMBED_HOT_S
                ):
                    skip = "model_hot"
            except Exception:
                logger.debug(
                    "memory_guard: embed-hot check failed", exc_info=True
                )
        if skip:
            rss = _pid_rss_mb(os.getpid())
            _log_line(
                f"{int(now)} reclaim_skip pid={os.getpid()} rss={rss}MB "
                f"reason={reason} skip={skip}"
            )
            return {
                "pid": os.getpid(),
                "reason": reason,
                "before_mb": rss,
                "after_mb": rss,
                "freed_mb": 0,
                "actions": [f"skipped:{skip}"],
                "skipped": skip,
            }
    before = _pid_rss_mb(os.getpid())
    actions: list[str] = []
    try:
        from . import embeddings
        if embeddings.unload_model():
            actions.append("embeddings.unload_model")
    except Exception:
        logger.debug("memory_guard: embedding model unload failed", exc_info=True)

    actions.extend(_empty_torch_caches())
    linecache.clearcache()
    importlib.invalidate_caches()
    actions.append("python.cache_clear")
    clear_internal = getattr(sys, "_clear_internal_caches", None)
    if callable(clear_internal):
        try:
            clear_internal()
            actions.append("sys._clear_internal_caches")
        except Exception:
            logger.debug("memory_guard: sys cache clear failed", exc_info=True)
    collected = gc.collect()
    actions.append(f"gc.collect={collected}")
    actions.extend(_allocator_pressure_relief())
    after = _pid_rss_mb(os.getpid())
    freed = before - after
    if freed <= 0 and before > 0:
        _reclaim_fail_streak += 1
        backoff = min(
            _RECLAIM_BACKOFF_MAX_S,
            _RECLAIM_BACKOFF_BASE_S * (2 ** (_reclaim_fail_streak - 1)),
        )
        _reclaim_backoff_until = time.time() + backoff
        actions.append(f"backoff={int(backoff)}s")
    else:
        _reclaim_fail_streak = 0
        _reclaim_backoff_until = 0.0
    result = {
        "pid": os.getpid(),
        "reason": reason,
        "before_mb": before,
        "after_mb": after,
        "freed_mb": max(0, freed),
        "actions": actions,
    }
    _log_line(
        f"{int(time.time())} reclaim pid={os.getpid()} "
        f"before={before}MB after={after}MB freed={freed}MB reason={reason}"
    )
    _emit_event(
        "memory_reclaim", os.getpid(),
        f"before={before}MB after={after}MB freed={freed}MB reason={reason}",
    )
    return result


def _pending_recent_control(conn, action: str, pid: int, now: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM resource_controls "
        "WHERE action=? AND target_pid=? AND created_at>=? "
        "ORDER BY id DESC LIMIT 1",
        (action, pid, now - max(1, MEMORY_GUARD_COOLDOWN_S)),
    ).fetchone()
    return row is not None


def _recent_event(conn, kind: str, target: str, now: int) -> bool:
    if MEMORY_GUARD_COOLDOWN_S <= 0:
        return False
    row = conn.execute(
        "SELECT 1 FROM events "
        "WHERE kind=? AND target=? AND created_at>=? "
        "ORDER BY id DESC LIMIT 1",
        (kind, target, now - max(1, MEMORY_GUARD_COOLDOWN_S)),
    ).fetchone()
    return row is not None


def request_reclaim(procs: list[dict] | None = None, reason: str = "manual") -> dict:
    """Queue trim requests for the given process rows, deduped by cooldown."""
    if procs is None:
        procs = process_health.scan()
    conn = get_db()
    now = int(time.time())
    inserted: list[int] = []
    for p in procs:
        pid = int(p["pid"])
        if _pending_recent_control(conn, "trim", pid, now):
            continue
        conn.execute(
            "INSERT INTO resource_controls "
            "(action, target_pid, reason, created_at, expires_at) "
            "VALUES (?,?,?,?,?)",
            ("trim", pid, reason[:200], now, now + 120),
        )
        inserted.append(pid)
    conn.commit()
    return {"requested": inserted, "count": len(inserted), "reason": reason}


def handle_resource_controls() -> list[dict]:
    """Handle trim requests addressed to this process."""
    conn = get_db()
    now = int(time.time())
    rows = conn.execute(
        "SELECT id, action, reason FROM resource_controls "
        "WHERE target_pid=? AND handled_at IS NULL AND expires_at>=? "
        "ORDER BY id",
        (os.getpid(), now),
    ).fetchall()
    handled: list[dict] = []
    for r in rows:
        if r["action"] != "trim":
            continue
        result = reclaim_memory(reason=f"control:{r['reason'] or 'trim'}")
        summary = (
            f"before={result['before_mb']}MB after={result['after_mb']}MB "
            f"freed={result['freed_mb']}MB"
        )
        conn.execute(
            "UPDATE resource_controls SET handled_at=?, result=? WHERE id=?",
            (int(time.time()), summary[:200], r["id"]),
        )
        handled.append({"id": r["id"], **result})
    if handled:
        conn.commit()
    return handled


def _aggregate_state(procs: list[dict]) -> dict:
    total = sum(_rss_mb(p) for p in procs)
    return {
        "rss_mb": total,
        "warn": MEMORY_GUARD_AGG_WARN_MB > 0 and total >= MEMORY_GUARD_AGG_WARN_MB,
        "kill": MEMORY_GUARD_AGG_KILL_MB > 0 and total >= MEMORY_GUARD_AGG_KILL_MB,
        "warn_mb": MEMORY_GUARD_AGG_WARN_MB,
        "kill_mb": MEMORY_GUARD_AGG_KILL_MB,
        "target_servers": MEMORY_GUARD_TARGET_SERVERS,
        "retire_idle_s": MEMORY_GUARD_RETIRE_IDLE_S,
        "retire_live": MEMORY_GUARD_RETIRE_LIVE,
    }


def _global_guard_coordinator_pid(procs: list[dict]) -> int | None:
    candidates = [
        int(p["pid"])
        for p in procs
        if int(p.get("pid") or 0) > 0 and not p.get("is_orphaned")
    ]
    if not candidates:
        candidates = [int(p["pid"]) for p in procs if int(p.get("pid") or 0) > 0]
    return min(candidates) if candidates else None


def is_global_guard_coordinator(procs: list[dict]) -> bool:
    """True when this server owns process-wide aggregate side effects.

    Every MCP server runs its own local daemon. Without coordination, N open
    conversations produce N aggregate warnings, N trim sweeps, and racing retire
    attempts. The oldest non-orphaned server is the single coordinator; if the
    scanner somehow misses this process, treat the current manual caller as the
    coordinator so diagnostics/tests still apply side effects.
    """
    self_pid = os.getpid()
    pids = {int(p["pid"]) for p in procs if int(p.get("pid") or 0) > 0}
    if self_pid not in pids:
        return True
    return self_pid == _global_guard_coordinator_pid(procs)


def _idle_retire_candidates(procs: list[dict]) -> list[dict]:
    from . import config as _cfg

    if _cfg.DAEMON_HOST_ENABLED:
        # Under daemon-host mode, thin servers are cheap and are never
        # idle-retired; the single host is supervised separately via
        # supervise_host(), not this path.
        return []

    candidates: list[dict] = []
    for p in procs:
        if p.get("is_self"):
            continue
        if p.get("parent_alive") and not MEMORY_GUARD_RETIRE_LIVE:
            continue
        hb = p.get("heartbeat_age_s")
        if hb is None or hb >= MEMORY_GUARD_RETIRE_IDLE_S:
            candidates.append(dict(p, rss_mb=_rss_mb(p)))

    def key(p: dict) -> tuple[int, int, int]:
        hb = p.get("heartbeat_age_s")
        no_hb_first = 0 if hb is None else 1
        hb_age = int(hb or 0)
        return (no_hb_first, -hb_age, -int(p.get("rss_mb") or 0))

    return sorted(candidates, key=key)


def _retire_plan(procs: list[dict], aggregate: dict) -> list[dict]:
    target_servers = max(1, int(MEMORY_GUARD_TARGET_SERVERS or 1))
    if len(procs) <= target_servers:
        return []
    if not aggregate["warn"] and not aggregate["kill"]:
        return []
    total = int(aggregate["rss_mb"])
    count = len(procs)
    target_mb = int(aggregate["warn_mb"] or 0)
    plan: list[dict] = []
    for p in _idle_retire_candidates(procs):
        if count <= target_servers:
            break
        if aggregate["kill"] is False and target_mb > 0 and total <= target_mb:
            break
        plan.append(p)
        total -= int(p.get("rss_mb") or 0)
        count -= 1
    return plan


def _host_alive() -> bool:
    from . import host
    return host._host_alive()


def supervise_host() -> None:
    """Respawn the daemon-host if its heartbeat is stale (flag on only).

    Phase 1: with THREADKEEPER_DAEMON_HOST on, thin per-session servers are
    never idle-retire targets (see `_idle_retire_candidates`), so the guard
    against a wedged/crashed host is this: if the host's presence heartbeat
    has gone stale, spawn a replacement. No-op when the flag is off or the
    host is already alive.
    """
    from . import config as _cfg
    if not _cfg.DAEMON_HOST_ENABLED:
        return
    if _host_alive():
        return
    try:
        from . import host
        host.ensure_host_running()
    except Exception:
        pass


def scan_over_limit() -> dict:
    """Return classified process rows split by warn/kill threshold."""
    procs = []
    for p in process_health.scan():
        d = dict(p)
        d["rss_mb"] = _rss_mb(d)
        procs.append(d)
    warn: list[dict] = []
    kill: list[dict] = []
    for p in procs:
        rss = p["rss_mb"]
        if MEMORY_GUARD_KILL_MB > 0 and rss >= MEMORY_GUARD_KILL_MB:
            kill.append(p)
        elif MEMORY_GUARD_WARN_MB > 0 and rss >= MEMORY_GUARD_WARN_MB:
            warn.append(p)
    aggregate = _aggregate_state(procs)
    retire = _retire_plan(procs, aggregate)
    coordinator = is_global_guard_coordinator(procs)
    return {
        "procs": procs,
        "warn": warn,
        "kill": kill,
        "aggregate": aggregate,
        "retire": retire,
        "coordinator": coordinator,
        "coordinator_pid": _global_guard_coordinator_pid(procs),
        "warn_mb": MEMORY_GUARD_WARN_MB,
        "kill_mb": MEMORY_GUARD_KILL_MB,
        "reclaim_mb": MEMORY_GUARD_RECLAIM_MB,
        "poll_s": MEMORY_GUARD_POLL_S,
        "notify": MEMORY_GUARD_NOTIFY,
    }


def check_once(*, dry_run: bool = True, notify: bool = True) -> dict:
    """Run one guard pass.

    dry_run=True reports offenders without killing. dry_run=False sends
    SIGTERM to processes over the kill threshold. Warning-threshold rows are
    never killed.
    """
    handled_controls: list[dict] = []
    if not dry_run:
        handled_controls = handle_resource_controls()

    result = scan_over_limit()
    is_coordinator = bool(result.get("coordinator"))
    killed: list[int] = []
    failed: list[dict] = []
    retired: list[int] = []
    skipped: list[dict] = []
    reclaim_requests: dict = {"requested": [], "count": 0, "reason": ""}
    local_reclaim: dict | None = None

    for p in result["warn"]:
        if p["pid"] != os.getpid():
            continue
        msg = (
            f"pid {p['pid']} RSS {p['rss_mb']}MB crossed warn "
            f"threshold {MEMORY_GUARD_WARN_MB}MB"
        )
        if not dry_run:
            _emit_event("memory_guard_warn", p["pid"], msg)
            if p["pid"] == os.getpid() and p["rss_mb"] >= MEMORY_GUARD_RECLAIM_MB:
                local_reclaim = reclaim_memory(reason="local_warn")
        if notify and not dry_run:
            _maybe_notify(p["pid"], "warn", msg)

    aggregate = result["aggregate"]
    if aggregate["warn"] and is_coordinator:
        msg = (
            f"aggregate RSS {aggregate['rss_mb']}MB crossed warn "
            f"threshold {aggregate['warn_mb']}MB across "
            f"{len(result['procs'])} server process(es)"
        )
        if not dry_run:
            conn = get_db()
            now = int(time.time())
            if not _recent_event(conn, "memory_guard_aggregate_warn", "aggregate", now):
                _emit_event("memory_guard_aggregate_warn", "aggregate", msg)
                reclaim_requests = request_reclaim(
                    result["procs"], reason="aggregate_warn"
                )
                if any(p["pid"] == os.getpid() for p in result["procs"]):
                    local_reclaim = reclaim_memory(reason="aggregate_warn")
                if notify:
                    _maybe_notify(os.getpid(), "aggregate_warn", msg)

    kill_rows = sorted(
        result["kill"], key=lambda p: p["pid"] == os.getpid()
    )
    for p in kill_rows:
        msg = (
            f"pid {p['pid']} RSS {p['rss_mb']}MB crossed kill "
            f"threshold {MEMORY_GUARD_KILL_MB}MB"
        )
        if dry_run:
            continue
        if p["pid"] != os.getpid() and not is_coordinator:
            continue
        if not process_health.is_threadkeeper_server_pid(p["pid"]):
            skipped.append({
                "pid": p["pid"],
                "action": "kill",
                "reason": "pid_no_longer_threadkeeper_server",
            })
            continue
        _emit_event("memory_guard_kill", p["pid"], msg)
        if notify:
            _maybe_notify(p["pid"], "kill", msg, force=True)
        try:
            sent, reason = process_health.signal_if_threadkeeper(
                p["pid"], _sig.SIGTERM
            )
            if sent:
                killed.append(p["pid"])
            else:
                skipped.append({
                    "pid": p["pid"],
                    "action": "kill",
                    "reason": reason,
                })
        except (ProcessLookupError, PermissionError, OSError) as e:
            failed.append({"pid": p["pid"], "err": str(e)})

    if aggregate["warn"] and is_coordinator and result["retire"]:
        for p in result["retire"]:
            msg = (
                f"aggregate RSS {aggregate['rss_mb']}MB; retiring idle "
                f"pid {p['pid']} rss={p['rss_mb']}MB "
                f"hb={p.get('heartbeat_age_s')}"
            )
            if dry_run:
                continue
            if not process_health.is_threadkeeper_server_pid(p["pid"]):
                skipped.append({
                    "pid": p["pid"],
                    "action": "retire",
                    "reason": "pid_no_longer_threadkeeper_server",
                })
                continue
            _emit_event("memory_guard_retire_idle", p["pid"], msg)
            if notify:
                _maybe_notify(p["pid"], "retire", msg, force=aggregate["kill"])
            try:
                sent, reason = process_health.signal_if_threadkeeper(
                    p["pid"], _sig.SIGTERM
                )
                if sent:
                    retired.append(p["pid"])
                else:
                    skipped.append({
                        "pid": p["pid"],
                        "action": "retire",
                        "reason": reason,
                    })
            except (ProcessLookupError, PermissionError, OSError) as e:
                failed.append({"pid": p["pid"], "err": str(e)})

    result["killed"] = killed
    result["retired"] = retired
    result["failed"] = failed
    result["skipped"] = skipped
    result["dry_run"] = dry_run
    result["reclaim_requests"] = reclaim_requests
    result["local_reclaim"] = local_reclaim
    result["handled_controls"] = handled_controls
    return result


def _daemon_loop() -> None:
    while True:
        try:
            supervise_host()
        except Exception:
            logger.debug("memory_guard: supervise_host failed", exc_info=True)
        try:
            check_once(dry_run=False, notify=True)
        except Exception:
            logger.debug("memory_guard daemon tick failed", exc_info=True)
        daemon_sleep(MEMORY_GUARD_POLL_S)


def start_memory_guard_daemon() -> None:
    """Idempotent daemon starter. Runs only in foreground parent processes."""
    global _started
    if _started:
        return
    if MEMORY_GUARD_POLL_S <= 0:
        return
    if (
        MEMORY_GUARD_WARN_MB <= 0
        and MEMORY_GUARD_KILL_MB <= 0
        and MEMORY_GUARD_AGG_WARN_MB <= 0
        and MEMORY_GUARD_AGG_KILL_MB <= 0
    ):
        return
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(
        target=_daemon_loop, name="memory_guard", daemon=True,
    )
    t.start()
    _started = True
