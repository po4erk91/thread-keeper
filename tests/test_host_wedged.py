"""Wedged-but-alive daemon-host recovery (issue #223).

With THREADKEEPER_DAEMON_HOST on, a host whose heartbeat has gone stale but
whose process is still up keeps holding the election flock, so a plain
respawn attempt exits 0 and the machine silently loses its loops. The
supervisor must verify the recorded pid really is a threadkeeper host, then
SIGTERM (escalating to SIGKILL) before spawning a replacement.
"""
from __future__ import annotations

import json
import subprocess
import sys, importlib, threading, time


def _reimport(monkeypatch, tmp_path):
    for k, v in {"THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
                 "THREADKEEPER_DAEMON_HOST": "1",
                 "THREADKEEPER_DISABLE_BG_DAEMONS": "1"}.items():
        monkeypatch.setenv(k, v)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    importlib.import_module("threadkeeper.server")
    return importlib.import_module("threadkeeper.host")


def _allow_spawn_path(monkeypatch, host):
    """Clear the suite-hygiene daemon pause so ensure_host_running() is
    reachable, and stub the replacement spawn."""
    from threadkeeper import config
    monkeypatch.setattr(config, "DISABLE_BG_DAEMONS", False)
    spawns = []
    monkeypatch.setattr(host.subprocess, "Popen",
                        lambda *a, **k: spawns.append((a, k)) or object())
    return spawns


def _seed_heartbeat(age_s: int) -> None:
    from threadkeeper.db import get_db
    conn = get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO presence (session_id, client, started_at, heartbeat_at, "
        "last_action) VALUES (?,?,?,?,?)",
        ("s-host-test", "daemon-host", now - age_s - 5, now - age_s, "hb"),
    )
    conn.commit()


def _dummy(ignore_sigterm: bool = False) -> subprocess.Popen:
    """A live non-child-of-init stand-in for the wedged host. A reaper thread
    waits on it so a delivered signal turns into ProcessLookupError for
    os.kill(pid, 0) instead of a zombie (detached real hosts never zombie)."""
    if ignore_sigterm:
        code = ("import signal, sys, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(120)")
    else:
        code = "print('ready', flush=True); import time; time.sleep(120)"
    proc = subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def _write_pidfile(host, pid: int, started_age_s: int) -> None:
    host._host_pid_path().write_text(json.dumps(
        {"pid": pid, "started_at": int(time.time()) - started_age_s}))


def test_pidfile_roundtrip_and_scoped_clear(monkeypatch, tmp_path):
    host = _reimport(monkeypatch, tmp_path)
    import os
    host._write_host_pidfile()
    info = host._read_host_pidfile()
    assert info and info["pid"] == os.getpid() and info["started_at"] > 0
    host._clear_host_pidfile(only_pid=info["pid"] + 1)   # someone else's → keep
    assert host._read_host_pidfile() is not None
    host._clear_host_pidfile(only_pid=info["pid"])       # ours → remove
    assert host._read_host_pidfile() is None


def test_heartbeat_age_and_alive(monkeypatch, tmp_path):
    host = _reimport(monkeypatch, tmp_path)
    assert host._host_heartbeat_age() is None
    assert host._host_alive() is False
    _seed_heartbeat(age_s=300)
    age = host._host_heartbeat_age()
    assert age is not None and 295 <= age <= 310
    assert host._host_alive() is False          # 300s > 120s TTL
    from threadkeeper import config
    monkeypatch.setattr(config, "HOST_HEARTBEAT_TTL_S", 1000.0)
    assert host._host_alive() is True


def test_wedged_host_sigtermed_then_replacement_spawned(monkeypatch, tmp_path):
    host = _reimport(monkeypatch, tmp_path)
    _seed_heartbeat(age_s=100_000)
    proc = _dummy()
    spawns = _allow_spawn_path(monkeypatch, host)
    try:
        _write_pidfile(host, proc.pid, started_age_s=100_000)
        monkeypatch.setattr(host, "_pid_is_host", lambda pid: pid == proc.pid)
        assert host.ensure_host_running() is True
        assert proc.poll() is not None                   # wedged holder killed
        assert len(spawns) == 1                          # replacement spawned
        assert host._read_host_pidfile() is None         # stale pidfile gone
        from threadkeeper.db import get_db
        kinds = [r["kind"] for r in get_db().execute(
            "SELECT kind FROM events WHERE kind LIKE 'host_wedged%'")]
        assert "host_wedged_sigterm" in kinds
    finally:
        if proc.poll() is None:
            proc.kill()


def test_sigterm_ignoring_host_gets_sigkilled(monkeypatch, tmp_path):
    host = _reimport(monkeypatch, tmp_path)
    monkeypatch.setattr(host, "_WEDGE_TERM_GRACE_S", 0.5)
    _seed_heartbeat(age_s=100_000)
    proc = _dummy(ignore_sigterm=True)
    spawns = _allow_spawn_path(monkeypatch, host)
    try:
        _write_pidfile(host, proc.pid, started_age_s=100_000)
        monkeypatch.setattr(host, "_pid_is_host", lambda pid: pid == proc.pid)
        assert host.ensure_host_running() is True
        assert proc.poll() is not None
        assert len(spawns) == 1
        from threadkeeper.db import get_db
        kinds = [r["kind"] for r in get_db().execute(
            "SELECT kind FROM events WHERE kind LIKE 'host_wedged%'")]
        assert "host_wedged_sigkill" in kinds
    finally:
        if proc.poll() is None:
            proc.kill()


def test_unverified_pid_is_never_signaled(monkeypatch, tmp_path):
    host = _reimport(monkeypatch, tmp_path)
    _seed_heartbeat(age_s=100_000)
    proc = _dummy()
    spawns = _allow_spawn_path(monkeypatch, host)
    try:
        _write_pidfile(host, proc.pid, started_age_s=100_000)
        monkeypatch.setattr(host, "_pid_is_host", lambda pid: False)
        host.ensure_host_running()
        assert proc.poll() is None            # untouched: pid could be recycled
        assert len(spawns) == 1               # legacy no-op spawn attempt kept
    finally:
        proc.kill()


def test_booting_holder_is_given_time(monkeypatch, tmp_path):
    host = _reimport(monkeypatch, tmp_path)
    # no heartbeat row at all: the fresh holder simply hasn't written one yet
    proc = _dummy()
    spawns = _allow_spawn_path(monkeypatch, host)
    try:
        _write_pidfile(host, proc.pid, started_age_s=3)
        monkeypatch.setattr(host, "_pid_is_host", lambda pid: True)
        host.ensure_host_running()
        assert proc.poll() is None                       # not killed
        assert host._read_host_pidfile() is not None     # pidfile kept
    finally:
        proc.kill()


def test_stale_but_not_wedge_old_heartbeat_not_killed(monkeypatch, tmp_path):
    host = _reimport(monkeypatch, tmp_path)
    _seed_heartbeat(age_s=200)      # > TTL (120) but < wedge threshold (600)
    proc = _dummy()
    spawns = _allow_spawn_path(monkeypatch, host)
    try:
        _write_pidfile(host, proc.pid, started_age_s=100_000)
        monkeypatch.setattr(host, "_pid_is_host", lambda pid: True)
        host.ensure_host_running()
        assert proc.poll() is None
        assert len(spawns) == 1
    finally:
        proc.kill()


def test_crashed_host_stale_pidfile_cleared_and_respawned(monkeypatch, tmp_path):
    host = _reimport(monkeypatch, tmp_path)
    _seed_heartbeat(age_s=100_000)
    proc = _dummy()
    spawns = _allow_spawn_path(monkeypatch, host)
    proc.kill()
    for _ in range(100):
        if proc.poll() is not None:
            break
        time.sleep(0.02)
    _write_pidfile(host, proc.pid, started_age_s=100_000)
    assert host.ensure_host_running() is True
    assert host._read_host_pidfile() is None
    assert len(spawns) == 1


def test_main_writes_then_clears_pidfile(monkeypatch, tmp_path):
    import signal as _signal
    host = _reimport(monkeypatch, tmp_path)
    monkeypatch.setattr(host, "start_daemons", lambda: [])
    monkeypatch.setattr(host, "_heartbeat", lambda: None)
    seen = {}
    monkeypatch.setattr(host, "start_embed_server",
                        lambda: seen.update(pidfile=host._read_host_pidfile()))
    real_event = threading.Event

    def preset_event():
        ev = real_event()
        ev.set()          # stop the main loop before its first iteration
        return ev

    monkeypatch.setattr(host.threading, "Event", preset_event)
    prev = _signal.getsignal(_signal.SIGTERM)
    try:
        assert host.main() == 0
    finally:
        _signal.signal(_signal.SIGTERM, prev)
    import os
    assert seen["pidfile"] and seen["pidfile"]["pid"] == os.getpid()
    assert host._read_host_pidfile() is None
