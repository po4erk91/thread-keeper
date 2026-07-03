"""Wall-clock watchdog for spawned children (#80).

A child that hangs while still alive stalls its loop's single-flight slot and
burns tokens forever. The budget daemon's sweep (`_refresh_all_running`) reaps
any pid>0 row that has outlived `SPAWN_MAX_RUNTIME_S`: SIGTERM → grace →
SIGKILL, then `ended_at` + the timeout sentinel `return_code` so the loop's
single-flight releases.

The real-kill test launches a genuinely detached sleeper (double-fork +
setsid, re-parented to init) so the watchdog's `os.waitpid`-based `alive()`
check can't reap it out from under the test, then asserts the sweep actually
kills it. The remaining cases mock the kill (using this process's own live
pid) to exercise the single-flight release / idempotency / disable semantics
without process-management flakiness.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest


_FAKE_CID = "33334444-5555-6666-7777-888899990000"


@pytest.fixture(autouse=True)
def _disable_timeout_retry_by_default(monkeypatch):
    # Most watchdog tests assert the kill/row-closing path only. Keep them from
    # launching a real child after timeout; retry-specific tests opt back in.
    monkeypatch.setenv("THREADKEEPER_SPAWN_TIMEOUT_RETRY_LIMIT", "0")


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _txt(res):
    if isinstance(res, str):
        return res
    return "\n".join(
        c.text for c in res.content if getattr(c, "type", None) == "text"
    )


def _insert_running(pkg, task_id, pid, started_at, prompt="test"):
    """Insert a running (ended_at NULL) task with an explicit pid + age."""
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at, ended_at, rss_kb, rss_updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (task_id, pid, _FAKE_CID, f"child-{task_id}", "/tmp", prompt,
         started_at, None, None, started_at),
    )
    conn.commit()
    return conn


def _spawn_detached_sleeper() -> int:
    """Start a long-lived sleeper that is NOT this process's waitable child.

    A launcher forks a setsid'd sleeper, prints its pid, then exits — so the
    sleeper re-parents to init/launchd and `alive()`'s opportunistic
    `os.waitpid` never reaps it. Returns the sleeper's pid."""
    code = (
        "import os,sys,time\n"
        "r=os.fork()\n"
        "if r>0:\n"
        "    sys.stdout.write(str(r)); sys.stdout.flush(); os._exit(0)\n"
        "os.setsid()\n"
        "os.close(0); os.close(1); os.close(2)\n"
        "time.sleep(300)\n"
    )
    out = subprocess.check_output([sys.executable, "-c", code], text=True)
    return int(out.strip())


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return True
        time.sleep(0.2)
    return False


# ─────────────────────────────────────────────────────────────────────
# Real kill path — seed an old running row with a live pid, run the sweep.
# ─────────────────────────────────────────────────────────────────────

def test_watchdog_kills_overcap_child(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_SPAWN_BUDGET_MB", "3072")
    monkeypatch.setenv("THREADKEEPER_SPAWN_MAX_RUNTIME_S", "60")
    monkeypatch.setenv("THREADKEEPER_SPAWN_KILL_GRACE_S", "5")
    pkg = mp_with_cid(_FAKE_CID)

    import threadkeeper.spawn_budget as sb

    pid = _spawn_detached_sleeper()
    try:
        old = int(time.time()) - 200  # older than the 60s cap
        conn = _insert_running(pkg, "tk_hung", pid, old)

        sb._refresh_all_running(conn)

        row = conn.execute(
            "SELECT ended_at, return_code FROM tasks WHERE id='tk_hung'"
        ).fetchone()
        assert row["ended_at"] is not None
        assert row["return_code"] == sb.SPAWN_TIMEOUT_RETURN_CODE
        # Single-flight releases: an ended row no longer counts as running.
        assert sb._running_tasks_rss(conn) == 0
        assert _wait_dead(pid), "watchdog did not actually kill the child"
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


def test_watchdog_releases_applier_single_flight(mp_with_cid, monkeypatch):
    """A timed-out applier child stops being reported by the applier
    single-flight check, so the loop can spawn again."""
    monkeypatch.setenv("THREADKEEPER_SPAWN_BUDGET_MB", "3072")
    monkeypatch.setenv("THREADKEEPER_SPAWN_MAX_RUNTIME_S", "60")
    pkg = mp_with_cid(_FAKE_CID)

    import threadkeeper.spawn_budget as sb
    from threadkeeper.evolve_applier import (
        _running_applier_children,
        EVOLVE_APPLY_PROMPT_PREFIX,
    )

    # Don't actually signal — pid is THIS test process (guaranteed alive).
    killed: list[int] = []
    monkeypatch.setattr(sb, "_terminate_tree",
                        lambda pid, grace: killed.append(pid))

    old = int(time.time()) - 200
    conn = _insert_running(
        pkg, "tk_appl", os.getpid(), old,
        prompt=EVOLVE_APPLY_PROMPT_PREFIX + " ISSUE #999 ...",
    )

    # Before the sweep the hung child pins the applier single-flight slot.
    assert "tk_appl" in _running_applier_children(conn)

    sb._refresh_all_running(conn)

    # After: killed, closed, and the slot is free.
    assert killed == [os.getpid()]
    row = conn.execute(
        "SELECT ended_at, return_code FROM tasks WHERE id='tk_appl'"
    ).fetchone()
    assert row["ended_at"] is not None
    assert row["return_code"] == sb.SPAWN_TIMEOUT_RETURN_CODE
    assert "tk_appl" not in _running_applier_children(conn)


def test_watchdog_immediately_respawns_with_continuation_prompt(
    mp_with_cid, monkeypatch
):
    monkeypatch.setenv("THREADKEEPER_SPAWN_BUDGET_MB", "3072")
    monkeypatch.setenv("THREADKEEPER_SPAWN_MAX_RUNTIME_S", "60")
    monkeypatch.setenv("THREADKEEPER_SPAWN_TIMEOUT_RETRY_LIMIT", "2")
    pkg = mp_with_cid(_FAKE_CID)

    import threadkeeper.spawn_budget as sb
    import threadkeeper.tools.spawn as spawn_mod

    killed: list[int] = []
    calls: list[dict] = []
    monkeypatch.setattr(sb, "_terminate_tree",
                        lambda pid, grace: killed.append(pid))

    def fake_spawn(**kwargs):
        calls.append(kwargs)
        return (
            "ok task=tk_retry pid=123 child_cid=abcd1234 "
            "parent_cid=33334444 perm=auto mode=headless log=/tmp/x"
        )

    monkeypatch.setattr(spawn_mod, "_spawn_impl", fake_spawn)

    old = int(time.time()) - 200
    conn = _insert_running(
        pkg, "tk_hung_retry", os.getpid(), old,
        prompt="Original assignment: repair the branch",
    )
    pkg["identity"]._ensure_session(conn)
    conn.execute(
        "UPDATE tasks SET role=?, write_origin=?, permission_mode=?, "
        "extra_allowed_tools=?, capture_output=?, visible=?, slim=?, "
        "model=?, effort=?, append_system=?, chosen_cli=?, retry_attempt=? "
        "WHERE id=?",
        (
            "evolve_applier", "evolve_apply", "bypassPermissions",
            "Bash(git *)", 1, 0, 1, "sonnet", "high", "extra sys", "codex", 0,
            "tk_hung_retry",
        ),
    )
    conn.commit()

    sb._refresh_all_running(conn)

    assert killed == [os.getpid()]
    assert len(calls) == 1
    call = calls[0]
    assert call["retry_of"] == "tk_hung_retry"
    assert call["retry_root"] == "tk_hung_retry"
    assert call["retry_attempt"] == 1
    assert call["parent_cid_override"] == _FAKE_CID
    assert call["role"] == "evolve_applier"
    assert call["write_origin"] == "evolve_apply"
    assert call["permission_mode"] == "bypassPermissions"
    assert call["extra_allowed_tools"] == "Bash(git *)"
    assert call["capture_output"] is True
    assert call["visible"] is False
    assert call["slim"] is True
    assert call["model"] == "sonnet"
    assert call["effort"] == "high"
    assert call["append_system"] == "extra sys"
    assert call["cli"] == "codex"
    assert "Continue the same assignment" in call["prompt"]
    assert "Original assignment: repair the branch" in call["prompt"]

    row = conn.execute(
        "SELECT ended_at, return_code, timeout_respawned_as "
        "FROM tasks WHERE id='tk_hung_retry'"
    ).fetchone()
    assert row["ended_at"] is not None
    assert row["return_code"] == sb.SPAWN_TIMEOUT_RETURN_CODE
    assert row["timeout_respawned_as"] == "tk_retry"
    ev = conn.execute(
        "SELECT summary FROM events WHERE kind='spawn_timeout_retry' "
        "AND target='tk_hung_retry'"
    ).fetchone()
    assert ev is not None
    assert "respawned_as=tk_retry" in ev["summary"]


def test_watchdog_retry_limit_stops_infinite_restart(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_SPAWN_MAX_RUNTIME_S", "60")
    monkeypatch.setenv("THREADKEEPER_SPAWN_TIMEOUT_RETRY_LIMIT", "1")
    pkg = mp_with_cid(_FAKE_CID)

    import threadkeeper.spawn_budget as sb
    import threadkeeper.tools.spawn as spawn_mod

    killed: list[int] = []
    calls: list[dict] = []
    monkeypatch.setattr(sb, "_terminate_tree",
                        lambda pid, grace: killed.append(pid))
    monkeypatch.setattr(spawn_mod, "_spawn_impl",
                        lambda **kwargs: calls.append(kwargs) or "ok task=tk_x")

    old = int(time.time()) - 200
    conn = _insert_running(pkg, "tk_retry_done", os.getpid(), old)
    pkg["identity"]._ensure_session(conn)
    conn.execute(
        "UPDATE tasks SET retry_root=?, retry_attempt=? WHERE id=?",
        ("tk_root", 1, "tk_retry_done"),
    )
    conn.commit()

    sb._refresh_all_running(conn)

    assert killed == [os.getpid()]
    assert calls == []
    ev = conn.execute(
        "SELECT summary FROM events WHERE kind='spawn_timeout_retry_skipped' "
        "AND target='tk_retry_done'"
    ).fetchone()
    assert ev is not None
    assert "attempts_exhausted" in ev["summary"]


def test_watchdog_is_idempotent(mp_with_cid, monkeypatch):
    """A second sweep over an already-reaped row neither re-kills nor
    rewrites the row."""
    monkeypatch.setenv("THREADKEEPER_SPAWN_MAX_RUNTIME_S", "60")
    pkg = mp_with_cid(_FAKE_CID)

    import threadkeeper.spawn_budget as sb
    killed: list[int] = []
    monkeypatch.setattr(sb, "_terminate_tree",
                        lambda pid, grace: killed.append(pid))

    old = int(time.time()) - 200
    conn = _insert_running(pkg, "tk_once", os.getpid(), old)

    sb._refresh_all_running(conn)
    first = conn.execute(
        "SELECT ended_at, return_code FROM tasks WHERE id='tk_once'"
    ).fetchone()
    assert first["ended_at"] is not None

    sb._refresh_all_running(conn)  # already ended → not re-selected
    second = conn.execute(
        "SELECT ended_at, return_code FROM tasks WHERE id='tk_once'"
    ).fetchone()
    assert second["ended_at"] == first["ended_at"]
    assert len(killed) == 1  # killed exactly once


def test_watchdog_disabled_when_zero(mp_with_cid, monkeypatch):
    """Cap=0 disables the watchdog — an old running child is left untouched
    (no surprise kills on upgrade)."""
    monkeypatch.setenv("THREADKEEPER_SPAWN_BUDGET_MB", "3072")
    monkeypatch.setenv("THREADKEEPER_SPAWN_MAX_RUNTIME_S", "0")
    pkg = mp_with_cid(_FAKE_CID)

    import threadkeeper.spawn_budget as sb
    killed: list[int] = []
    monkeypatch.setattr(sb, "_terminate_tree",
                        lambda pid, grace: killed.append(pid))

    old = int(time.time()) - 100_000  # ancient
    conn = _insert_running(pkg, "tk_keep", os.getpid(), old)

    sb._refresh_all_running(conn)

    row = conn.execute(
        "SELECT ended_at, return_code FROM tasks WHERE id='tk_keep'"
    ).fetchone()
    assert row["ended_at"] is None
    assert killed == []


def test_young_child_not_reaped(mp_with_cid, monkeypatch):
    """A child within the cap is never killed."""
    monkeypatch.setenv("THREADKEEPER_SPAWN_MAX_RUNTIME_S", "3600")
    pkg = mp_with_cid(_FAKE_CID)

    import threadkeeper.spawn_budget as sb
    killed: list[int] = []
    monkeypatch.setattr(sb, "_terminate_tree",
                        lambda pid, grace: killed.append(pid))

    young = int(time.time()) - 30
    conn = _insert_running(pkg, "tk_young", os.getpid(), young)

    sb._refresh_all_running(conn)

    row = conn.execute(
        "SELECT ended_at FROM tasks WHERE id='tk_young'"
    ).fetchone()
    assert row["ended_at"] is None
    assert killed == []


# ─────────────────────────────────────────────────────────────────────
# Observability — timed-out children are surfaced, not silent.
# ─────────────────────────────────────────────────────────────────────

def test_dashboard_reports_timed_out_tasks(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    import threadkeeper.spawn_budget as sb
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at, ended_at, return_code) VALUES (?,?,?,?,?,?,?,?,?)",
        ("tk_to", 0, _FAKE_CID, "c", "/tmp", "test",
         now - 5000, now - 100, sb.SPAWN_TIMEOUT_RETURN_CODE),
    )
    conn.commit()

    txt = _txt(_tool(pkg, "mp_dashboard")())
    assert "tasks_timed_out=1" in txt


def test_agent_status_reports_timed_out(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    import threadkeeper.spawn_budget as sb
    from threadkeeper.agent_status import (
        agent_status_snapshot,
        format_agent_status,
    )
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at, ended_at, return_code) VALUES (?,?,?,?,?,?,?,?,?)",
        ("tk_to2", 0, _FAKE_CID, "c", "/tmp", "test",
         now - 5000, now - 100, sb.SPAWN_TIMEOUT_RETURN_CODE),
    )
    conn.commit()

    snap = agent_status_snapshot(refresh=False)
    assert snap["timed_out_count"] == 1
    assert "timed_out=1" in format_agent_status(snap)
