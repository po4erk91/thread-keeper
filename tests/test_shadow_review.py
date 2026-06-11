"""Shadow-review autonomous observer — pure functions + MCP wrappers.

Daemon thread isn't tested directly (it just calls run_shadow_pass in a
loop). The contract we care about:
  * cursor advances on every pass
  * empty / too-short windows skip the spawn
  * a window above MIN_CHARS triggers spawn() with the shadow prompt
  * dry_run returns the window without spawning
"""
from __future__ import annotations

import time
import sys
import os
from pathlib import Path

import pytest


_FAKE_CID = "aaaa1111-2222-3333-4444-555566667777"


def _bootstrap(tmp_path, monkeypatch, interval="0", min_chars="50"):
    """Pin shadow-review env to predictable values for the test."""
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": interval,
        "THREADKEEPER_SHADOW_REVIEW_MIN_CHARS": min_chars,
        "THREADKEEPER_SHADOW_REVIEW_WINDOW_S": "3600",
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_FORCE_CID": _FAKE_CID,
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import db, shadow_review, identity
    return {"db": db, "shadow_review": shadow_review, "identity": identity}


def _seed_dialog(conn, role, content, ts, session_id="sess-x"):
    """Direct INSERT into dialog_messages — bypasses ingest for unit-test
    speed. Uses unique uuid based on timestamp+role to avoid collisions."""
    uid = f"u-{ts}-{role}-{abs(hash(content)) % 100000}"
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, project, session_id, "
        "role, content, model, created_at) "
        "VALUES (?, 'claude-code', 'p1', ?, ?, ?, ?, ?)",
        (uid, session_id, role, content, "test-model", ts),
    )


# ──────────────────────────────────────────────────────────────────────
# Pure functions: cursor, window collection
# ──────────────────────────────────────────────────────────────────────

def test_cursor_initial_is_zero(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    assert pkg["shadow_review"]._last_shadow_ts(conn) == 0


def test_cursor_reads_summary_from_events(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    pkg["shadow_review"]._record_shadow_pass(conn, 12345, "no_window")
    pkg["shadow_review"]._record_shadow_pass(conn, 67890, "too_short")
    # most-recent wins
    assert pkg["shadow_review"]._last_shadow_ts(conn) == 67890


def test_collect_window_returns_nothing_when_empty(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    dump, hw, n_chars = pkg["shadow_review"]._collect_window(conn, 0, 3600)
    assert dump == "" and n_chars == 0


def test_collect_window_skips_messages_before_floor(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    _seed_dialog(conn, "user", "old message before floor", now - 1000)
    _seed_dialog(conn, "user", "fresh message after floor", now - 10)
    conn.commit()
    # floor at now-500 → old skipped, fresh kept
    dump, hw, n_chars = pkg["shadow_review"]._collect_window(
        conn, now - 500, 3600,
    )
    assert "fresh message" in dump
    assert "old message" not in dump
    assert hw == now - 10


def test_collect_window_excludes_shadow_observer_sessions(
    tmp_path, monkeypatch,
):
    """The most expensive failure mode of the v0.3 shadow loop was that
    spawned shadow children's own conversations were re-ingested, so the
    next pass saw only its own prior reasoning and SKIPped 60%+ of ticks.
    Sessions whose opening user prompt starts with the shadow-observer
    or close-thread-reviewer marker are excluded from the window."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    # Real user dialog — should appear
    _seed_dialog(conn, "user", "fix flaky network in WDA", now - 30,
                 session_id="real-sess")
    _seed_dialog(conn, "assistant", "On each WDA start, read networksetup; "
                 "if proxy 127.0.0.1 detected, reset before continuing.",
                 now - 25, session_id="real-sess")
    # Shadow observer child's session — first user message starts with the
    # internal marker. ALL of its messages (including its own assistant
    # response) must be excluded, not just the prompt message.
    _seed_dialog(conn, "user",
                 "You are a SHADOW LEARNING OBSERVER for thread-keeper. "
                 "You read a slice of recent…", now - 20,
                 session_id="shadow-sess-1")
    _seed_dialog(conn, "assistant", "SKIP: env-specific E2E debugging",
                 now - 19, session_id="shadow-sess-1")
    # close_thread reviewer child — different marker, same treatment
    _seed_dialog(conn, "user",
                 "You are reviewing closed thread T123. Thread notes:…",
                 now - 15, session_id="review-sess-2")
    _seed_dialog(conn, "assistant", "Nothing to save.",
                 now - 14, session_id="review-sess-2")
    conn.commit()

    dump, _, n_chars = pkg["shadow_review"]._collect_window(conn, 0, 3600)
    assert "flaky network in WDA" in dump
    assert "WDA start, read networksetup" in dump
    assert "SHADOW LEARNING OBSERVER" not in dump
    assert "SKIP:" not in dump
    assert "reviewing closed thread" not in dump
    assert "Nothing to save." not in dump
    # Sanity: char_count reflects only the real-session bytes
    assert n_chars > 0
    assert n_chars < 500  # would be much larger if pollution leaked through


def test_collect_window_excludes_codex_spawned_marker_sessions(
    tmp_path, monkeypatch,
):
    """Codex spawned transcripts can have a rollout UUID as session_id instead
    of tasks.spawned_cid. The injected spawn preamble is the reliable session
    boundary marker."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    codex_rollout = "019eb584-bee1-72a3-ada5-89eaaeab8b8f"
    _seed_dialog(
        conn,
        "user",
        "You were spawned in the background by parent conversation abc. "
        "Your own cid is child-xyz.",
        now - 40,
        session_id=codex_rollout,
    )
    _seed_dialog(
        conn,
        "assistant",
        "MATERIALIZED: fake-child-learning",
        now - 35,
        session_id=codex_rollout,
    )
    _seed_dialog(
        conn,
        "user",
        "real foreground English prompt about the current task",
        now - 20,
        session_id="real-sess",
    )
    conn.commit()

    dump, _, n_chars = pkg["shadow_review"]._collect_window(conn, 0, 3600)
    assert "foreground English prompt" in dump
    assert "fake-child-learning" not in dump
    assert "You were spawned" not in dump
    assert n_chars > 0


def test_collect_window_strips_tool_results_keeps_thinking(
    tmp_path, monkeypatch,
):
    """Tool_result / tool_call adapter-prefixed lines are verbose
    file-dump / shell-output renderings without semantic signal for
    class-level learning. Strip them. Keep [thinking] (chain-of-thought
    often contains the rule being learned)."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    _seed_dialog(conn, "assistant", (
        "Found the root cause: WDA crashes when launchd Wi-Fi proxy "
        "lingers.\n"
        "[thinking] I'll verify by reading networksetup before WDA "
        "start.\n"
        "[tool_result] 100 lines of networksetup output here\n"
        "Decision: always read networksetup and reset 127.0.0.1 proxy "
        "before launching WDA."
    ), now - 30, session_id="real")
    # Pure-noise row should disappear entirely
    _seed_dialog(conn, "assistant",
                 "[tool_result] another 200 lines of grep output\n"
                 "[tool_call] Read(file=/tmp/x.log)",
                 now - 25, session_id="real")
    conn.commit()

    dump, _, n_chars = pkg["shadow_review"]._collect_window(conn, 0, 3600)
    # Tool results stripped
    assert "[tool_result]" not in dump
    assert "[tool_call]" not in dump
    assert "100 lines of networksetup" not in dump
    assert "another 200 lines" not in dump
    # Thinking and plain text preserved
    assert "[thinking]" in dump
    assert "networksetup before WDA start" in dump
    assert "always read networksetup" in dump
    assert "WDA crashes when launchd" in dump
    # Pure-noise row left no trace
    assert "Read(file=/tmp/x.log)" not in dump
    assert n_chars > 0


def test_collect_window_caps_long_messages(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    huge = "X" * 5000  # well above the 1500-cap per-turn
    _seed_dialog(conn, "user", huge, int(time.time()) - 5)
    conn.commit()
    dump, _, n_chars = pkg["shadow_review"]._collect_window(conn, 0, 3600)
    # capped to 1500 + ellipsis char ≈ 1501
    assert n_chars <= 1502
    assert "…" in dump


# ──────────────────────────────────────────────────────────────────────
# run_shadow_pass: dispatch logic
# ──────────────────────────────────────────────────────────────────────

def test_run_shadow_pass_disabled_without_force(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)  # interval=0 → disabled
    assert pkg["shadow_review"].run_shadow_pass() == "disabled"


def test_run_shadow_pass_no_window(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    out = pkg["shadow_review"].run_shadow_pass(force=True)
    assert out == "no_window"
    # cursor advanced (event recorded)
    conn = pkg["db"].get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='shadow_review_pass'"
    ).fetchone()[0]
    assert n == 1


def test_run_shadow_pass_too_short(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, min_chars="500")
    conn = pkg["db"].get_db()
    _seed_dialog(conn, "user", "tiny", int(time.time()) - 5)
    conn.commit()
    out = pkg["shadow_review"].run_shadow_pass(force=True)
    assert out == "too_short"


def test_run_shadow_pass_spawns_when_threshold_met(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, min_chars="100")
    # Stub spawn so the test doesn't fork a real claude.
    import threadkeeper.tools.spawn as spawn_mod
    captured = []

    def fake_spawn(**kwargs):
        captured.append(kwargs)
        return "spawn task_id=fake-shadow-task pid=0"

    monkeypatch.setattr(spawn_mod, "spawn", fake_spawn)

    conn = pkg["db"].get_db()
    long_msg = "Pattern: in this type of task always X. " * 10  # ~400 chars
    _seed_dialog(conn, "user", long_msg, int(time.time()) - 5)
    conn.commit()

    out = pkg["shadow_review"].run_shadow_pass(force=True)
    assert "fake-shadow-task" in out
    assert len(captured) == 1
    kw = captured[0]
    assert kw["slim"] is True
    assert kw["visible"] is False
    assert kw["write_origin"] == "shadow_review"
    assert "SHADOW LEARNING OBSERVER" in kw["prompt"]
    assert "lesson_list(k=80)" in kw["prompt"]
    assert "hard cap 450" in kw["prompt"]
    assert "mcp__thread-keeper__lesson_get" in kw["extra_allowed_tools"]
    assert long_msg.strip()[:40] in kw["prompt"]


def test_run_shadow_pass_single_flight_when_child_running(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, min_chars="100")
    conn = pkg["db"].get_db()
    now = int(time.time())
    long_msg = "Pattern: in this type of task always X. " * 10
    _seed_dialog(conn, "user", long_msg, now - 5)
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at, rss_kb, rss_updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "tk_shadow_running",
            os.getpid(),
            "parent",
            "child",
            str(tmp_path),
            pkg["shadow_review"].SHADOW_REVIEW_PROMPT,
            now - 1,
            123,
            now,
        ),
    )
    conn.commit()

    import threadkeeper.tools.spawn as spawn_mod

    def should_not_spawn(**kwargs):
        raise AssertionError("shadow pass should be single-flight")

    monkeypatch.setattr(spawn_mod, "spawn", should_not_spawn)
    out = pkg["shadow_review"].run_shadow_pass(force=True)
    assert out == "shadow_child_running n=1"
    # Cursor does not advance; retry the same window when the child exits.
    assert pkg["shadow_review"]._last_shadow_ts(conn) == 0


def test_run_shadow_pass_idempotent_after_cursor_advance(tmp_path, monkeypatch):
    """Second pass over the same data must produce no_window once cursor
    catches up."""
    pkg = _bootstrap(tmp_path, monkeypatch, min_chars="100")
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn",
                        lambda **kw: "spawn task_id=t1 pid=0")

    conn = pkg["db"].get_db()
    long_msg = "Y" * 200
    _seed_dialog(conn, "user", long_msg, int(time.time()) - 5)
    conn.commit()

    first = pkg["shadow_review"].run_shadow_pass(force=True)
    assert "t1" in first
    second = pkg["shadow_review"].run_shadow_pass(force=True)
    assert second == "no_window"


# ──────────────────────────────────────────────────────────────────────
# MCP tools
# ──────────────────────────────────────────────────────────────────────

def test_mcp_shadow_review_run_dry_run(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, min_chars="100")
    conn = pkg["db"].get_db()
    _seed_dialog(conn, "assistant", "We learned X" * 30, int(time.time()) - 5)
    conn.commit()
    from threadkeeper._mcp import mcp
    tm = mcp._tool_manager
    tool = tm._tools["shadow_review_run"]
    out = tool.fn(dry_run=True)
    assert "dry_run" in out
    assert "would_spawn=yes" in out
    assert "We learned X" in out
    # cursor MUST NOT advance on dry_run
    assert pkg["shadow_review"]._last_shadow_ts(conn) == 0


def test_daemon_does_not_start_in_slim_child(tmp_path, monkeypatch):
    """Spawned slim children (NO_EMBEDDINGS=1 → SEMANTIC_AVAILABLE=False)
    must NOT fire the shadow daemon. Otherwise every spawn cascades into
    new shadow children which themselves spawn more shadows, etc."""
    monkeypatch.setenv("THREADKEEPER_SHADOW_REVIEW_INTERVAL_S", "60")
    pkg = _bootstrap(tmp_path, monkeypatch, interval="60")
    # Simulate slim-child semantics by patching the constant after import.
    import threadkeeper.config as cfg
    monkeypatch.setattr(cfg, "SEMANTIC_AVAILABLE", False)
    # Reset the module-level _started flag so start_shadow_daemon attempts again.
    pkg["shadow_review"]._started = False
    pkg["shadow_review"].start_shadow_daemon()
    assert pkg["shadow_review"]._started is False, (
        "slim child should refuse to start shadow daemon"
    )


def test_daemon_does_not_start_in_marked_spawned_child(tmp_path, monkeypatch):
    """The cascade guard must not depend only on NO_EMBEDDINGS.

    Some CLIs launch MCP servers from a config env block, so the child
    process may not reliably inherit THREADKEEPER_NO_EMBEDDINGS. The explicit
    THREADKEEPER_SPAWNED_CHILD marker still has to stop shadow_review.
    """
    monkeypatch.setenv("THREADKEEPER_SPAWNED_CHILD", "1")
    pkg = _bootstrap(tmp_path, monkeypatch, interval="60")
    import threadkeeper.config as cfg
    monkeypatch.setattr(cfg, "SEMANTIC_AVAILABLE", True)
    pkg["shadow_review"]._started = False
    pkg["shadow_review"].start_shadow_daemon()
    assert pkg["shadow_review"]._started is False, (
        "marked spawned child should refuse to start shadow daemon"
    )


def test_mcp_shadow_review_status_reports_passes(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    pkg["shadow_review"]._record_shadow_pass(
        pkg["db"].get_db(), 12345, "no_window"
    )
    from threadkeeper._mcp import mcp
    tm = mcp._tool_manager
    tool = tm._tools["shadow_review_status"]
    out = tool.fn()
    assert "interval_s=0" in out
    assert "no_window" in out
