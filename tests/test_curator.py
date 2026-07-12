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

import json
import os
import sys
import time
from pathlib import Path


_FAKE_CID = "aaaa1111-2222-3333-4444-555566667777"


def _bootstrap(
    tmp_path,
    monkeypatch,
    interval="0",
    min_lessons="3",
    destructive=None,
    retention=None,
    write_origin="foreground",
):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_SKILLS_DIR": str(tmp_path / "skills"),
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
        "THREADKEEPER_WRITE_ORIGIN": write_origin,
    }
    if destructive is not None:
        env["THREADKEEPER_CURATOR_DESTRUCTIVE"] = destructive
    if retention is not None:
        env["THREADKEEPER_CURATOR_SNAPSHOT_RETENTION"] = retention
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
        "skills_dir": Path(env["CLAUDE_SKILLS_DIR"]),
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
    assert "STALE LESSONS (dry-run decay ranking)" in dump


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
        snap_raw = os.environ.get("THREADKEEPER_CURATOR_SNAPSHOT_DIR", "")
        pass_id = os.environ.get("THREADKEEPER_CURATOR_PASS_ID", "")
        assert pass_id
        assert snap_raw
        snap = Path(snap_raw)
        assert snap.is_dir()
        assert (snap / "lessons.md").is_file()
        manifest = json.loads((snap / "manifest.json").read_text())
        assert manifest["pass_id"] == pass_id
        assert manifest["lessons"]["count"] == 2
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
    assert "STALE LESSONS DRY-RUN" in kw["prompt"]
    assert "do NOT call lesson_remove solely" in kw["prompt"]
    assert "EVOLVE_CANDIDATE" in kw["prompt"]
    assert "evolve_format" in kw["prompt"]
    assert "lesson-one" in kw["prompt"]
    assert "lesson-two" in kw["prompt"]
    # Scoped toolset — destructive default (the new default) includes
    # lesson_append / lesson_remove / skill_manage, but never shell or spawn.
    allowed = kw["extra_allowed_tools"]
    assert "lesson_list" in allowed
    assert "lesson_get" in allowed
    assert "lesson_append" in allowed
    assert "lesson_remove" in allowed
    assert "skill_manage" in allowed
    assert "evolve_format" in allowed
    assert "Read" in allowed
    assert "Write" in allowed
    assert "Bash" not in allowed
    # REPORTS_DIR was created so the child has a place to write
    assert pkg["reports_dir"].is_dir()
    assert "THREADKEEPER_CURATOR_PASS_ID" not in os.environ
    assert "THREADKEEPER_CURATOR_SNAPSHOT_DIR" not in os.environ


def test_run_curator_pass_recent_high_water_is_not_due(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="3600", min_lessons="2")
    pkg["lessons"].append_lesson(
        title="lesson one", body="body one", source="shadow"
    )
    pkg["lessons"].append_lesson(
        title="lesson two", body="body two", source="shadow"
    )
    conn = pkg["db"].get_db()
    last = 2_000_000
    pkg["curator"]._record_curator_pass(conn, last, "spawned previous")
    monkeypatch.setattr(pkg["curator"].time, "time", lambda: last + 10)

    import threadkeeper.tools.spawn as spawn_mod

    def fail_spawn(**kwargs):  # pragma: no cover - should not be called
        raise AssertionError("spawn should not run before interval elapses")

    monkeypatch.setattr(spawn_mod, "spawn", fail_spawn)

    assert pkg["curator"].run_curator_pass() == "not_due"
    rows = conn.execute(
        "SELECT target, summary FROM events WHERE kind='curator_pass' "
        "ORDER BY id ASC"
    ).fetchall()
    assert rows[-1]["summary"] == "not_due"
    assert rows[-1]["target"] == str(last)


def test_run_curator_pass_stale_high_water_spawns(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="3600", min_lessons="2")
    pkg["lessons"].append_lesson(
        title="lesson one", body="body one", source="shadow"
    )
    pkg["lessons"].append_lesson(
        title="lesson two", body="body two", source="shadow"
    )
    conn = pkg["db"].get_db()
    now = 2_000_000
    pkg["curator"]._record_curator_pass(
        conn, now - 3601, "spawned previous"
    )
    monkeypatch.setattr(pkg["curator"].time, "time", lambda: now)

    import threadkeeper.tools.spawn as spawn_mod
    captured: list[dict] = []

    def fake_spawn(**kwargs):
        captured.append(kwargs)
        return "spawn task_id=fake-curator-stale pid=0"

    monkeypatch.setattr(spawn_mod, "spawn", fake_spawn)

    out = pkg["curator"].run_curator_pass()
    assert "fake-curator-stale" in out
    assert len(captured) == 1


def test_run_curator_pass_force_bypasses_recent_high_water(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="3600", min_lessons="2")
    pkg["lessons"].append_lesson(
        title="lesson one", body="body one", source="shadow"
    )
    pkg["lessons"].append_lesson(
        title="lesson two", body="body two", source="shadow"
    )
    conn = pkg["db"].get_db()
    now = 2_000_000
    pkg["curator"]._record_curator_pass(
        conn, now - 10, "spawned previous"
    )
    monkeypatch.setattr(pkg["curator"].time, "time", lambda: now)

    import threadkeeper.tools.spawn as spawn_mod
    captured: list[dict] = []

    def fake_spawn(**kwargs):
        captured.append(kwargs)
        return "spawn task_id=fake-curator-forced pid=0"

    monkeypatch.setattr(spawn_mod, "spawn", fake_spawn)

    out = pkg["curator"].run_curator_pass(force=True)
    assert "fake-curator-forced" in out
    assert len(captured) == 1


def test_run_curator_pass_advisory_writes_no_snapshot(tmp_path, monkeypatch):
    pkg = _bootstrap(
        tmp_path, monkeypatch, min_lessons="2", destructive="0",
    )
    pkg["lessons"].append_lesson(
        title="lesson one", body="body one", source="shadow"
    )
    pkg["lessons"].append_lesson(
        title="lesson two", body="body two", source="shadow"
    )

    import threadkeeper.tools.spawn as spawn_mod
    captured: list[dict] = []

    def fake_spawn(**kwargs):
        assert "THREADKEEPER_CURATOR_PASS_ID" not in os.environ
        assert "THREADKEEPER_CURATOR_SNAPSHOT_DIR" not in os.environ
        captured.append(kwargs)
        return "spawn task_id=fake-curator-task pid=0"

    monkeypatch.setattr(spawn_mod, "spawn", fake_spawn)

    out = pkg["curator"].run_curator_pass(force=True)
    assert "fake-curator-task" in out
    assert len(captured) == 1
    assert not (pkg["reports_dir"] / "snapshots").exists()
    allowed = captured[0]["extra_allowed_tools"]
    assert "lesson_remove" not in allowed
    assert "skill_manage" not in allowed


def test_curator_snapshot_retention_is_bounded(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, retention="2")
    pkg["lessons"].append_lesson(
        title="lesson one", body="body one", source="shadow"
    )
    from threadkeeper.curator_snapshots import create_curator_snapshot

    conn = pkg["db"].get_db()
    for pass_id in ("20260101T000000", "20260102T000000", "20260103T000000"):
        create_curator_snapshot(pass_id, conn=conn, retention=2)

    root = pkg["reports_dir"] / "snapshots"
    assert not (root / "20260101T000000").exists()
    assert (root / "20260102T000000").is_dir()
    assert (root / "20260103T000000").is_dir()


def test_curator_restore_recovers_pruned_lesson_body(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="curator")
    pkg["lessons"].append_lesson(
        title="restore target lesson",
        body="recover this durable body",
        summary="recover me",
        source="shadow",
    )
    from threadkeeper.curator_snapshots import create_curator_snapshot

    conn = pkg["db"].get_db()
    pass_id = "20260101T000000"
    snap = create_curator_snapshot(pass_id, conn=conn, retention=10)
    monkeypatch.setenv("THREADKEEPER_CURATOR_PASS_ID", pass_id)
    monkeypatch.setenv("THREADKEEPER_CURATOR_SNAPSHOT_DIR", str(snap))

    from threadkeeper._mcp import mcp

    remove = mcp._tool_manager._tools["lesson_remove"].fn
    restore = mcp._tool_manager._tools["curator_restore"].fn
    get = mcp._tool_manager._tools["lesson_get"].fn

    out = remove(slug="restore-target-lesson")
    assert out.startswith("ok removed="), out
    assert "ERR not_found" in get(slug="restore-target-lesson")
    tombstone = snap / "tombstones" / "lesson" / "lesson_pruned" / (
        "restore-target-lesson.md"
    )
    assert tombstone.is_file()
    assert "recover this durable body" in tombstone.read_text()

    out = restore(pass_id=pass_id, lesson_slug="restore-target-lesson")
    assert out.startswith("ok restored_lesson="), out
    assert "recover this durable body" in get(slug="restore-target-lesson")


def test_run_curator_pass_skips_unchanged_inventory(
    tmp_path, monkeypatch,
):
    """A second wake-up over the same stable inventory endorses the last
    pass instead of spawning another full curator child."""
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
        return f"spawn task_id=fake-curator-{len(captured)} pid=0"

    monkeypatch.setattr(spawn_mod, "spawn", fake_spawn)

    first = pkg["curator"].run_curator_pass(force=True)
    second = pkg["curator"].run_curator_pass(force=True)

    assert "fake-curator-1" in first
    assert second.startswith("unchanged_inventory")
    assert len(captured) == 1

    conn = pkg["db"].get_db()
    rows = conn.execute(
        "SELECT summary FROM events WHERE kind='curator_pass' "
        "ORDER BY id ASC"
    ).fetchall()
    assert "spawned inventory_sha256=" in rows[-2]["summary"]
    assert "unchanged_inventory inventory_sha256=" in rows[-1]["summary"]

    pkg["lessons"].append_lesson(
        title="lesson three", body="body three", source="shadow"
    )
    third = pkg["curator"].run_curator_pass(force=True)
    assert "fake-curator-2" in third
    assert len(captured) == 2


def test_curator_wakeup_coalesces_before_rereading_inflight_snapshot(
    tmp_path, monkeypatch,
):
    """When a curator child is already running, another wake-up coalesces
    behind it and does not re-read the same inventory snapshot."""
    pkg = _bootstrap(tmp_path, monkeypatch, min_lessons="2")
    pkg["lessons"].append_lesson(title="one", body="b1", source="shadow")
    pkg["lessons"].append_lesson(title="two", body="b2", source="shadow")
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO tasks "
        "(id, pid, parent_cid, spawned_cid, cwd, prompt, started_at) "
        "VALUES ('tk_running_curator', ?, 'p', 'c', '/x', ?, ?)",
        (
            os.getpid(),
            "You are an autonomous CURATOR for thread-keeper's lessons + "
            "skills library.",
            int(time.time()) - 30,
        ),
    )
    conn.commit()

    def fail_fingerprint(_conn):  # pragma: no cover - should not be called
        raise AssertionError("in-flight wake-up should not re-read inventory")

    monkeypatch.setattr(
        pkg["curator"], "_current_inventory_fingerprint", fail_fingerprint,
    )

    import threadkeeper.tools.spawn as spawn_mod

    def fail_spawn(**kwargs):  # pragma: no cover - should not be called
        raise AssertionError("spawn should not run while a curator is active")

    monkeypatch.setattr(spawn_mod, "spawn", fail_spawn)

    out = pkg["curator"].run_curator_pass(force=True)

    assert out == "curator_running n=1 (single-flight)"


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
    assert "STALE LESSONS (dry-run decay ranking)" in out
    # cursor MUST NOT advance on dry_run
    conn = pkg["db"].get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='curator_pass'"
    ).fetchone()[0]
    assert n == 0


def test_mcp_curator_review_status_reports(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    pkg["curator"]._record_curator_pass(
        pkg["db"].get_db(), 12345,
        "spawned inventory_sha256=" + ("a" * 64) + " lessons=2",
    )
    from threadkeeper._mcp import mcp
    tool = mcp._tool_manager._tools["curator_review_status"].fn
    out = tool()
    assert "interval_s=0" in out
    assert "spawned inventory_sha256=" in out
    assert "inventory_sha256=" + ("a" * 64) in out
    assert "current_inventory_sha256=" in out
    assert "latest_report=(none yet)" in out
    # Default mode is now destructive (CURATOR_DESTRUCTIVE defaults to 1)
    assert "mode=destructive" in out


def test_curator_dry_run_ranks_stale_lessons_by_decay_score(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, min_lessons="2")
    now = 1_800_000_000
    old = now - 90 * 86400
    monkeypatch.setattr(pkg["lessons"].time, "time", lambda: old)
    for title in [
        "never pulled",
        "one old pull",
        "pinned lesson",
        "validated lesson",
    ]:
        pkg["lessons"].append_lesson(
            title=title, body=f"{title} body", source="shadow"
        )

    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO lesson_usage "
        "(slug, created_at, source, last_used_at, use_count) "
        "VALUES (?, ?, 'shadow', ?, 1)",
        ("one-old-pull", old, now - 70 * 86400),
    )
    conn.execute(
        "INSERT INTO lesson_usage "
        "(slug, created_at, source, last_viewed_at, view_count, pinned) "
        "VALUES (?, ?, 'shadow', ?, 1, 1)",
        ("pinned-lesson", old, now - 80 * 86400),
    )
    conn.execute(
        "INSERT INTO lesson_usage "
        "(slug, created_at, source, last_viewed_at, view_count, tier) "
        "VALUES (?, ?, 'shadow', ?, 1, 'validated')",
        ("validated-lesson", old, now - 80 * 86400),
    )
    conn.commit()
    monkeypatch.setattr(pkg["lessons"].time, "time", lambda: now)
    monkeypatch.setattr(pkg["curator"].time, "time", lambda: now)

    from threadkeeper._mcp import mcp
    tool = mcp._tool_manager._tools["curator_review"].fn
    out = tool(dry_run=True)

    stale = out.split("## STALE LESSONS (dry-run decay ranking)", 1)[1]
    stale = stale.split("## SKILLS", 1)[0]
    assert "Advisory only" in stale
    assert "score=" in stale
    assert stale.index("never-pulled") < stale.index("one-old-pull")
    assert "pinned-lesson" not in stale
    assert "validated-lesson" not in stale


def test_destructive_mode_widens_allowed_tools(tmp_path, monkeypatch):
    """When CURATOR_DESTRUCTIVE=1 the curator child gets skill_manage +
    lesson_append in extra_allowed_tools AND the prompt instructs it
    to apply its own recommendations directly."""
    monkeypatch.setenv("THREADKEEPER_CURATOR_DESTRUCTIVE", "1")
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
        return "spawn task_id=fake-curator pid=0"

    monkeypatch.setattr(spawn_mod, "spawn", fake_spawn)

    pkg["curator"].run_curator_pass(force=True)
    assert len(captured) == 1
    kw = captured[0]
    allowed = kw["extra_allowed_tools"]
    # Destructive mode → widened toolset (incl. lesson_remove for prune/consolidate)
    assert "skill_manage" in allowed
    assert "lesson_append" in allowed
    assert "lesson_remove" in allowed
    assert "evolve_format" in allowed
    # Prompt explicitly flips into destructive mode
    assert "DESTRUCTIVE MODE ENABLED" in kw["prompt"]
    assert "audit trail first" in kw["prompt"]
    # PROTECTED guarantee still in effect
    assert "PROTECTED" in kw["prompt"]


def test_advisory_mode_excludes_destructive_tools(
    tmp_path, monkeypatch,
):
    """With THREADKEEPER_CURATOR_DESTRUCTIVE=0 the curator child is read-only:
    prompt forbids skill_manage/lesson_append/lesson_remove and they aren't in
    allowed_tools. (Destructive is the default, so advisory is now opt-in.)"""
    monkeypatch.setenv("THREADKEEPER_CURATOR_DESTRUCTIVE", "0")
    pkg = _bootstrap(tmp_path, monkeypatch, min_lessons="2")
    pkg["lessons"].append_lesson(
        title="a", body="b1", source="shadow"
    )
    pkg["lessons"].append_lesson(
        title="b", body="b2", source="shadow"
    )

    import threadkeeper.tools.spawn as spawn_mod
    captured: list[dict] = []

    def fake_spawn(**kwargs):
        captured.append(kwargs)
        return "spawn task_id=fake pid=0"

    monkeypatch.setattr(spawn_mod, "spawn", fake_spawn)

    pkg["curator"].run_curator_pass(force=True)
    kw = captured[0]
    allowed = kw["extra_allowed_tools"]
    assert "skill_manage" not in allowed
    assert "lesson_append" not in allowed
    assert "evolve_format" in allowed
    assert "ADVISORY MODE" in kw["prompt"]
    assert "DESTRUCTIVE MODE ENABLED" not in kw["prompt"]


def test_single_flight_when_curator_child_running(tmp_path, monkeypatch):
    """The curator mutates ONE shared store (lessons.md + skills); a second
    pass must not spawn while a curator child is already running, even when the
    inventory is above threshold and force=True. Cross-process single-flight."""
    pkg = _bootstrap(tmp_path, monkeypatch, min_lessons="2")
    pkg["lessons"].append_lesson(title="one", body="b1", source="shadow")
    pkg["lessons"].append_lesson(title="two", body="b2", source="shadow")
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO tasks "
        "(id, pid, parent_cid, spawned_cid, cwd, prompt, started_at) "
        "VALUES ('tk_running_curator', ?, 'p', 'c', '/x', ?, ?)",
        (
            os.getpid(),
            "You are an autonomous CURATOR for thread-keeper's lessons + "
            "skills library.",
            int(time.time()) - 30,
        ),
    )
    conn.commit()

    import threadkeeper.tools.spawn as spawn_mod

    def fail_spawn(**kwargs):  # pragma: no cover - should not be called
        raise AssertionError("spawn should not run while a curator is active")

    monkeypatch.setattr(spawn_mod, "spawn", fail_spawn)

    out = pkg["curator"].run_curator_pass(force=True)

    assert out == "curator_running n=1 (single-flight)"
    row = conn.execute(
        "SELECT summary FROM events WHERE kind='curator_pass' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert "curator_running n=1" in row["summary"]


# ──────────────────────────────────────────────────────────────────────
# Concepts review (F1) — curator also audits the concepts store
# ──────────────────────────────────────────────────────────────────────

def _add_concept(conn, cid, desc, confidence="medium",
                 registered_at=None, last_evidence_at=None):
    now = int(time.time())
    conn.execute(
        "INSERT INTO concepts (id, description, confidence, registered_at, "
        "last_evidence_at) VALUES (?,?,?,?,?)",
        (cid, desc, confidence, registered_at or now, last_evidence_at),
    )
    conn.commit()


def test_collect_concepts_empty(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    text, n = pkg["curator"]._collect_concepts(conn)
    assert n == 0
    assert text == ""


def test_collect_concepts_lists_with_age(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    _add_concept(conn, "Cfresh", "fresh high-conf idea",
                 confidence="high", last_evidence_at=now - 86400)  # 1d
    _add_concept(conn, "Cstale", "stale low-conf idea",
                 confidence="low",
                 registered_at=now - 40 * 86400,
                 last_evidence_at=None)  # never corroborated, 40d old
    text, n = pkg["curator"]._collect_concepts(conn)
    assert n == 2
    assert "Cfresh" in text and "Cstale" in text
    assert "CONCEPTS (n=2)" in text
    # stale concept (no last_evidence, registered 40d ago) shows ~40d age
    assert "40d_ago" in text
    # oldest-first ordering: stale concept appears before fresh one
    assert text.index("Cstale") < text.index("Cfresh")


def test_run_curator_pass_includes_concepts_in_inventory(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, min_lessons="2")
    pkg["lessons"].append_lesson(title="a", body="b1", source="shadow")
    pkg["lessons"].append_lesson(title="b", body="b2", source="shadow")
    conn = pkg["db"].get_db()
    _add_concept(conn, "Cabc", "asymmetric in-band reactivity",
                 confidence="high")

    import threadkeeper.tools.spawn as spawn_mod
    captured: list[dict] = []
    monkeypatch.setattr(
        spawn_mod, "spawn",
        lambda **kw: captured.append(kw) or "spawn task_id=fake pid=0",
    )
    pkg["curator"].run_curator_pass(force=True)
    prompt = captured[0]["prompt"]
    assert "CONCEPTS (n=1)" in prompt
    assert "Cabc" in prompt
    assert "asymmetric in-band reactivity" in prompt


def test_destructive_toolset_includes_concept_manage(tmp_path, monkeypatch):
    """In destructive mode the curator child can apply its own
    CONSOLIDATE_CONCEPT / PRUNE_CONCEPT recommendations: concept_manage is in
    the allowed toolset and the prompt instructs it how (#75). Before #75 the
    concept rubric was permanently advisory because no concept tool was wired."""
    monkeypatch.setenv("THREADKEEPER_CURATOR_DESTRUCTIVE", "1")
    pkg = _bootstrap(tmp_path, monkeypatch, min_lessons="2")
    pkg["lessons"].append_lesson(title="a", body="b1", source="shadow")
    pkg["lessons"].append_lesson(title="b", body="b2", source="shadow")
    conn = pkg["db"].get_db()
    _add_concept(conn, "Cdup", "a near-duplicate idea", confidence="low")

    import threadkeeper.tools.spawn as spawn_mod
    captured: list[dict] = []
    monkeypatch.setattr(
        spawn_mod, "spawn",
        lambda **kw: captured.append(kw) or "spawn task_id=fake pid=0",
    )
    pkg["curator"].run_curator_pass(force=True)
    kw = captured[0]
    allowed = kw["extra_allowed_tools"]
    assert "concept_manage" in allowed
    assert "list_concepts" in allowed
    assert "expand_concept" in allowed
    # Prompt tells the child how to apply concept recommendations.
    assert "CONSOLIDATE_CONCEPT" in kw["prompt"]
    assert "PRUNE_CONCEPT" in kw["prompt"]
    assert "concept_manage" in kw["prompt"]


def test_advisory_toolset_excludes_concept_manage(tmp_path, monkeypatch):
    """Advisory mode is read-only: concept_manage must NOT be granted, though
    the read-only concept tools may be (so the child can inspect descriptions)."""
    monkeypatch.setenv("THREADKEEPER_CURATOR_DESTRUCTIVE", "0")
    pkg = _bootstrap(tmp_path, monkeypatch, min_lessons="2")
    pkg["lessons"].append_lesson(title="a", body="b1", source="shadow")
    pkg["lessons"].append_lesson(title="b", body="b2", source="shadow")

    import threadkeeper.tools.spawn as spawn_mod
    captured: list[dict] = []
    monkeypatch.setattr(
        spawn_mod, "spawn",
        lambda **kw: captured.append(kw) or "spawn task_id=fake pid=0",
    )
    pkg["curator"].run_curator_pass(force=True)
    allowed = captured[0]["extra_allowed_tools"]
    assert "concept_manage" not in allowed


def test_concepts_alone_do_not_trigger_pass(tmp_path, monkeypatch):
    """Concepts enrich the review but don't lower the lesson threshold —
    a pass still requires CURATOR_MIN_LESSONS lessons."""
    pkg = _bootstrap(tmp_path, monkeypatch, min_lessons="3")
    conn = pkg["db"].get_db()
    _add_concept(conn, "Conly", "a lone concept", confidence="high")

    import threadkeeper.tools.spawn as spawn_mod
    called = []
    monkeypatch.setattr(spawn_mod, "spawn",
                        lambda **kw: called.append(kw) or "x")
    out = pkg["curator"].run_curator_pass(force=True)
    assert out.startswith("below_threshold")
    assert called == []
