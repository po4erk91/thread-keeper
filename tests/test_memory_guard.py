"""Memory guard for thread-keeper server RSS thresholds."""
from __future__ import annotations

import os
import signal as _sig
import subprocess
import threading


_FAKE_CID = "55556666-7777-8888-9999-000011112222"


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _proc(pid, rss_mb, *, ppid=None):
    return {
        "pid": pid,
        "ppid": os.getpid() if ppid is None else ppid,
        "rss_kb": rss_mb * 1024,
        "rss_mb": rss_mb,
        "etime": "1:00",
        "command": "python -m threadkeeper.server",
        "parent_alive": True,
        "heartbeat_age_s": 5,
        "is_self": pid == os.getpid(),
        "is_orphaned": False,
        "orphan_reason": "parent_alive",
    }


def test_scan_over_limit_splits_warn_and_kill(mp_with_cid, monkeypatch):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 1000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 0)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 0)
    monkeypatch.setattr(process_health, "scan", lambda: [
        _proc(1001, 800),
        _proc(1002, 1200),
        _proc(1003, 2500),
    ])

    out = memory_guard.scan_over_limit()
    assert [p["pid"] for p in out["warn"]] == [1002]
    assert [p["pid"] for p in out["kill"]] == [1003]


def test_check_dry_run_does_not_kill(mp_with_cid, monkeypatch):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 1000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 0)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 0)
    monkeypatch.setattr(process_health, "scan", lambda: [_proc(1003, 2500)])
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "os.kill",
        lambda pid, sig: killed.append((pid, sig)) if sig != 0 else None,
    )

    out = memory_guard.check_once(dry_run=True, notify=False)
    assert [p["pid"] for p in out["kill"]] == [1003]
    assert out["killed"] == []
    assert killed == []


def test_check_apply_sends_sigterm(mp_with_cid, monkeypatch):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 1000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 0)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 0)
    monkeypatch.setattr(process_health, "scan", lambda: [_proc(1004, 2500)])
    monkeypatch.setattr(
        process_health, "is_threadkeeper_server_pid", lambda pid: True
    )
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr("os.kill", lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(memory_guard, "reclaim_memory", lambda reason="": {
        "before_mb": 2500, "after_mb": 2400, "freed_mb": 100,
        "pid": os.getpid(), "actions": [],
    })

    out = memory_guard.check_once(dry_run=False, notify=False)
    assert out["killed"] == [1004]
    assert calls == [(1004, _sig.SIGTERM)]


def test_check_apply_skips_reused_pid(mp_with_cid, monkeypatch):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 1000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 0)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 0)
    monkeypatch.setattr(process_health, "scan", lambda: [_proc(1006, 2500)])

    class Result:
        stdout = "python -m unrelated.worker\n"

    def fake_run(args, **kwargs):
        assert args == ["ps", "-p", "1006", "-o", "command="]
        return Result()

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(process_health.subprocess, "run", fake_run)
    monkeypatch.setattr("os.kill", lambda pid, sig: calls.append((pid, sig)))

    out = memory_guard.check_once(dry_run=False, notify=False)

    assert out["killed"] == []
    assert out["failed"] == []
    assert out["skipped"] == [
        {
            "pid": 1006,
            "action": "kill",
            "reason": "pid_no_longer_threadkeeper_server",
        }
    ]
    assert calls == []


def test_memory_guard_status_tool_reports_thresholds(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_POLL_S", 30)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 1000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 0)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 0)
    monkeypatch.setattr(process_health, "scan", lambda: [
        _proc(1001, 800),
        _proc(1002, 1200),
        _proc(1003, 2500),
    ])

    txt = _tool(pkg, "memory_guard_status")()
    assert "state=active" in txt
    assert "warn_mb=1000" in txt
    assert "kill_mb=2000" in txt
    assert "coordinator=on" in txt
    assert "ok pid=1001" in txt
    assert "WARN pid=1002" in txt
    assert "KILL pid=1003" in txt


def test_memory_guard_check_tool_defaults_to_dry_run(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 1000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 0)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 0)
    monkeypatch.setattr(process_health, "scan", lambda: [_proc(1005, 2500)])
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "os.kill",
        lambda pid, sig: killed.append((pid, sig)) if sig != 0 else None,
    )

    txt = _tool(pkg, "memory_guard_check")()
    assert txt.startswith("dry_run")
    assert "would SIGTERM pid=1005" in txt
    assert killed == []


def test_scan_reports_aggregate_pressure(mp_with_cid, monkeypatch):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 5000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 6000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 3000)
    monkeypatch.setattr(process_health, "scan", lambda: [
        _proc(1001, 800),
        _proc(1002, 900),
        _proc(1003, 1200),
    ])

    out = memory_guard.scan_over_limit()
    assert out["aggregate"]["rss_mb"] == 2900
    assert out["aggregate"]["warn"] is True
    assert out["aggregate"]["kill"] is False
    assert out["warn"] == []
    assert out["kill"] == []


def test_check_apply_requests_peer_trim_on_aggregate_warn(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 5000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 6000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 0)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_TARGET_SERVERS", 1)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_RETIRE_IDLE_S", 900)
    monkeypatch.setattr(memory_guard, "reclaim_memory", lambda reason="": {
        "before_mb": 900, "after_mb": 800, "freed_mb": 100,
        "pid": os.getpid(), "actions": ["fake"],
    })
    monkeypatch.setattr(process_health, "scan", lambda: [
        _proc(os.getpid(), 900),
        _proc(os.getpid() + 1002, 1200, ppid=os.getpid()),
    ])

    out = memory_guard.check_once(dry_run=False, notify=False)
    peer_pid = os.getpid() + 1002
    assert sorted(out["reclaim_requests"]["requested"]) == sorted(
        [os.getpid(), peer_pid]
    )

    conn = pkg["db"].get_db()
    rows = conn.execute(
        "SELECT target_pid FROM resource_controls WHERE action='trim'"
    ).fetchall()
    assert sorted(r["target_pid"] for r in rows) == sorted([os.getpid(), peer_pid])


def test_aggregate_side_effects_only_run_on_coordinator(mp_with_cid, monkeypatch):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    self_pid = os.getpid()
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 5000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 6000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 0)
    monkeypatch.setattr(process_health, "scan", lambda: [
        _proc(self_pid - 1, 1200),
        _proc(self_pid, 1200),
    ])
    reclaim_calls: list[str] = []
    monkeypatch.setattr(
        memory_guard,
        "reclaim_memory",
        lambda reason="": reclaim_calls.append(reason) or {
            "before_mb": 1200, "after_mb": 1100, "freed_mb": 100,
            "pid": self_pid, "actions": [],
        },
    )

    out = memory_guard.check_once(dry_run=False, notify=False)
    assert out["aggregate"]["warn"] is True
    assert out["coordinator"] is False
    assert out["reclaim_requests"]["count"] == 0
    assert out["local_reclaim"] is None
    assert reclaim_calls == []


def test_check_apply_retires_idle_candidate_on_aggregate_pressure(mp_with_cid, monkeypatch):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 5000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 6000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 3000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_TARGET_SERVERS", 1)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_RETIRE_IDLE_S", 900)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_RETIRE_LIVE", False)
    monkeypatch.setattr(memory_guard, "reclaim_memory", lambda reason="": {
        "before_mb": 900, "after_mb": 800, "freed_mb": 100,
        "pid": os.getpid(), "actions": ["fake"],
    })

    def scan():
        return [
            _proc(os.getpid(), 900),
            _proc(os.getpid() + 1001, 1200, ppid=1) | {
                "heartbeat_age_s": None,
                "parent_alive": False,
                "is_orphaned": True,
                "orphan_reason": "parent_gone + no_heartbeat",
            },
            _proc(os.getpid() + 1002, 800, ppid=os.getpid()) | {"heartbeat_age_s": 5},
        ]

    monkeypatch.setattr(process_health, "scan", scan)
    monkeypatch.setattr(
        process_health, "is_threadkeeper_server_pid", lambda pid: True
    )
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "os.kill",
        lambda pid, sig: calls.append((pid, sig)) if sig != 0 else None,
    )

    out = memory_guard.check_once(dry_run=False, notify=False)
    stale_pid = os.getpid() + 1001
    assert out["retired"] == [stale_pid]
    assert calls == [(stale_pid, _sig.SIGTERM)]


def test_check_apply_skips_reused_pid_on_retire(mp_with_cid, monkeypatch):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 5000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 6000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 3000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_TARGET_SERVERS", 1)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_RETIRE_IDLE_S", 900)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_RETIRE_LIVE", False)
    monkeypatch.setattr(memory_guard, "reclaim_memory", lambda reason="": {
        "before_mb": 900, "after_mb": 800, "freed_mb": 100,
        "pid": os.getpid(), "actions": ["fake"],
    })
    stale_pid = os.getpid() + 1001
    monkeypatch.setattr(process_health, "scan", lambda: [
        _proc(os.getpid(), 900),
        _proc(stale_pid, 1200, ppid=1) | {
            "heartbeat_age_s": None,
            "parent_alive": False,
            "is_orphaned": True,
            "orphan_reason": "parent_gone + no_heartbeat",
        },
    ])

    class Result:
        stdout = "python -m unrelated.worker\n"

    def fake_run(args, **kwargs):
        assert args == ["ps", "-p", str(stale_pid), "-o", "command="]
        return Result()

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(process_health.subprocess, "run", fake_run)
    monkeypatch.setattr("os.kill", lambda pid, sig: calls.append((pid, sig)))

    out = memory_guard.check_once(dry_run=False, notify=False)

    assert out["retired"] == []
    assert out["failed"] == []
    assert out["skipped"] == [
        {
            "pid": stale_pid,
            "action": "retire",
            "reason": "pid_no_longer_threadkeeper_server",
        }
    ]
    assert calls == []


def test_aggregate_retire_skips_live_parent_without_opt_in(mp_with_cid, monkeypatch):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 5000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 6000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 3000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_TARGET_SERVERS", 1)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_RETIRE_IDLE_S", 900)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_RETIRE_LIVE", False)
    monkeypatch.setattr(memory_guard, "reclaim_memory", lambda reason="": {
        "before_mb": 900, "after_mb": 800, "freed_mb": 100,
        "pid": os.getpid(), "actions": ["fake"],
    })
    monkeypatch.setattr(process_health, "scan", lambda: [
        _proc(os.getpid(), 900),
        _proc(os.getpid() + 1001, 1200, ppid=os.getpid()) | {"heartbeat_age_s": None},
    ])
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "os.kill",
        lambda pid, sig: calls.append((pid, sig)) if sig != 0 else None,
    )

    out = memory_guard.check_once(dry_run=False, notify=False)
    assert out["retired"] == []
    assert calls == []


def test_guard_tool_does_not_leak_daemon_thread(mp_with_cid, monkeypatch):
    """Invoking a guard tool with POLL_S>0 must NOT spawn a live daemon thread.

    Regression for a test-isolation bug: the status test monkeypatches
    MEMORY_GUARD_POLL_S>0 and calls a tool whose _ensure_session starts the
    real memory_guard daemon. That daemon=True thread survives the per-test
    module re-import (conftest wipes sys.modules but cannot join the thread),
    then races later tests' os.kill/process_health.scan monkeypatches and
    SIGTERMs real processes — flaking assert killed == [] elsewhere. Tests
    disable background daemons via THREADKEEPER_DISABLE_BG_DAEMONS, so no
    thread must ever be created here.
    """
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_POLL_S", 30)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 1000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 2000)
    monkeypatch.setattr(process_health, "scan", lambda: [_proc(1001, 800)])

    before = {t for t in threading.enumerate() if t.name == "memory_guard"}
    _tool(pkg, "memory_guard_status")()
    leaked = {
        t for t in threading.enumerate()
        if t.name == "memory_guard" and t.is_alive()
    } - before
    assert leaked == set(), f"memory_guard daemon thread leaked: {leaked}"


# ──────────────────────────────────────────────────────────────────────
# _last_notify_at pruning (issue #86 — bound the long-lived coordinator)
# ──────────────────────────────────────────────────────────────────────

def test_prune_notify_state_drops_stale_and_dead(monkeypatch):
    """Entries past the cooldown window or for a dead pid are pruned; a
    fresh entry for a live pid survives."""
    from threadkeeper import memory_guard as mg

    monkeypatch.setattr(mg, "MEMORY_GUARD_COOLDOWN_S", 60)
    monkeypatch.setattr(mg, "_pid_alive", lambda pid: pid == 100)
    now = 1_000_000.0
    mg._last_notify_at.clear()
    mg._last_notify_at[(100, "warn")] = now - 10   # fresh + alive → keep
    mg._last_notify_at[(100, "kill")] = now - 120  # stale → drop
    mg._last_notify_at[(200, "warn")] = now - 10   # dead pid → drop
    try:
        mg._prune_notify_state(now)
        assert set(mg._last_notify_at) == {(100, "warn")}
    finally:
        mg._last_notify_at.clear()


def test_maybe_notify_keeps_dict_bounded(monkeypatch):
    """Notifying about a churn of transient (dead) pids does not grow the
    module dict without bound — each tick prunes the prior dead entries."""
    from threadkeeper import memory_guard as mg

    monkeypatch.setattr(mg, "MEMORY_GUARD_COOLDOWN_S", 0)  # cooldown disabled
    monkeypatch.setattr(mg, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(mg, "_notify_user", lambda *a, **k: True)
    monkeypatch.setattr(mg, "_log_line", lambda *a, **k: None)
    mg._last_notify_at.clear()
    try:
        for pid in range(1000, 1200):
            mg._maybe_notify(pid, "warn", "rss over limit")
        assert len(mg._last_notify_at) <= 1
    finally:
        mg._last_notify_at.clear()


def test_pid_rss_mb_distinguishes_failed_sample_from_zero(monkeypatch):
    from threadkeeper import memory_guard as mg

    def fail_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 3))

    monkeypatch.setattr(mg.subprocess, "run", fail_run)
    assert mg._pid_rss_mb(1234) is None

    class _Result:
        def __init__(self, stdout):
            self.stdout = stdout

    monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: _Result("nope\n"))
    assert mg._pid_rss_mb(1234) is None

    monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: _Result("0\n"))
    assert mg._pid_rss_mb(1234) == 0


def test_reclaim_does_not_treat_failed_after_sample_as_freed_memory(
    mp_with_cid, monkeypatch,
):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import embeddings, memory_guard as mg

    monkeypatch.setattr(mg, "_reclaim_backoff_until", 123.0)
    monkeypatch.setattr(mg, "_reclaim_fail_streak", 2)
    monkeypatch.setattr(mg, "_log_line", lambda *a, **k: None)
    monkeypatch.setattr(mg, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr(mg, "_empty_torch_caches", lambda: [])
    monkeypatch.setattr(mg, "_allocator_pressure_relief", lambda: [])
    monkeypatch.setattr(embeddings, "unload_model", lambda: False)
    rss = iter([2000, None])
    monkeypatch.setattr(mg, "_pid_rss_mb", lambda pid: next(rss))

    out = mg.reclaim_memory(reason="aggregate_warn", force=True)

    assert out["before_mb"] == 2000
    assert out["after_mb"] is None
    assert out["freed_mb"] == 0
    assert "rss_measurement_unavailable" in out["actions"]
    assert not any(a.startswith("backoff=") for a in out["actions"])
    assert mg._reclaim_backoff_until == 123.0
    assert mg._reclaim_fail_streak == 2


# ──────────────────────────────────────────────────────────────────────
# reclaim_memory guards: hot-model skip + ineffective-reclaim back-off
# ──────────────────────────────────────────────────────────────────────

def test_reclaim_skips_hot_model(mp_with_cid, monkeypatch):
    """A model used seconds ago is not unloaded — the ingester would reload
    a fresh copy immediately, making the reclaim net-negative."""
    mp_with_cid(_FAKE_CID)
    import time as _time
    from threadkeeper import embeddings, memory_guard as mg

    monkeypatch.setattr(mg, "MEMORY_GUARD_EMBED_HOT_S", 300.0)
    monkeypatch.setattr(mg, "_reclaim_backoff_until", 0.0)
    monkeypatch.setattr(mg, "_reclaim_fail_streak", 0)
    monkeypatch.setattr(mg, "_log_line", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "model_loaded", lambda: True)
    monkeypatch.setattr(embeddings, "last_used_at", lambda: _time.time())
    unloaded: list[int] = []
    monkeypatch.setattr(
        embeddings, "unload_model", lambda: unloaded.append(1) or True
    )

    out = mg.reclaim_memory(reason="aggregate_warn")
    assert out.get("skipped") == "model_hot"
    assert out["freed_mb"] == 0
    assert unloaded == []


def test_reclaim_force_bypasses_hot_guard(mp_with_cid, monkeypatch):
    """Manual tool invocations (force=True) trim regardless of the guards."""
    mp_with_cid(_FAKE_CID)
    import time as _time
    from threadkeeper import embeddings, memory_guard as mg

    monkeypatch.setattr(mg, "MEMORY_GUARD_EMBED_HOT_S", 300.0)
    monkeypatch.setattr(mg, "_reclaim_backoff_until", _time.time() + 999)
    monkeypatch.setattr(mg, "_reclaim_fail_streak", 3)
    monkeypatch.setattr(mg, "_log_line", lambda *a, **k: None)
    monkeypatch.setattr(mg, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "model_loaded", lambda: True)
    monkeypatch.setattr(embeddings, "last_used_at", lambda: _time.time())
    unloaded: list[int] = []
    monkeypatch.setattr(
        embeddings, "unload_model", lambda: unloaded.append(1) or True
    )

    out = mg.reclaim_memory(reason="manual:self", force=True)
    assert "skipped" not in out
    assert unloaded == [1]


def test_reclaim_backs_off_after_ineffective_pass(mp_with_cid, monkeypatch):
    """A reclaim that GREW RSS (after > before — the observed pathology)
    engages an exponential back-off instead of thrash-repeating."""
    mp_with_cid(_FAKE_CID)
    from threadkeeper import embeddings, memory_guard as mg

    monkeypatch.setattr(mg, "MEMORY_GUARD_EMBED_HOT_S", 300.0)
    monkeypatch.setattr(mg, "_reclaim_backoff_until", 0.0)
    monkeypatch.setattr(mg, "_reclaim_fail_streak", 0)
    monkeypatch.setattr(mg, "_log_line", lambda *a, **k: None)
    monkeypatch.setattr(mg, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "model_loaded", lambda: False)
    rss = iter([100, 350])  # before=100 → after=350 (net-negative reclaim)
    monkeypatch.setattr(mg, "_pid_rss_mb", lambda pid: next(rss, 350))

    out = mg.reclaim_memory(reason="aggregate_warn")
    assert out["freed_mb"] == 0
    assert any(a.startswith("backoff=") for a in out["actions"])

    out2 = mg.reclaim_memory(reason="aggregate_warn")
    assert str(out2.get("skipped", "")).startswith("backoff")
