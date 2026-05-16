"""Extract daemon — periodic auto-harvest of decision-shaped utterances
into the extract_candidates queue.

The daemon thread is a thin wrapper around extract_recent() with a
cursor in events.kind='extract_pass'. We test the wrapper and the
internal-session filter we added to extract_recent for self-pollution
guarding.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path


_FAKE_CID = "aaaa1111-2222-3333-4444-555566667777"


def _bootstrap(tmp_path, monkeypatch, interval="0", window_min="30"):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CURATOR_INTERVAL_S": "0",
        "THREADKEEPER_EXTRACT_INTERVAL_S": interval,
        "THREADKEEPER_EXTRACT_WINDOW_MIN": window_min,
        "THREADKEEPER_LESSONS": str(tmp_path / "lessons.md"),
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
    from threadkeeper import db, extract_daemon, identity
    return {
        "db": db,
        "extract_daemon": extract_daemon,
        "identity": identity,
    }


def _seed_dialog(conn, role, content, ts, session_id="user-sess"):
    uid = f"u-{ts}-{role}-{abs(hash(content)) % 100000}"
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, project, session_id, "
        "role, content, model, created_at) "
        "VALUES (?, 'claude-code', 'p1', ?, ?, ?, ?, ?)",
        (uid, session_id, role, content, "test-model", ts),
    )


# ──────────────────────────────────────────────────────────────────────
# Pure functions: cursor advance
# ──────────────────────────────────────────────────────────────────────

def test_cursor_initial_is_zero(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    assert pkg["extract_daemon"]._last_extract_ts(conn) == 0


def test_cursor_advances_after_pass(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    pkg["extract_daemon"]._record_extract_pass(conn, 12345, "no_dialog window=30m")
    pkg["extract_daemon"]._record_extract_pass(conn, 67890, "ok scanned=5")
    assert pkg["extract_daemon"]._last_extract_ts(conn) == 67890


# ──────────────────────────────────────────────────────────────────────
# run_extract_pass dispatch
# ──────────────────────────────────────────────────────────────────────

def test_run_extract_pass_disabled_without_force(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)  # interval=0 → disabled
    assert pkg["extract_daemon"].run_extract_pass() == "disabled"


def test_run_extract_pass_advances_cursor_when_forced(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)  # interval=0, but forced
    out = pkg["extract_daemon"].run_extract_pass(force=True)
    # extract_recent returns "no_dialog window=30m" when nothing seeded
    assert "no_dialog" in out
    conn = pkg["db"].get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='extract_pass'"
    ).fetchone()[0]
    assert n == 1


def test_run_extract_pass_picks_up_user_want_pattern(tmp_path, monkeypatch):
    """Seed a 'want' utterance, run extract, confirm a verbatim
    candidate was enqueued."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    _seed_dialog(
        conn, "user",
        "I want you to always verify the state transition, not just "
        "the destination label being visible — that's the false-"
        "positive trap we keep hitting.",
        now - 60, session_id="real-sess",
    )
    conn.commit()

    out = pkg["extract_daemon"].run_extract_pass(force=True)
    assert "ok" in out
    # Candidate must be queued
    rows = conn.execute(
        "SELECT kind, content FROM extract_candidates WHERE status='pending'"
    ).fetchall()
    kinds = {r["kind"] for r in rows}
    assert "verbatim" in kinds, f"got kinds: {kinds}"


def test_extract_filters_noise_content_prefixes(tmp_path, monkeypatch):
    """Message-level noise filter — even within a valid session,
    individual messages matching known noise prefixes (compaction
    summary, SKILL.md injection, subagent prompt, interrupt service
    string) must NOT become extract candidates."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    # Real session with mixed valid + noise messages
    _seed_dialog(
        conn, "user",
        "I want you to always run tests before merging — that's the "
        "policy I expect every CI gate to enforce.",
        now - 120, session_id="real-sess",
    )
    # Compaction summary — must be skipped
    _seed_dialog(
        conn, "user",
        "This session is being continued from a previous conversation "
        "that ran out of context. The summary below covers the earlier "
        "portion of the conversation.",
        now - 100, session_id="real-sess",
    )
    # SKILL.md injection — must be skipped
    _seed_dialog(
        conn, "user",
        "Base directory for this skill: /Users/dmytro/.claude/skills/"
        "e2e-walk-flow-manually-before-runner-iteration",
        now - 80, session_id="real-sess",
    )
    # Subagent task prompt — must be skipped
    _seed_dialog(
        conn, "user",
        "In the repo at /Users/dmytro/masthead, find all code related "
        "to the content translation language switch limit.",
        now - 60, session_id="real-sess",
    )
    # Interrupt service string — must be skipped (also short, but
    # prefix filter catches it independent of length)
    _seed_dialog(
        conn, "user",
        "[Request interrupted by user for tool use]",
        now - 40, session_id="real-sess",
    )
    conn.commit()

    pkg["extract_daemon"].run_extract_pass(force=True)
    rows = conn.execute(
        "SELECT content FROM extract_candidates WHERE status='pending'"
    ).fetchall()
    # Real "I want you to" gets through
    assert any("policy I expect" in r["content"] for r in rows)
    # None of the noise rows enqueued
    for noise in (
        "This session is being continued",
        "Base directory for this skill",
        "In the repo at /Users/",
        "[Request interrupted by user",
    ):
        assert not any(noise in r["content"] for r in rows), (
            f"noise leaked: {noise!r}"
        )


def test_extract_filters_subagent_role_prompts(tmp_path, monkeypatch):
    """Subagent / spawn-child role prompts ('You are the X', 'Research
    task', 'Design task', 'Context:') aren't user intent — they're
    parent-injected task framing that polluted the first calibration
    pass. Filter them at the message level."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    # Valid user_want
    _seed_dialog(
        conn, "user",
        "I want you to never run destructive operations without "
        "confirmation in production.",
        now - 200, session_id="real-sess",
    )
    # Subagent role prompts — each must be filtered
    for i, prompt in enumerate([
        "You are the news-arc curator for the-masthead. You have "
        "THREE jobs in this run.",
        "You are a researcher. Analyze the codebase and identify the "
        "module responsible for proxy state.",
        "You are an editor with strict formatting rules. Rewrite the "
        "passage below as a numbered list.",
        "Research task. Read /Users/dmytro/ai-memory/server.py and "
        "report the top-level functions.",
        "Design task for memory-partner. Sketch the schema migration "
        "plan, do not implement.",
        "Context: This summary will be shown in a list to help users "
        "choose which conversations are relevant.",
    ]):
        _seed_dialog(
            conn, "user", prompt, now - (180 - i * 20),
            session_id="real-sess",
        )
    conn.commit()

    pkg["extract_daemon"].run_extract_pass(force=True)
    rows = conn.execute(
        "SELECT content FROM extract_candidates WHERE status='pending'"
    ).fetchall()
    # Valid stays
    assert any("never run destructive" in r["content"] for r in rows)
    # All subagent prompts filtered
    for noise in (
        "You are the news-arc curator",
        "You are a researcher",
        "You are an editor",
        "Research task",
        "Design task for memory-partner",
        "Context: This summary",
    ):
        assert not any(noise in r["content"] for r in rows), (
            f"subagent prompt leaked: {noise!r}"
        )


def test_extract_skips_short_user_want_matches(tmp_path, monkeypatch):
    """Verbatim-specific min length — '[Request interrupted by user for
    tool use]' is 42 chars; even if its prefix didn't match the noise
    list, the length floor would catch it. Conversely, a 50+-char
    legitimate `I want you to ...` line gets through."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    # Just-above-min-length valid user_want
    _seed_dialog(
        conn, "user",
        "I want you to write down the WDA reset rule for sure.",
        now - 60, session_id="ok-sess",
    )
    # Just-below-min-length: 40 chars total, still has 'i want to'
    _seed_dialog(
        conn, "user",
        "I want to break for lunch.",
        now - 40, session_id="ok-sess",
    )
    conn.commit()
    pkg["extract_daemon"].run_extract_pass(force=True)
    rows = conn.execute(
        "SELECT content FROM extract_candidates WHERE status='pending'"
    ).fetchall()
    assert any("WDA reset rule" in r["content"] for r in rows)
    assert not any("break for lunch" in r["content"] for r in rows)


def test_extract_skips_test_runner_log_dumps(tmp_path, monkeypatch):
    """Log dumps with checkmark/cross density should not yield candidates
    — even when they contain words matching _WANT_RE inside log lines."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    log_dump = (
        "Earnings Withdrawal — Direct Deposit (OAuth) with Wallet V2 "
        "lambdas (NTV601)\n"
        "  ✓ runFixture:registerUser → $ahmed (2.0s)\n"
        "  ✓ runFixture:fundWallet → $ahmed-funded (1.2s)\n"
        "  ✓ find labelContains 'Add payout method' → $addMethod\n"
        "  ✗ tap $addMethod (no element) → I want to retry but "
        "got blocked\n"
        "  ○ find labelContains 'Congrats' → $proveCongrats skipped\n"
    )
    _seed_dialog(conn, "user", log_dump, now - 60, session_id="ok-sess")
    # Plus a clean valid utterance in same session
    _seed_dialog(
        conn, "user",
        "I want you to never retry a failing tap automatically, "
        "always surface the error to me first.",
        now - 30, session_id="ok-sess",
    )
    conn.commit()
    pkg["extract_daemon"].run_extract_pass(force=True)
    rows = conn.execute(
        "SELECT content FROM extract_candidates WHERE status='pending'"
    ).fetchall()
    assert any("never retry a failing tap" in r["content"] for r in rows)
    assert not any("runFixture:registerUser" in r["content"] for r in rows)


def test_extract_filters_shadow_observer_sessions(tmp_path, monkeypatch):
    """Shadow-observer's own session start with the marker prompt — any
    seemingly substantive utterance INSIDE that session should NOT
    become an extract candidate (same self-pollution rule as
    shadow_review._collect_window)."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    # Shadow-observer child's session — entire session must be skipped
    _seed_dialog(
        conn, "user",
        "You are a SHADOW LEARNING OBSERVER for thread-keeper. You "
        "read a slice of recent dialog…",
        now - 90, session_id="shadow-sess",
    )
    _seed_dialog(
        conn, "assistant",
        "I want to highlight a rule: always reset network before WDA "
        "start. That's a class-level recovery procedure.",
        now - 85, session_id="shadow-sess",
    )
    # Real user session — should be picked up
    _seed_dialog(
        conn, "user",
        "I want you to record decision notes automatically without "
        "waiting for the agent to remember.",
        now - 60, session_id="real-sess",
    )
    conn.commit()

    out = pkg["extract_daemon"].run_extract_pass(force=True)
    assert "ok" in out
    rows = conn.execute(
        "SELECT source_cid, content FROM extract_candidates "
        "WHERE status='pending'"
    ).fetchall()
    # Real session yielded a candidate
    assert any(r["source_cid"] == "real-sess" for r in rows)
    # Shadow session yielded NONE
    assert not any(r["source_cid"] == "shadow-sess" for r in rows)


# ──────────────────────────────────────────────────────────────────────
# Daemon lifecycle
# ──────────────────────────────────────────────────────────────────────

def test_daemon_does_not_start_in_slim_child(tmp_path, monkeypatch):
    """Slim children (SEMANTIC_AVAILABLE=False) must refuse to start
    the extract daemon — otherwise each spawn cascades into N more."""
    monkeypatch.setenv("THREADKEEPER_EXTRACT_INTERVAL_S", "600")
    pkg = _bootstrap(tmp_path, monkeypatch, interval="600")
    import threadkeeper.config as cfg
    monkeypatch.setattr(cfg, "SEMANTIC_AVAILABLE", False)
    pkg["extract_daemon"]._started = False
    pkg["extract_daemon"].start_extract_daemon()
    assert pkg["extract_daemon"]._started is False, (
        "slim child must refuse to start extract daemon"
    )


def test_daemon_disabled_at_interval_zero(tmp_path, monkeypatch):
    """interval=0 (default) → no daemon thread started."""
    pkg = _bootstrap(tmp_path, monkeypatch, interval="0")
    pkg["extract_daemon"]._started = False
    pkg["extract_daemon"].start_extract_daemon()
    assert pkg["extract_daemon"]._started is False
