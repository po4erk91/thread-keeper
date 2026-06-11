"""Candidate-reviewer daemon — fifth learning loop. Closes the
extract → SKILL.md gap. Tests the scaffolding (cursor advance,
threshold, spawn invocation, slim-child cascade prevention) — the
actual LLM decision is exercised in production with a real `claude -p`
fork.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


_FAKE_CID = "aaaa1111-2222-3333-4444-555566667777"


def _bootstrap(tmp_path, monkeypatch, interval="0", min_n="3"):
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
        "THREADKEEPER_EXTRACT_INTERVAL_S": "0",
        "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S": interval,
        "THREADKEEPER_CANDIDATE_REVIEW_MIN": min_n,
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
    from threadkeeper import db, candidate_reviewer, identity
    return {
        "db": db,
        "candidate_reviewer": candidate_reviewer,
        "identity": identity,
    }


def _seed_pending(conn, kind, content, source_cid="real-sess", age_s=60):
    now = int(time.time())
    conn.execute(
        "INSERT INTO extract_candidates "
        "(kind, source_uuid, source_cid, content, rationale, status, "
        " created_at) VALUES (?,?,?,?,?, 'pending', ?)",
        (kind, f"u-{now}-{abs(hash(content)) % 10000}", source_cid,
         content, f"H1 {kind}_pattern", now - age_s),
    )


# ──────────────────────────────────────────────────────────────────────
# Cursor + inventory
# ──────────────────────────────────────────────────────────────────────

def test_cursor_initial_is_zero(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    assert pkg["candidate_reviewer"]._last_review_ts(conn) == 0


def test_cursor_advances_after_pass(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    pkg["candidate_reviewer"]._record_review_pass(conn, 12345, "below_threshold")
    pkg["candidate_reviewer"]._record_review_pass(conn, 67890, "spawned")
    assert pkg["candidate_reviewer"]._last_review_ts(conn) == 67890


def test_collect_pending_empty_returns_zero(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    dump, n = pkg["candidate_reviewer"]._collect_pending(conn)
    assert n == 0
    assert dump == ""


def test_collect_pending_lists_recent_candidates(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _seed_pending(conn, "verbatim", "I want X policy", age_s=60)
    _seed_pending(conn, "concept", "ABCD bullet rules apply", age_s=120)
    conn.commit()
    dump, n = pkg["candidate_reviewer"]._collect_pending(conn)
    assert n == 2
    assert "I want X policy" in dump
    assert "ABCD bullet" in dump
    assert "PENDING CANDIDATES (n=2)" in dump


def test_collect_pending_excludes_stale_candidates(tmp_path, monkeypatch):
    """Anything older than 30 days is stale — likely overtaken by
    fresh dialog. Don't surface to the reviewer child."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _seed_pending(conn, "verbatim", "fresh candidate", age_s=3600)
    _seed_pending(conn, "verbatim", "ancient candidate",
                  age_s=40 * 86400)
    conn.commit()
    dump, n = pkg["candidate_reviewer"]._collect_pending(conn)
    assert n == 1
    assert "fresh candidate" in dump
    assert "ancient candidate" not in dump


def test_collect_pending_excludes_already_accepted(tmp_path, monkeypatch):
    """Only status='pending' candidates surface. Already-accepted and
    already-rejected ones are out of the loop's concern."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _seed_pending(conn, "verbatim", "pending one", age_s=60)
    now = int(time.time())
    conn.execute(
        "INSERT INTO extract_candidates "
        "(kind, source_uuid, source_cid, content, rationale, status, "
        " created_at) VALUES (?,?,?,?,?, 'accepted', ?)",
        ("verbatim", "u-x", "sess-x", "accepted one", "?", now - 60),
    )
    conn.execute(
        "INSERT INTO extract_candidates "
        "(kind, source_uuid, source_cid, content, rationale, status, "
        " created_at) VALUES (?,?,?,?,?, 'rejected', ?)",
        ("verbatim", "u-y", "sess-y", "rejected one", "?", now - 60),
    )
    conn.commit()
    dump, n = pkg["candidate_reviewer"]._collect_pending(conn)
    assert n == 1
    assert "pending one" in dump
    assert "accepted one" not in dump
    assert "rejected one" not in dump


# ──────────────────────────────────────────────────────────────────────
# Dispatch
# ──────────────────────────────────────────────────────────────────────

def test_run_disabled_without_force(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)  # interval=0
    assert pkg["candidate_reviewer"].run_review_pass() == "disabled"


def test_run_below_threshold_records_no_spawn(tmp_path, monkeypatch):
    """Two pending candidates with min=3 → below threshold; no spawn."""
    pkg = _bootstrap(tmp_path, monkeypatch, min_n="3")
    conn = pkg["db"].get_db()
    _seed_pending(conn, "verbatim", "one", age_s=60)
    _seed_pending(conn, "concept", "two", age_s=60)
    conn.commit()

    out = pkg["candidate_reviewer"].run_review_pass(force=True)
    assert out.startswith("below_threshold")
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='candidate_review_pass'"
    ).fetchone()[0]
    assert n == 1


def test_run_spawns_when_threshold_met(tmp_path, monkeypatch):
    """Five pending candidates with min=3 → spawn fires; reviewer
    child gets the right toolset and prompt."""
    pkg = _bootstrap(tmp_path, monkeypatch, min_n="3")
    conn = pkg["db"].get_db()
    for i in range(5):
        _seed_pending(conn, "verbatim",
                      f"candidate utterance {i} I want you to do X",
                      source_cid=f"real-{i}", age_s=60 + i)
    conn.commit()

    import threadkeeper.tools.spawn as spawn_mod
    captured: list[dict] = []

    def fake_spawn(**kwargs):
        captured.append(kwargs)
        return "spawn task_id=fake-reviewer pid=0"

    monkeypatch.setattr(spawn_mod, "spawn", fake_spawn)

    out = pkg["candidate_reviewer"].run_review_pass(force=True)
    assert "fake-reviewer" in out
    assert len(captured) == 1
    kw = captured[0]
    assert kw["slim"] is True
    assert kw["visible"] is False
    assert kw["role"] == "candidate_reviewer"
    assert kw["write_origin"] == "candidate_review"
    # Prompt scaffolding + inventory in the spawn payload
    assert "CANDIDATE REVIEWER" in kw["prompt"]
    assert "PENDING CANDIDATES (n=5)" in kw["prompt"]
    assert "I want you to do X" in kw["prompt"]
    # Toolset — can act (skill_manage, accept, reject) but not bash
    allowed = kw["extra_allowed_tools"]
    assert "skill_manage" in allowed
    assert "accept_candidate" in allowed
    assert "reject_candidate" in allowed
    assert "Bash" not in allowed


def test_single_flight_when_reviewer_child_running(tmp_path, monkeypatch):
    """Candidate review consumes one global queue; don't spawn duplicates."""
    pkg = _bootstrap(tmp_path, monkeypatch, min_n="3")
    conn = pkg["db"].get_db()
    for i in range(4):
        _seed_pending(conn, "verbatim", f"candidate {i}", age_s=60 + i)
    conn.execute(
        "INSERT INTO tasks "
        "(id, pid, parent_cid, spawned_cid, cwd, prompt, started_at) "
        "VALUES ('tk_running_review', ?, 'p', 'c', '/x', ?, ?)",
        (
            os.getpid(),
            "You are a CANDIDATE REVIEWER for thread-keeper's extract queue.",
            int(time.time()) - 30,
        ),
    )
    conn.commit()

    import threadkeeper.tools.spawn as spawn_mod

    def fail_spawn(**kwargs):  # pragma: no cover - should not be called
        raise AssertionError("spawn should not run while reviewer is active")

    monkeypatch.setattr(spawn_mod, "spawn", fail_spawn)

    out = pkg["candidate_reviewer"].run_review_pass(force=True)

    assert out == "candidate_review_running n=1 (single-flight)"
    row = conn.execute(
        "SELECT summary FROM events WHERE kind='candidate_review_pass' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert "candidate_review_running n=1" in row["summary"]


# ──────────────────────────────────────────────────────────────────────
# Daemon lifecycle
# ──────────────────────────────────────────────────────────────────────

def test_daemon_does_not_start_in_slim_child(tmp_path, monkeypatch):
    """Cascade prevention — slim children can't fire this daemon
    either (would recurse via spawn into more reviewers)."""
    monkeypatch.setenv("THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S", "3600")
    pkg = _bootstrap(tmp_path, monkeypatch, interval="3600")
    import threadkeeper.config as cfg
    monkeypatch.setattr(cfg, "SEMANTIC_AVAILABLE", False)
    pkg["candidate_reviewer"]._started = False
    pkg["candidate_reviewer"].start_candidate_reviewer_daemon()
    assert pkg["candidate_reviewer"]._started is False


def test_daemon_silent_at_interval_zero(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="0")
    pkg["candidate_reviewer"]._started = False
    pkg["candidate_reviewer"].start_candidate_reviewer_daemon()
    assert pkg["candidate_reviewer"]._started is False


# ──────────────────────────────────────────────────────────────────────
# MCP tools
# ──────────────────────────────────────────────────────────────────────

def test_mcp_candidate_review_run_dry_run(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, min_n="3")
    conn = pkg["db"].get_db()
    for i in range(4):
        _seed_pending(conn, "verbatim", f"text {i}", age_s=60)
    conn.commit()
    from threadkeeper._mcp import mcp
    tool = mcp._tool_manager._tools["candidate_review_run"].fn
    out = tool(dry_run=True)
    assert "dry_run" in out
    assert "would_spawn=yes" in out
    assert "pending=4" in out
    # cursor must NOT advance on dry_run
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='candidate_review_pass'"
    ).fetchone()[0]
    assert n == 0


def test_mcp_candidate_review_status(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, min_n="3")
    conn = pkg["db"].get_db()
    _seed_pending(conn, "verbatim", "one", age_s=60)
    conn.commit()
    pkg["candidate_reviewer"]._record_review_pass(
        conn, 12345, "below_threshold pending=1 min=3",
    )
    from threadkeeper._mcp import mcp
    tool = mcp._tool_manager._tools["candidate_review_status"].fn
    out = tool()
    assert "interval_s=0" in out
    assert "pending_now=1" in out
    assert "below_threshold" in out
