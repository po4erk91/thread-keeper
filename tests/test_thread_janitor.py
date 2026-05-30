"""Thread-janitor daemon — closes idle threads so the skill-harvest path
(close_thread → auto-review hook) actually runs.

Isolation: bespoke _bootstrap mirrors conftest._force_clean_env (all
daemons off) so the janitor only fires when a test calls run_janitor_pass
directly. AUTO_REVIEW off here — we test the CLOSE behavior; the harvest
hook is close_thread's own tested concern.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


_FAKE_CID = "11111111-2222-3333-4444-555555555555"


def _bootstrap(tmp_path, monkeypatch, interval="0", idle_days="1"):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_MEMORY_GUARD_POLL_S": "0",
        "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CURATOR_INTERVAL_S": "0",
        "THREADKEEPER_EXTRACT_INTERVAL_S": "0",
        "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_PROBE_INTERVAL_S": "0",
        "THREADKEEPER_EVOLVE_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_THREAD_JANITOR_INTERVAL_S": interval,
        "THREADKEEPER_THREAD_IDLE_CLOSE_DAYS": idle_days,
        "THREADKEEPER_AUTO_REVIEW": "",  # off — harvest hook is close_thread's concern
        "THREADKEEPER_LESSONS": str(tmp_path / "lessons.md"),
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_FORCE_CID": _FAKE_CID,
        "THREADKEEPER_NO_EMBEDDINGS": "1",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import db, thread_janitor, identity, _mcp
    return {
        "db": db,
        "thread_janitor": thread_janitor,
        "identity": identity,
        "mcp": _mcp.mcp,
    }


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _age_thread(conn, tid, days_ago):
    """Backdate a thread's last_touched_at by `days_ago` days."""
    ts = int(time.time()) - int(days_ago * 86400)
    conn.execute(
        "UPDATE threads SET last_touched_at=? WHERE id=?", (ts, tid)
    )
    conn.commit()


# ──────────────────────────────────────────────────────────────────────
# dispatch / gating
# ──────────────────────────────────────────────────────────────────────

def test_disabled_without_force(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="0")
    assert pkg["thread_janitor"].run_janitor_pass() == "disabled"


def test_no_stale_when_all_fresh(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _tool(pkg, "open_thread")(question="fresh thread")
    # default last_touched_at = now → not stale
    assert pkg["thread_janitor"].run_janitor_pass(force=True) == "no_stale"


# ──────────────────────────────────────────────────────────────────────
# closing behavior
# ──────────────────────────────────────────────────────────────────────

def test_closes_stale_active_thread(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, idle_days="1")
    conn = pkg["db"].get_db()
    tid = _tool(pkg, "open_thread")(question="stale one")
    _age_thread(conn, tid, days_ago=2)  # older than 1d threshold
    out = pkg["thread_janitor"].run_janitor_pass(force=True)
    assert out == "closed=1", out
    row = conn.execute("SELECT state, outcome FROM threads WHERE id=?",
                       (tid,)).fetchone()
    assert row["state"] == "closed"
    assert "janitor" in (row["outcome"] or "")


def test_closes_stale_idle_thread(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, idle_days="1")
    conn = pkg["db"].get_db()
    tid = _tool(pkg, "open_thread")(question="parked")
    _tool(pkg, "idle_thread")(thread_id=tid)
    _age_thread(conn, tid, days_ago=3)
    out = pkg["thread_janitor"].run_janitor_pass(force=True)
    assert out == "closed=1", out
    row = conn.execute("SELECT state FROM threads WHERE id=?", (tid,)).fetchone()
    assert row["state"] == "closed"


def test_leaves_fresh_thread_open(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, idle_days="1")
    conn = pkg["db"].get_db()
    stale = _tool(pkg, "open_thread")(question="stale")
    fresh = _tool(pkg, "open_thread")(question="fresh")
    _age_thread(conn, stale, days_ago=2)
    # fresh keeps default now-ish last_touched_at
    out = pkg["thread_janitor"].run_janitor_pass(force=True)
    assert out == "closed=1", out
    s = conn.execute("SELECT state FROM threads WHERE id=?", (stale,)).fetchone()
    f = conn.execute("SELECT state FROM threads WHERE id=?", (fresh,)).fetchone()
    assert s["state"] == "closed"
    assert f["state"] == "active"


def test_idempotent_second_pass(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, idle_days="1")
    conn = pkg["db"].get_db()
    tid = _tool(pkg, "open_thread")(question="stale")
    _age_thread(conn, tid, days_ago=2)
    assert pkg["thread_janitor"].run_janitor_pass(force=True) == "closed=1"
    # already closed → not re-matched
    assert pkg["thread_janitor"].run_janitor_pass(force=True) == "no_stale"


def test_closed_then_note_reopens_survives_janitor(tmp_path, monkeypatch):
    """The whole safety story end-to-end: janitor closes a stale thread,
    a note reopens it (fresh last_touched_at), and the next janitor pass
    leaves it alone because it's no longer stale."""
    pkg = _bootstrap(tmp_path, monkeypatch, idle_days="1")
    conn = pkg["db"].get_db()
    tid = _tool(pkg, "open_thread")(question="comes back")
    _age_thread(conn, tid, days_ago=2)
    pkg["thread_janitor"].run_janitor_pass(force=True)
    assert conn.execute("SELECT state FROM threads WHERE id=?",
                        (tid,)).fetchone()["state"] == "closed"
    # user returns → agent notes on it → reopen
    _tool(pkg, "note")(thread_id=tid, content="picking this back up", kind="move")
    assert conn.execute("SELECT state FROM threads WHERE id=?",
                        (tid,)).fetchone()["state"] == "active"
    # fresh now, so a second janitor pass must not re-close it
    assert pkg["thread_janitor"].run_janitor_pass(force=True) == "no_stale"
    assert conn.execute("SELECT state FROM threads WHERE id=?",
                        (tid,)).fetchone()["state"] == "active"


def test_records_janitor_pass_event(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, idle_days="1")
    conn = pkg["db"].get_db()
    tid = _tool(pkg, "open_thread")(question="stale")
    _age_thread(conn, tid, days_ago=2)
    pkg["thread_janitor"].run_janitor_pass(force=True)
    row = conn.execute(
        "SELECT summary FROM events WHERE kind='janitor_pass' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert "closed=1" in row["summary"]


def test_daemon_does_not_start_at_interval_zero(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="0")
    pkg["thread_janitor"].start_thread_janitor()
    assert pkg["thread_janitor"]._started is False
