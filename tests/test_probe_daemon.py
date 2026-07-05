"""Probe daemon — isolated self-test driver that fills probe_results +
reliability (both were empty because the probe loop was never run).

Tests cover the pure logic without launching real children: due-probe
selection (objective-only, cooldown), parent-side grading of answer files,
and run_probe_pass dispatch with spawn monkeypatched.
"""
from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path


_FAKE_CID = "bbbb2222-3333-4444-5555-666677778888"


def _bootstrap(tmp_path, monkeypatch, interval="0", cooldown=str(7 * 86400)):
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
        "THREADKEEPER_PROBE_INTERVAL_S": interval,
        "THREADKEEPER_PROBE_COOLDOWN_S": cooldown,
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
    from threadkeeper import db, probe_daemon, identity
    return {"db": db, "pd": probe_daemon, "identity": identity}


def _add_probe(conn, pid, category, grader="regex", pattern="42",
               enabled=1, prompt="task"):
    conn.execute(
        "INSERT INTO probes (id, category, prompt, expected_pattern, grader, "
        "enabled, created_at) VALUES (?,?,?,?,?,?,?)",
        (pid, category, prompt, pattern, grader, enabled, int(time.time())),
    )
    conn.commit()


# ── due-probe selection ────────────────────────────────────────────────

def test_objective_probe_with_no_results_is_due(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_probe(conn, "P001", "date_arithmetic", grader="regex", pattern="42")
    due = pkg["pd"]._due_probes(conn, int(time.time()))
    assert [d["id"] for d in due] == ["P001"]


def test_manual_probe_is_not_due(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_probe(conn, "P002", "detect_contradiction", grader="manual",
               pattern="")
    due = pkg["pd"]._due_probes(conn, int(time.time()))
    assert due == []


def test_objective_probe_without_pattern_is_not_due(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_probe(conn, "P003", "preserve_list_order", grader="exact",
               pattern="")
    assert pkg["pd"]._due_probes(conn, int(time.time())) == []


def test_recently_tested_probe_is_not_due_within_cooldown(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, cooldown="604800")
    conn = pkg["db"].get_db()
    _add_probe(conn, "P004", "count_long_context", grader="regex", pattern="9")
    now = int(time.time())
    conn.execute(
        "INSERT INTO probe_results (probe_id, category, success, created_at) "
        "VALUES (?,?,?,?)",
        ("P004", "count_long_context", 1, now - 3600),  # 1h ago
    )
    conn.commit()
    assert pkg["pd"]._due_probes(conn, now) == []


def test_disabled_probe_is_not_due(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_probe(conn, "P005", "date_arithmetic", enabled=0)
    assert pkg["pd"]._due_probes(conn, int(time.time())) == []


# ── parent-side grading of answer files ────────────────────────────────

def test_grade_pending_records_pass_for_matching_answer(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_probe(conn, "P010", "date_arithmetic", grader="regex", pattern="2026-06-15")
    adir = pkg["pd"]._answer_dir()
    (adir / "P010__abc123.txt").write_text("The answer is 2026-06-15", encoding="utf-8")
    n = pkg["pd"]._grade_pending(conn)
    assert n == 1
    row = conn.execute(
        "SELECT success FROM probe_results WHERE probe_id='P010'"
    ).fetchone()
    assert row["success"] == 1
    # reliability aggregate populated
    rel = conn.execute(
        "SELECT attempts, successes FROM reliability WHERE category='date_arithmetic'"
    ).fetchone()
    assert rel["attempts"] == 1 and rel["successes"] == 1
    # file consumed
    assert not (adir / "P010__abc123.txt").exists()


def test_grade_pending_records_fail_for_nonmatching_answer(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_probe(conn, "P011", "count_long_context", grader="exact", pattern="47")
    adir = pkg["pd"]._answer_dir()
    (adir / "P011__zzz999.txt").write_text("I counted 32 occurrences", encoding="utf-8")
    n = pkg["pd"]._grade_pending(conn)
    assert n == 1
    row = conn.execute(
        "SELECT success FROM probe_results WHERE probe_id='P011'"
    ).fetchone()
    assert row["success"] == 0


def test_grade_pending_deletes_orphan_answer(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    adir = pkg["pd"]._answer_dir()
    (adir / "PXXX__orphan.txt").write_text("no such probe", encoding="utf-8")
    assert pkg["pd"]._grade_pending(conn) == 0
    assert not (adir / "PXXX__orphan.txt").exists()


# ── run_probe_pass dispatch ────────────────────────────────────────────

def test_run_probe_pass_disabled_without_force(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)  # interval=0
    assert pkg["pd"].run_probe_pass() == "disabled"


def test_run_probe_pass_no_due_when_empty(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    out = pkg["pd"].run_probe_pass(force=True)
    assert out.startswith("graded=0") and "no_due" in out


def test_run_probe_pass_spawns_due_probe(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_probe(conn, "P020", "date_arithmetic", grader="regex", pattern="42")

    calls = {}
    def _fake_spawn(**kw):
        calls.update(kw)
        return "ok task=tk_probe pid=4242 child_cid=deadbeef parent_cid=- perm=auto mode=headless"
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", _fake_spawn)

    out = pkg["pd"].run_probe_pass(force=True)
    assert "spawned" in out and "date_arithmetic" in out
    # child got the bare prompt with NO answer key leaked. The answer-file
    # path carries a random hex token (and a tmp dir whose digits are
    # incidental) — that's an artifact location, not answer-key leakage — so
    # drop that whole line before scanning the instructions for the pattern.
    prefix = calls["prompt"].split("TASK:")[0]
    prefix = re.sub(r"(?m)^.*P020__[0-9a-f]+\.txt.*$", "", prefix)
    assert "42" not in prefix
    assert calls["role"] == "probe_runner"
    assert calls["write_origin"] == "probe"
    assert "Write" in calls["extra_allowed_tools"]
    # pass recorded
    assert pkg["pd"]._last_probe_ts(conn) > 0


def test_run_probe_pass_single_flight(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_probe(conn, "P021", "date_arithmetic", grader="regex", pattern="42")
    # a probe child already running (our own pid → alive)
    import os
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at) "
        "VALUES (?,?,?,?,?)",
        ("tk_run", os.getpid(), "/tmp",
         "You are a PROBE RUNNER for category 'x'.", int(time.time())),
    )
    conn.commit()

    def _boom(**kw):
        raise AssertionError("must not spawn while a probe child runs")
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", _boom)

    out = pkg["pd"].run_probe_pass(force=True)
    assert "probe_child_running" in out


def test_run_probe_pass_single_flight_lock_race(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_probe(conn, "P022", "date_arithmetic", grader="regex", pattern="42")

    entered_spawn = threading.Event()
    release_spawn = threading.Event()
    results: list[str] = []
    errors: list[BaseException] = []
    calls: list[str] = []

    def fake_spawn(probe):
        calls.append(probe["id"])
        if len(calls) == 1:
            entered_spawn.set()
            assert release_spawn.wait(timeout=5)
        return "ok task=tk_probe pid=4242"

    def run_pass():
        try:
            out = pkg["pd"].run_probe_pass(force=True)
            results.append(out)
        except BaseException as e:  # pragma: no cover - surfaced below
            errors.append(e)
            release_spawn.set()

    monkeypatch.setattr(pkg["pd"], "_spawn_probe_child", fake_spawn)

    t = threading.Thread(target=run_pass)
    t.start()
    assert entered_spawn.wait(timeout=5)
    results.append(pkg["pd"].run_probe_pass(force=True))
    release_spawn.set()
    t.join(timeout=5)
    assert not t.is_alive()

    assert not errors
    assert calls == ["P022"]
    assert len(results) == 2
    assert sum("spawned" in r for r in results) == 1
    assert (
        results.count("graded=0 probe_child_running n=1 (single-flight lock)")
        == 1
    )
