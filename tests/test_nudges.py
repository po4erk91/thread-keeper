"""Counter-driven nudge logic (threadkeeper/nudges.py).

Tests three public functions:
- compute_memory_nudge(conn, session_id) — fires when N events since last
  memory save AND a rich thread exists.
- compute_skill_nudge(conn, session_id) — fires when N events since last
  skill materialize AND a rich pending closed thread exists.
- auto_review_should_fire(conn, session_id, force=False) — returns
  thread_id of richest pending closed thread when AUTO_REVIEW_ENABLED +
  threshold crossed (force=True bypasses both gates).

Plus the auto_review_trigger MCP tool surface.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest


_FAKE_CID = "deadbeef-0000-0000-0000-aaaaaaaaaaaa"


def _bootstrap_with_env(tmp_path, monkeypatch,
                        *,
                        memory_interval: int = 3,
                        skill_interval: int = 3,
                        auto_review: bool = False,
                        force_cid: str = _FAKE_CID) -> dict:
    """Local bootstrap that mirrors conftest._bootstrap_mp but adds the
    nudge-tuning env vars. Each call wipes sys.modules of threadkeeper
    so the fresh config values take effect."""
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_MEMORY_NUDGE_INTERVAL": str(memory_interval),
        "THREADKEEPER_SKILL_NUDGE_INTERVAL": str(skill_interval),
        "THREADKEEPER_AUTO_REVIEW": "1" if auto_review else "0",
    }
    if force_cid:
        env["THREADKEEPER_FORCE_CID"] = force_cid
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)

    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]

    import threadkeeper.server  # noqa: F401
    from threadkeeper import _mcp, identity, db, brief, config, nudges

    return {
        "mcp": _mcp.mcp,
        "identity": identity,
        "db": db,
        "brief": brief,
        "config": config,
        "nudges": nudges,
        "tmp": tmp_path,
    }


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _open_session(pkg):
    """Force session row + emit a no-op event so _session_id is populated."""
    # brief() triggers _ensure_session.
    _tool(pkg, "brief")()
    return pkg["identity"]._session_id


def _open_thread_with_notes(pkg, n_total: int, n_rich: int,
                            close: bool = False) -> str:
    """Open a thread, append n_total notes (n_rich of them insight/move,
    rest open_q), optionally close. Return thread id."""
    open_t = _tool(pkg, "open_thread")
    note = _tool(pkg, "note")
    close_t = _tool(pkg, "close_thread")
    tid = open_t(question=f"rich thread n={n_total} r={n_rich}")
    rich_kinds = ["insight", "move"]
    for i in range(n_total):
        kind = rich_kinds[i % 2] if i < n_rich else "open_q"
        note(thread_id=tid, content=f"note #{i}", kind=kind)
    if close:
        close_t(thread_id=tid, outcome="finished")
    return tid


def _inject_neutral_events(pkg, count: int) -> None:
    """Insert `count` rows of a non-reset event kind for this session.
    Used to drive the nudge counter without triggering reset events.
    'idle_thread' is in neither memory nor skill reset sets."""
    conn = pkg["db"].get_db()
    sid = pkg["identity"]._session_id or ""
    now = int(time.time())
    for i in range(count):
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?,?,?,?,?)",
            (sid, "idle_thread", None, f"neutral #{i}", now + i),
        )
    conn.commit()


# ──────────────────────────────────────────────────────────────────────────
# compute_memory_nudge
# ──────────────────────────────────────────────────────────────────────────

def test_memory_nudge_silent_below_threshold(tmp_path, monkeypatch):
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=5, skill_interval=5)
    sid = _open_session(pkg)
    _open_thread_with_notes(pkg, n_total=3, n_rich=1)
    # Reset event 'open_thread' just fired; subsequent note:open_q × 2
    # advance the counter to 2 — below threshold 5.
    conn = pkg["db"].get_db()
    out = pkg["nudges"].compute_memory_nudge(conn, sid)
    assert out is None


def test_memory_nudge_soft_at_threshold(tmp_path, monkeypatch):
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=3, skill_interval=99)
    sid = _open_session(pkg)
    _open_thread_with_notes(pkg, n_total=3, n_rich=0)
    # open_thread is reset; three note:open_q events follow → counter=3.
    conn = pkg["db"].get_db()
    out = pkg["nudges"].compute_memory_nudge(conn, sid)
    assert out is not None
    assert "memory_nudge" in out
    assert "CONSOLIDATE" in out
    assert "threshold=3" in out
    # Soft does NOT include the warning emoji.
    assert "⚠️" not in out


def test_memory_nudge_demanding_at_2x_threshold(tmp_path, monkeypatch):
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=2, skill_interval=99)
    sid = _open_session(pkg)
    _open_thread_with_notes(pkg, n_total=4, n_rich=0)
    # open_thread (reset) + 4 note:open_q → counter=4 = 2× threshold.
    conn = pkg["db"].get_db()
    out = pkg["nudges"].compute_memory_nudge(conn, sid)
    assert out is not None
    assert "⚠️" in out
    assert "overdue=2x" in out
    assert "MUST consolidate" in out


def test_memory_nudge_silent_without_rich_thread(tmp_path, monkeypatch):
    """Counter is past threshold but no thread has ≥3 notes → no nudge."""
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=2, skill_interval=99)
    sid = _open_session(pkg)
    # Push counter via neutral events; no thread opened.
    _inject_neutral_events(pkg, count=5)
    conn = pkg["db"].get_db()
    out = pkg["nudges"].compute_memory_nudge(conn, sid)
    assert out is None


def test_memory_nudge_disabled_when_interval_zero(tmp_path, monkeypatch):
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=0, skill_interval=99)
    sid = _open_session(pkg)
    _open_thread_with_notes(pkg, n_total=5, n_rich=0)
    conn = pkg["db"].get_db()
    out = pkg["nudges"].compute_memory_nudge(conn, sid)
    assert out is None


# ──────────────────────────────────────────────────────────────────────────
# compute_skill_nudge
# ──────────────────────────────────────────────────────────────────────────

def test_skill_nudge_silent_below_threshold(tmp_path, monkeypatch):
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=99, skill_interval=20)
    sid = _open_session(pkg)
    _open_thread_with_notes(pkg, n_total=6, n_rich=3, close=True)
    # Closed rich thread but events emitted (open + 6 notes + close = 8)
    # are below threshold 20.
    conn = pkg["db"].get_db()
    out = pkg["nudges"].compute_skill_nudge(conn, sid)
    assert out is None


def test_skill_nudge_soft_at_threshold(tmp_path, monkeypatch):
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=99, skill_interval=5)
    sid = _open_session(pkg)
    _open_thread_with_notes(pkg, n_total=6, n_rich=3, close=True)
    # 8 events total since session start, none are skill reset kinds.
    conn = pkg["db"].get_db()
    out = pkg["nudges"].compute_skill_nudge(conn, sid)
    assert out is not None
    assert "skill_nudge" in out
    assert "threshold=5" in out
    assert "review_thread" in out
    assert "⚠️" not in out


def test_skill_nudge_demanding_at_2x_threshold(tmp_path, monkeypatch):
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=99, skill_interval=3)
    sid = _open_session(pkg)
    _open_thread_with_notes(pkg, n_total=6, n_rich=3, close=True)
    # 8 events total ≥ 6 (2× threshold=3).
    conn = pkg["db"].get_db()
    out = pkg["nudges"].compute_skill_nudge(conn, sid)
    assert out is not None
    assert "⚠️" in out
    assert "overdue=2x" in out
    assert "MUST act next" in out


def test_skill_nudge_silent_without_rich_closed_thread(tmp_path, monkeypatch):
    """Counter past threshold but no rich closed thread → no nudge."""
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=99, skill_interval=3)
    sid = _open_session(pkg)
    # Thin: only 4 notes, none insight/move.
    _open_thread_with_notes(pkg, n_total=4, n_rich=0, close=True)
    conn = pkg["db"].get_db()
    out = pkg["nudges"].compute_skill_nudge(conn, sid)
    assert out is None


def test_skill_nudge_silent_when_thread_already_materialized(tmp_path, monkeypatch):
    """A 'skill_materialized' event suppresses the nudge for that thread.
    With only that one thread, the nudge cannot fire."""
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=99, skill_interval=3)
    sid = _open_session(pkg)
    tid = _open_thread_with_notes(pkg, n_total=6, n_rich=3, close=True)
    # Materialize via the existing tool — but skill_materialized event
    # is NOT in skill reset kinds, so counter stays high. The
    # _find_rich_pending_thread filter is what suppresses.
    mark = _tool(pkg, "mark_skill_materialized")
    mark(thread_id=tid, skill_path="/tmp/foo/SKILL.md")
    conn = pkg["db"].get_db()
    out = pkg["nudges"].compute_skill_nudge(conn, sid)
    assert out is None


def test_skill_nudge_silent_after_skill_create_event(tmp_path, monkeypatch):
    """A real skill_create event resets the counter even without
    mark_skill_materialized on the specific thread."""
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=99, skill_interval=3)
    sid = _open_session(pkg)
    _open_thread_with_notes(pkg, n_total=6, n_rich=3, close=True)
    # Inject a skill_create event AFTER the close, simulating that the
    # agent went and wrote a skill. Counter resets.
    conn = pkg["db"].get_db()
    now = int(time.time()) + 100
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?,?,?,?,?)",
        (sid, "skill_create", "my-new-skill", "wrote a skill", now),
    )
    conn.commit()
    out = pkg["nudges"].compute_skill_nudge(conn, sid)
    # Counter has reset (n_since=0 < threshold) → silent.
    assert out is None


# ──────────────────────────────────────────────────────────────────────────
# auto_review_should_fire
# ──────────────────────────────────────────────────────────────────────────

def test_auto_review_silent_without_env_flag(tmp_path, monkeypatch):
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=99, skill_interval=3,
                              auto_review=False)
    sid = _open_session(pkg)
    _open_thread_with_notes(pkg, n_total=6, n_rich=3, close=True)
    conn = pkg["db"].get_db()
    out = pkg["nudges"].auto_review_should_fire(conn, sid)
    assert out is None


def test_auto_review_fires_when_all_conditions_met(tmp_path, monkeypatch):
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=99, skill_interval=3,
                              auto_review=True)
    sid = _open_session(pkg)
    tid = _open_thread_with_notes(pkg, n_total=6, n_rich=3, close=True)
    conn = pkg["db"].get_db()
    out = pkg["nudges"].auto_review_should_fire(conn, sid)
    assert out == tid


def test_auto_review_silent_below_threshold(tmp_path, monkeypatch):
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=99, skill_interval=20,
                              auto_review=True)
    sid = _open_session(pkg)
    _open_thread_with_notes(pkg, n_total=6, n_rich=3, close=True)
    # Only 8 events < 20.
    conn = pkg["db"].get_db()
    out = pkg["nudges"].auto_review_should_fire(conn, sid)
    assert out is None


def test_auto_review_force_bypasses_env_flag(tmp_path, monkeypatch):
    """force=True ignores AUTO_REVIEW_ENABLED and the counter check —
    fires whenever there's a rich pending closed thread."""
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=99, skill_interval=999,
                              auto_review=False)
    sid = _open_session(pkg)
    tid = _open_thread_with_notes(pkg, n_total=6, n_rich=3, close=True)
    conn = pkg["db"].get_db()
    out = pkg["nudges"].auto_review_should_fire(conn, sid, force=True)
    assert out == tid


def test_auto_review_force_silent_without_rich_thread(tmp_path, monkeypatch):
    """force=True still requires an actual rich pending thread."""
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=99, skill_interval=999,
                              auto_review=False)
    sid = _open_session(pkg)
    _open_thread_with_notes(pkg, n_total=3, n_rich=1, close=True)  # too thin
    conn = pkg["db"].get_db()
    out = pkg["nudges"].auto_review_should_fire(conn, sid, force=True)
    assert out is None


# ──────────────────────────────────────────────────────────────────────────
# auto_review_trigger MCP tool
# ──────────────────────────────────────────────────────────────────────────

def test_auto_review_trigger_no_pending(tmp_path, monkeypatch):
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=99, skill_interval=99,
                              auto_review=True)
    _open_session(pkg)
    trigger = _tool(pkg, "auto_review_trigger")
    out = trigger()
    assert out.startswith("no_pending")


def test_auto_review_trigger_force_no_pending(tmp_path, monkeypatch):
    """Force=True still returns no_pending when there's no rich thread."""
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=99, skill_interval=99,
                              auto_review=False)
    _open_session(pkg)
    _open_thread_with_notes(pkg, n_total=2, n_rich=0, close=True)
    trigger = _tool(pkg, "auto_review_trigger")
    out = trigger(force=True)
    assert out.startswith("no_pending")


def test_auto_review_trigger_registered(tmp_path, monkeypatch):
    """The new MCP tool must be in the registry."""
    pkg = _bootstrap_with_env(tmp_path, monkeypatch)
    assert "auto_review_trigger" in pkg["mcp"]._tool_manager._tools


def test_auto_review_trigger_force_calls_review_thread(tmp_path, monkeypatch):
    """Force=True with a rich pending closed thread should invoke
    review_thread. We monkeypatch review_thread to avoid spawning a
    real claude process and assert it was called."""
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=99, skill_interval=999,
                              auto_review=False)
    _open_session(pkg)
    tid = _open_thread_with_notes(pkg, n_total=6, n_rich=3, close=True)

    calls: list[dict] = []

    def fake_review_thread(thread_id, focus="combined", mode="auto"):
        calls.append({"thread_id": thread_id, "focus": focus, "mode": mode})
        return f"fake_spawn ok tid={thread_id}"

    from threadkeeper.tools import skills as skills_mod
    monkeypatch.setattr(skills_mod, "review_thread", fake_review_thread)

    trigger = _tool(pkg, "auto_review_trigger")
    out = trigger(focus="skills", force=True)
    assert "triggered" in out
    assert tid in out
    assert calls
    assert calls[0]["thread_id"] == tid
    assert calls[0]["focus"] == "skills"
    assert calls[0]["mode"] == "auto"


def test_close_thread_with_auto_review_disabled_does_not_spawn(tmp_path, monkeypatch):
    """When AUTO_REVIEW_ENABLED is false, close_thread must NOT call
    review_thread, even on a rich thread."""
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=99, skill_interval=3,
                              auto_review=False)
    _open_session(pkg)

    calls: list = []
    from threadkeeper.tools import skills as skills_mod
    monkeypatch.setattr(
        skills_mod,
        "review_thread",
        lambda **kw: (calls.append(kw), "fake")[1],
    )

    _open_thread_with_notes(pkg, n_total=6, n_rich=3, close=True)
    assert calls == []


def test_close_thread_with_auto_review_enabled_spawns(tmp_path, monkeypatch):
    """When AUTO_REVIEW_ENABLED is true AND threshold crossed AND thread
    is rich, close_thread should auto-fire review_thread."""
    pkg = _bootstrap_with_env(tmp_path, monkeypatch,
                              memory_interval=99, skill_interval=3,
                              auto_review=True)
    _open_session(pkg)

    calls: list = []
    from threadkeeper.tools import skills as skills_mod
    monkeypatch.setattr(
        skills_mod,
        "review_thread",
        lambda **kw: (calls.append(kw), "fake")[1],
    )

    tid = _open_thread_with_notes(pkg, n_total=6, n_rich=3, close=True)
    assert len(calls) == 1
    assert calls[0]["thread_id"] == tid
    assert calls[0]["mode"] == "auto"
