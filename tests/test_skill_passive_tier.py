"""Passive skill-use detection must feed tier promotion.

Regression: the ingest-side skill scanner bumped only `use_count` and never
`foreground_use_count`, and never called `_recompute_skill_tier`. So every
skill stayed at tier='hypothesis' forever — the tier ladder could not fire
from real (passive) usage, only from the rarely-called skill_record tool.

The fix routes both scan sites through `_record_skill_use`, which:
  - always bumps use_count (raw),
  - bumps foreground_use_count + recomputes tier ONLY for genuine
    foreground sessions — NOT spawned review-fork children (whose
    self-use must not promote skills, mirroring the dialectic discount).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest


def _bootstrap(tmp_path, monkeypatch):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",
        "THREADKEEPER_MEMORY_GUARD_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CURATOR_INTERVAL_S": "0",
        "THREADKEEPER_EXTRACT_INTERVAL_S": "0",
        "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_NO_EMBEDDINGS": "1",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import db, ingest
    return {"db": db, "ingest": ingest}


@pytest.fixture
def pkg(tmp_path, monkeypatch):
    return _bootstrap(tmp_path, monkeypatch)


def test_foreground_passive_use_promotes_hypothesis_to_observed(pkg):
    """Two foreground Skill invocations → foreground_use_count=2 → observed
    (SKILL_OBSERVED_FG_USES = 2)."""
    ingest, db = pkg["ingest"], pkg["db"]
    conn = db.get_db()
    t0 = int(time.time())
    ingest._record_skill_use(conn, "superpowers:brainstorming", t0, "sess-fg")
    ingest._record_skill_use(
        conn, "superpowers:brainstorming", t0 + 10, "sess-fg"
    )
    conn.commit()
    row = conn.execute(
        "SELECT use_count, foreground_use_count, tier FROM skill_usage "
        "WHERE name=?", ("superpowers:brainstorming",),
    ).fetchone()
    assert row["use_count"] == 2
    assert row["foreground_use_count"] == 2
    assert row["tier"] == "observed"


def test_spawned_child_use_does_not_promote(pkg):
    """A skill invoked inside a spawned review-fork session bumps raw
    use_count but NOT foreground_use_count, so tier stays hypothesis."""
    ingest, db = pkg["ingest"], pkg["db"]
    conn = db.get_db()
    now = int(time.time())
    # Register a spawned child: its cid is recorded as tasks.spawned_cid.
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at, spawned_cid) "
        "VALUES (?,?,?,?,?,?)",
        ("tk_child", 0, "/tmp", "shadow", now, "child-cid-1"),
    )
    conn.commit()
    for i in range(3):
        ingest._record_skill_use(
            conn, "superpowers:test-driven-development",
            now + i, "child-cid-1",
        )
    conn.commit()
    row = conn.execute(
        "SELECT use_count, foreground_use_count, tier FROM skill_usage "
        "WHERE name=?", ("superpowers:test-driven-development",),
    ).fetchone()
    assert row["use_count"] == 3
    assert row["foreground_use_count"] == 0
    assert row["tier"] == "hypothesis"


def test_is_spawned_child_session(pkg):
    ingest, db = pkg["ingest"], pkg["db"]
    conn = db.get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at, spawned_cid) "
        "VALUES (?,?,?,?,?,?)",
        ("tk_x", 0, "/tmp", "p", now, "spawned-1"),
    )
    conn.commit()
    assert ingest._is_spawned_child_session(conn, "spawned-1") is True
    assert ingest._is_spawned_child_session(conn, "foreground-x") is False
    assert ingest._is_spawned_child_session(conn, "") is False
    assert ingest._is_spawned_child_session(conn, None) is False
