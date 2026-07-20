"""Notifier daemon — surfaces silent loop/spawn failures + materialization
(issue #257). Uses the log channel for observable assertions.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import pytest

_FAKE_CID = "aaaa1111-2222-3333-4444-555566667777"


def _bootstrap(tmp_path, monkeypatch, *, poll="0", channel="log",
               loop_fail="true", skill="false", lesson="false", cooldown="3600"):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        # every other daemon off
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CURATOR_INTERVAL_S": "0",
        "THREADKEEPER_PROBE_INTERVAL_S": "0",
        "THREADKEEPER_EXTRACT_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",
        # notifier under test
        "THREADKEEPER_NOTIFY_POLL_S": poll,
        "THREADKEEPER_NOTIFY_CHANNEL": channel,
        "THREADKEEPER_NOTIFY_LOOP_FAILURE": loop_fail,
        "THREADKEEPER_NOTIFY_SKILL_MATERIALIZED": skill,
        "THREADKEEPER_NOTIFY_LESSON": lesson,
        "THREADKEEPER_NOTIFY_FAILURE_COOLDOWN_S": cooldown,
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
        "THREADKEEPER_LESSONS": str(tmp_path / "lessons.md"),
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_FORCE_CID": _FAKE_CID,
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(env["THREADKEEPER_TASK_LOG_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import db, notify, identity
    return {"db": db, "notify": notify, "identity": identity}


def _ev(conn, kind, summary="", target="", ts=None):
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?,?,?,?,?)",
        ("s", kind, target, summary, ts or int(time.time())),
    )
    conn.commit()


def _task(conn, task_id, rc, role, ended_at):
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at, ended_at, "
        "return_code, role) VALUES (?,?,?,?,?,?,?,?)",
        (task_id, 0, "/tmp", "p", ended_at - 1, ended_at, rc, role),
    )
    conn.commit()


# ── classifier (pure) ───────────────────────────────────────────────────────

@pytest.mark.parametrize("summary,expected", [
    ("ERR claude_cli_not_found", "failure"),
    ("spawn_error: boom", "failure"),
    ("graded=2 spawn_error: budget", "failure"),
    ("spawned pending=3 :: ERR token_budget_exceeded", "failure"),
    ("spawn_failed cli=claude reason=binary_not_found", "failure"),
    ("not_due", "neutral"),
    ("no_window w=30m", "neutral"),
    ("too_short chars=5", "neutral"),
    ("ok task=abc pid=1", "neutral"),
    ("shadow_child_running n=1", "neutral"),
    ("below_threshold", "neutral"),
    ("graded=2 no_due", "neutral"),
    ("", "neutral"),
])
def test_classify(tmp_path, monkeypatch, summary, expected):
    m = _bootstrap(tmp_path, monkeypatch)
    assert m["notify"].classify_summary(summary) == expected


# ── failure detection ───────────────────────────────────────────────────────

def test_failure_pass_fires_one_notification(tmp_path, monkeypatch, caplog):
    m = _bootstrap(tmp_path, monkeypatch)
    notify, db = m["notify"], m["db"]
    conn = db.get_db()
    assert notify.run_notify_pass(force=True) == "seed"      # first run seeds
    _ev(conn, "curator_pass", "spawn_error: budget_exceeded", target="123")
    with caplog.at_level(logging.WARNING, logger="threadkeeper.notify"):
        out = notify.run_notify_pass(force=True)
    assert out == "ok fired=1", out
    assert "[notify]" in caplog.text and "curator loop failed" in caplog.text
    assert "budget exhausted" in caplog.text


def test_dead_child_fires_notification_with_log_reason(tmp_path, monkeypatch, caplog):
    m = _bootstrap(tmp_path, monkeypatch)
    notify, db = m["notify"], m["db"]
    conn = db.get_db()
    assert notify.run_notify_pass(force=True) == "seed"
    (tmp_path / "tasks" / "child-1.log").write_text(
        "starting…\nerror: credit balance too low\n")
    _task(conn, "child-1", rc=1, role="curator", ended_at=int(time.time()))
    with caplog.at_level(logging.WARNING, logger="threadkeeper.notify"):
        out = notify.run_notify_pass(force=True)
    assert out == "ok fired=1", out
    assert "curator child died (rc=1)" in caplog.text
    assert "credit balance too low" in caplog.text


def test_timeout_and_zero_exit_children_ignored(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    notify, db = m["notify"], m["db"]
    conn = db.get_db()
    assert notify.run_notify_pass(force=True) == "seed"
    now = int(time.time())
    _task(conn, "ok-child", rc=0, role="curator", ended_at=now)       # success
    _task(conn, "timeout-child", rc=124, role="probe", ended_at=now)  # timeout (retried)
    out = notify.run_notify_pass(force=True)
    assert out == "ok fired=0", out


# ── cooldown / de-dup ───────────────────────────────────────────────────────

def test_cooldown_dedups_repeat_failures(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch, cooldown="3600")
    notify, db = m["notify"], m["db"]
    conn = db.get_db()
    assert notify.run_notify_pass(force=True) == "seed"
    _ev(conn, "curator_pass", "spawn_error: a")
    _ev(conn, "curator_pass", "spawn_error: b")      # same loop, same pass
    assert notify.run_notify_pass(force=True) == "ok fired=1"   # coalesced
    _ev(conn, "curator_pass", "spawn_error: c")      # still within cooldown
    assert notify.run_notify_pass(force=True) == "ok fired=0"


def test_distinct_loops_both_fire(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch)
    notify, db = m["notify"], m["db"]
    conn = db.get_db()
    assert notify.run_notify_pass(force=True) == "seed"
    _ev(conn, "curator_pass", "spawn_error: x")
    _ev(conn, "probe_pass", "graded=1 spawn_error: y")
    assert notify.run_notify_pass(force=True) == "ok fired=2"


# ── first-run seed ──────────────────────────────────────────────────────────

def test_first_run_seeds_no_backlog(tmp_path, monkeypatch, caplog):
    m = _bootstrap(tmp_path, monkeypatch, poll="60")   # >0 so scheduled tick runs
    notify, db = m["notify"], m["db"]
    conn = db.get_db()
    _ev(conn, "curator_pass", "spawn_error: historical")   # pre-seed backlog
    _ev(conn, "shadow_review_pass", "spawn_error: old")
    with caplog.at_level(logging.WARNING, logger="threadkeeper.notify"):
        assert notify.run_notify_pass(scheduled=True) == "seed"
    assert "[notify]" not in caplog.text                   # backlog never fires
    _ev(conn, "curator_pass", "spawn_error: fresh")        # after seed
    with caplog.at_level(logging.WARNING, logger="threadkeeper.notify"):
        assert notify.run_notify_pass(force=True) == "ok fired=1"


# ── positive toggles ────────────────────────────────────────────────────────

def test_positives_off_by_default(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch, skill="false", lesson="false")
    notify, db = m["notify"], m["db"]
    conn = db.get_db()
    assert notify.run_notify_pass(force=True) == "seed"
    _ev(conn, "skill_materialized", target="T1", summary="/skills/foo/SKILL.md")
    _ev(conn, "lesson_append", target="my-lesson", summary="op=create source=shadow")
    assert notify.run_notify_pass(force=True) == "ok fired=0"


def test_positives_on_fire(tmp_path, monkeypatch, caplog):
    m = _bootstrap(tmp_path, monkeypatch, skill="true", lesson="true")
    notify, db = m["notify"], m["db"]
    conn = db.get_db()
    assert notify.run_notify_pass(force=True) == "seed"
    _ev(conn, "skill_materialized", target="T1", summary="/skills/foo/SKILL.md")
    _ev(conn, "lesson_append", target="my-lesson", summary="op=create source=shadow")
    with caplog.at_level(logging.WARNING, logger="threadkeeper.notify"):
        out = notify.run_notify_pass(force=True)
    assert out == "ok fired=2", out
    assert "skill materialized" in caplog.text
    assert "lesson added" in caplog.text and "my-lesson" in caplog.text


# ── disabled ────────────────────────────────────────────────────────────────

def test_disabled_when_poll_zero(tmp_path, monkeypatch):
    m = _bootstrap(tmp_path, monkeypatch, poll="0")
    assert m["notify"].run_notify_pass(scheduled=True) == "disabled"
