"""Daemon-thread liveness, stale-pass, and restart coverage (#114)."""
from __future__ import annotations

import threading
import time


def _loop(snapshot, loop_id: str):
    return {row["id"]: row for row in snapshot["loops"]}[loop_id]


def test_enabled_missing_thread_reports_dead(mp_with_cid):
    pkg = mp_with_cid("33334444-5555-6666-7777-888899990000")
    pkg["config"].EXTRACT_INTERVAL_S = 10

    from threadkeeper.agent_status import agent_status_snapshot

    loop = _loop(agent_status_snapshot(refresh=False), "extract")
    assert loop["enabled"] is True
    assert loop["thread_alive"] is False
    assert loop["verdict"] == "dead"


def test_disabled_daemon_is_disabled_not_dead(mp_with_cid):
    pkg = mp_with_cid("33334444-5555-6666-7777-888899990000")
    pkg["config"].EXTRACT_INTERVAL_S = 0

    from threadkeeper.agent_status import agent_status_snapshot

    loop = _loop(agent_status_snapshot(refresh=False), "extract")
    assert loop["thread_alive"] is False
    assert loop["verdict"] == "disabled"


def test_running_single_flight_past_grace_reports_stale(mp_with_cid, monkeypatch):
    pkg = mp_with_cid("33334444-5555-6666-7777-888899990000")
    pkg["config"].EXTRACT_INTERVAL_S = 10
    now = int(time.time())
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES ('s', 'extract_pass', '', 'extract_running n=1', ?)",
        (now - 1,),
    )
    conn.commit()

    import threadkeeper.agent_status as status

    monkeypatch.setattr(
        status,
        "daemon_thread_status",
        lambda name: {"thread_alive": True, "thread_started_at": now - 31},
    )
    loop = _loop(status.agent_status_snapshot(refresh=False), "extract")
    assert loop["thread_alive"] is True
    assert loop["last_success_age_s"] >= 31
    assert loop["verdict"] == "stale"


def test_supervisor_restarts_dead_thread_and_clears_stale(mp_with_cid, monkeypatch):
    pkg = mp_with_cid("33334444-5555-6666-7777-888899990000")
    cfg = pkg["config"]
    cfg.EXTRACT_INTERVAL_S = 10
    cfg.BACKGROUND_DAEMONS_ALLOWED = True
    cfg.SEMANTIC_AVAILABLE = True
    now = int(time.time())
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES ('s', 'extract_pass', '', 'completed', ?)",
        (now - 100,),
    )
    conn.commit()

    import threadkeeper.agent_status as status
    import threadkeeper.daemon_liveness as liveness
    import threadkeeper.daemon_supervisor as supervisor
    import threadkeeper.extract_daemon as extract

    extract._started = True  # reproduces the old permanent-latch failure mode
    monkeypatch.setattr(
        status,
        "daemon_thread_status",
        lambda name: {"thread_alive": True, "thread_started_at": now - 100},
    )
    assert _loop(status.agent_status_snapshot(refresh=False), "extract")["verdict"] == "stale"

    stop = threading.Event()
    monkeypatch.setattr(extract, "_serve_loop", stop.wait)
    monkeypatch.setattr(status, "daemon_thread_status", liveness.daemon_thread_status)
    result = supervisor.supervise_once(now=now)
    try:
        loop = _loop(status.agent_status_snapshot(refresh=False), "extract")
        assert result["restarted"] >= 1
        assert loop["thread_alive"] is True
        assert loop["verdict"] == "ok"
        assert extract._started is True
    finally:
        stop.set()
        thread = liveness.daemon_thread("extract_daemon")
        if thread is not None:
            thread.join(timeout=1)
        extract._started = False
