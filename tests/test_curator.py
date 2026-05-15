"""Autonomous Curator — periodic library audit.

The Curator inspection is LLM-driven, so we don't fork a real Claude
in unit tests. We exercise the pure scaffolding:

  * cursor advances on each pass
  * empty inventory → below_threshold (skip spawn)
  * inventory ≥ CURATOR_MIN_LESSONS → spawn() invoked with right args
  * dry_run returns inventory without spawning
  * REPORTS_DIR created on first spawn so the child has a place to write
  * daemon does NOT start in slim children (cascade prevention, same
    pattern as shadow_review)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path


_FAKE_CID = "aaaa1111-2222-3333-4444-555566667777"


def _bootstrap(tmp_path, monkeypatch, interval="0", min_lessons="3"):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CURATOR_INTERVAL_S": interval,
        "THREADKEEPER_CURATOR_MIN_LESSONS": min_lessons,
        "THREADKEEPER_CURATOR_REPORTS_DIR": str(tmp_path / "curator"),
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
    from threadkeeper import db, curator, identity, lessons
    return {
        "db": db,
        "curator": curator,
        "identity": identity,
        "lessons": lessons,
        "reports_dir": Path(env["THREADKEEPER_CURATOR_REPORTS_DIR"]),
        "lessons_path": Path(env["THREADKEEPER_LESSONS"]),
    }


# ──────────────────────────────────────────────────────────────────────
# Pure functions
# ──────────────────────────────────────────────────────────────────────

def test_cursor_initial_is_zero(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    assert pkg["curator"]._last_curator_ts(conn) == 0


def test_cursor_reads_latest_event(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    pkg["curator"]._record_curator_pass(conn, 12345, "below_threshold")
    pkg["curator"]._record_curator_pass(conn, 67890, "spawned task=t1")
    assert pkg["curator"]._last_curator_ts(conn) == 67890


def test_collect_inventory_empty(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    dump, n_lessons, n_skills = pkg["curator"]._collect_inventory(conn)
    assert n_lessons == 0
    assert n_skills == 0
    assert "LESSONS (n=0)" in dump
    assert "SKILLS (n=0)" in dump
    assert "(none)" in dump


def test_collect_inventory_counts_lessons_and_skills(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    pkg["lessons"].append_lesson(
        title="reset wifi proxy before WDA start",
        body="Always read networksetup; if 127.0.0.1, reset.",
        source="shadow",
    )
    pkg["lessons"].append_lesson(
        title="testID drift detection",
        body="Before chasing logic, check fixture testIDs.",
        source="foreground",
    )
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO skill_usage "
        "(name, created_at, created_by_origin, last_used_at, "
        " use_count, pinned, state) "
        "VALUES (?, ?, 'foreground', ?, 5, 1, 'active')",
        ("pinned-skill", now - 86400, now - 3600),
    )
    conn.execute(
        "INSERT INTO skill_usage "
        "(name, created_at, created_by_origin, last_used_at, "
        " use_count, state) "
        "VALUES (?, ?, 'background_review', ?, 2, 'active')",
        ("auto-created-skill", now - 172800, now - 7200),
    )
    conn.commit()

    dump, n_lessons, n_skills = pkg["curator"]._collect_inventory(conn)
    assert n_lessons == 2
    assert n_skills == 2
    # foreground-origin lesson is PROTECTED
    assert "testid-drift-detection [PROTECTED]" in dump
    # shadow-origin lesson is NOT protected
    assert "reset-wifi-proxy-before-wda-start [PROTECTED]" not in dump
    # pinned + foreground skill is PROTECTED
    assert "SKILL pinned-skill [PROTECTED]" in dump
    # background_review skill (not pinned) is NOT protected
    assert "SKILL auto-created-skill [PROTECTED]" not in dump
    assert "SKILL auto-created-skill" in dump


# ──────────────────────────────────────────────────────────────────────
# run_curator_pass — dispatch logic
# ──────────────────────────────────────────────────────────────────────

def test_run_curator_pass_disabled_without_force(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)  # interval=0 → disabled
    assert pkg["curator"].run_curator_pass() == "disabled"


def test_run_curator_pass_below_threshold(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, min_lessons="3")
    pkg["lessons"].append_lesson(
        title="only one lesson", body="not enough", source="shadow"
    )
    out = pkg["curator"].run_curator_pass(force=True)
    assert out.startswith("below_threshold")
    conn = pkg["db"].get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='curator_pass'"
    ).fetchone()[0]
    assert n == 1  # cursor advanced


def test_run_curator_pass_spawns_when_threshold_met(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, min_lessons="2")
    pkg["lessons"].append_lesson(
        title="lesson one", body="body one", source="shadow"
    )
    pkg["lessons"].append_lesson(
        title="lesson two", body="body two", source="shadow"
    )

    import threadkeeper.tools.spawn as spawn_mod
    captured: list[dict] = []

    def fake_spawn(**kwargs):
        captured.append(kwargs)
        return "spawn task_id=fake-curator-task pid=0"

    monkeypatch.setattr(spawn_mod, "spawn", fake_spawn)

    out = pkg["curator"].run_curator_pass(force=True)
    assert "fake-curator-task" in out
    assert len(captured) == 1
    kw = captured[0]
    assert kw["slim"] is True
    assert kw["visible"] is False
    assert kw["role"] == "curator"
    assert kw["write_origin"] == "curator"
    # Prompt contains the rubric + the inventory
    assert "KEEP" in kw["prompt"]
    assert "PATCH" in kw["prompt"]
    assert "CONSOLIDATE" in kw["prompt"]
    assert "PRUNE" in kw["prompt"]
    assert "lesson-one" in kw["prompt"]
    assert "lesson-two" in kw["prompt"]
    # Scoped toolset — no shell, no spawn, no destructive lesson_append
    allowed = kw["extra_allowed_tools"]
    assert "lesson_list" in allowed
    assert "lesson_get" in allowed
    assert "Read" in allowed
    assert "Write" in allowed
    assert "lesson_append" not in allowed
    assert "skill_manage" not in allowed
    assert "Bash" not in allowed
    # REPORTS_DIR was created so the child has a place to write
    assert pkg["reports_dir"].is_dir()


def test_daemon_does_not_start_in_slim_child(tmp_path, monkeypatch):
    """Slim children (NO_EMBEDDINGS=1 → SEMANTIC_AVAILABLE=False) must
    NOT start the curator daemon. Otherwise every spawn would cascade
    into curator spawning more children, etc."""
    monkeypatch.setenv("THREADKEEPER_CURATOR_INTERVAL_S", "604800")
    pkg = _bootstrap(tmp_path, monkeypatch, interval="604800")
    import threadkeeper.config as cfg
    monkeypatch.setattr(cfg, "SEMANTIC_AVAILABLE", False)
    pkg["curator"]._started = False
    pkg["curator"].start_curator_daemon()
    assert pkg["curator"]._started is False, (
        "slim child must refuse to start curator daemon"
    )


# ──────────────────────────────────────────────────────────────────────
# MCP tools
# ──────────────────────────────────────────────────────────────────────

def test_mcp_curator_review_dry_run_shows_inventory(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, min_lessons="2")
    pkg["lessons"].append_lesson(
        title="reset wifi before wda", body="b1", source="shadow"
    )
    pkg["lessons"].append_lesson(
        title="testid drift detection", body="b2", source="foreground"
    )
    from threadkeeper._mcp import mcp
    tool = mcp._tool_manager._tools["curator_review"].fn
    out = tool(dry_run=True)
    assert "dry_run" in out
    assert "would_spawn=yes" in out
    assert "reset-wifi-before-wda" in out
    assert "testid-drift-detection" in out
    # cursor MUST NOT advance on dry_run
    conn = pkg["db"].get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='curator_pass'"
    ).fetchone()[0]
    assert n == 0


def test_mcp_curator_review_status_reports(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    pkg["curator"]._record_curator_pass(
        pkg["db"].get_db(), 12345, "below_threshold lessons=1"
    )
    from threadkeeper._mcp import mcp
    tool = mcp._tool_manager._tools["curator_review_status"].fn
    out = tool()
    assert "interval_s=0" in out
    assert "below_threshold" in out
    assert "latest_report=(none yet)" in out
