from __future__ import annotations

import json
import os
import time


_FAKE_CID = "33334444-5555-6666-7777-888899990000"


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _insert_task(pkg, task_id: str, prompt: str, rss_mb: int = 0):
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at, ended_at, rss_kb, rss_updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            os.getpid(),
            _FAKE_CID,
            f"child-{task_id}",
            "/tmp",
            prompt,
            now - 65,
            None,
            rss_mb * 1024,
            now,
        ),
    )
    conn.commit()


def _insert_completed_task(pkg, task_id: str, prompt: str, log_text: str):
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at, ended_at, return_code, rss_kb, rss_updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            os.getpid(),
            _FAKE_CID,
            f"child-{task_id}",
            "/tmp",
            prompt,
            now - 120,
            now - 5,
            0,
            0,
            now,
        ),
    )
    pkg["config"].TASK_LOG_DIR.mkdir(parents=True, exist_ok=True)
    (pkg["config"].TASK_LOG_DIR / f"{task_id}.log").write_text(log_text)
    conn.commit()


def test_agent_status_snapshot_reports_running_agents(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _insert_task(
        pkg,
        "tk_review",
        "You are a CANDIDATE REVIEWER for thread-keeper's extract queue.\n\n"
        "Review pending candidates.",
        rss_mb=386,
    )
    _insert_task(pkg, "tk_generic", "Build a compact menu-bar status app.", rss_mb=42)

    from threadkeeper.agent_status import agent_status_snapshot

    snap = agent_status_snapshot(refresh=False)
    assert snap["running_count"] == 2
    assert snap["total_rss_mb"] == 428
    by_id = {a["task_id"]: a for a in snap["agents"]}
    assert by_id["tk_review"]["name"] == "candidate_reviewer"
    assert "Reviews extracted conversation candidates" in by_id["tk_review"][
        "description"
    ]
    assert by_id["tk_review"]["status"] == "running"
    assert by_id["tk_review"]["elapsed"] == "1m"
    assert by_id["tk_generic"]["name"] == "child-tk"
    assert by_id["tk_generic"]["description"].startswith("Spawned child task")
    by_loop = {loop["id"]: loop for loop in snap["loops"]}
    assert "Reviews extracted conversation candidates" in by_loop[
        "candidate_reviewer"
    ]["description"]
    assert by_loop["candidate_reviewer"]["status"] == "running"
    assert by_loop["candidate_reviewer"]["running_agent_count"] == 1
    assert by_loop["candidate_reviewer"]["rss_mb"] == 386
    assert by_loop["extract"]["status"] == "off"
    assert all(loop["description"] for loop in snap["loops"])


def test_agent_status_orders_active_loops_first(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    pkg["config"].INGEST_INTERVAL_S = 3600
    pkg["config"].CANDIDATE_REVIEW_INTERVAL_S = 3600
    pkg["config"].CANDIDATE_REVIEW_MIN = 1
    _insert_task(
        pkg,
        "tk_shadow",
        "You are a SHADOW LEARNING OBSERVER for thread-keeper.\n\n"
        "Scan recent dialog for durable lessons.",
        rss_mb=300,
    )
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO extract_candidates "
        "(kind, content, status, created_at) VALUES "
        "('note', 'pending candidate', 'pending', ?)",
        (int(time.time()),),
    )
    conn.commit()

    from threadkeeper.agent_status import agent_status_snapshot

    snap = agent_status_snapshot(refresh=False)
    loop_ids = [loop["id"] for loop in snap["loops"]]
    statuses = [loop["status"] for loop in snap["loops"]]

    assert loop_ids[:3] == ["shadow_review", "candidate_reviewer", "ingest"]
    assert statuses[:3] == ["running", "ready", "idle"]
    assert statuses == sorted(statuses, key={
        "running": 0,
        "ready": 1,
        "idle": 2,
        "off": 3,
    }.get)


def test_agent_status_evolve_applier_ready_when_promoted_queue_exists(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    pkg["config"].EVOLVE_APPLY_INTERVAL_S = 604800
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO evolve (suggestion, rationale, applied, created_at, status) "
        "VALUES ('score verbatim by reuse', 'promoted by reviewer', 0, ?, 'promoted')",
        (int(time.time()),),
    )
    conn.commit()

    from threadkeeper.agent_status import agent_status_snapshot

    snap = agent_status_snapshot(refresh=False)
    loop = {l["id"]: l for l in snap["loops"]}["evolve_apply"]
    assert loop["enabled"] is True
    assert loop["backlog_count"] == 1
    assert loop["backlog_label"] == "apply work items"
    assert loop["status"] == "ready"


def test_agent_status_evolve_applier_ready_when_curator_report_exists(
    mp_with_cid,
):
    pkg = mp_with_cid(_FAKE_CID)
    pkg["config"].EVOLVE_APPLY_INTERVAL_S = 604800
    reports_dir = pkg["tmp"] / "curator"
    reports_dir.mkdir()
    pkg["config"].CURATOR_REPORTS_DIR = reports_dir
    (reports_dir / "REPORT-20260611T120000.md").write_text(
        "# report\n\nPATCH: stale-skill\n\nCURATOR_PASS_COMPLETE\n",
        encoding="utf-8",
    )

    from threadkeeper.agent_status import agent_status_snapshot

    snap = agent_status_snapshot(refresh=False)
    loop = {l["id"]: l for l in snap["loops"]}["evolve_apply"]
    assert loop["backlog_count"] == 1
    assert loop["backlog_label"] == "apply work items"
    assert loop["status"] == "ready"


def test_agent_status_evolve_applier_prefers_completion_event(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    pkg["config"].EVOLVE_APPLY_INTERVAL_S = 604800
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?, 'evolve_apply_pass', ?, ?, ?)",
        (_FAKE_CID, str(now - 120), "applier_running n=2", now - 120),
    )
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?, 'curator_report_applied', ?, ?, ?)",
        (
            _FAKE_CID,
            "/tmp/REPORT.md",
            "Applied 4 lesson consolidations",
            now - 5,
        ),
    )
    conn.commit()

    from threadkeeper.agent_status import agent_status_snapshot

    snap = agent_status_snapshot(refresh=False)
    loop = {l["id"]: l for l in snap["loops"]}["evolve_apply"]
    assert loop["last_summary"] == "Applied 4 lesson consolidations"
    assert loop["work"] == "Applied 4 lesson consolidations"


def test_agent_status_probe_backlog_counts_only_due_objective_probes(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    pkg["config"].PROBE_INTERVAL_S = 1800
    pkg["config"].PROBE_COOLDOWN_S = 86400
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.executemany(
        "INSERT INTO probes "
        "(id, category, prompt, expected_pattern, grader, enabled, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            ("P_due", "date_arithmetic", "task", "42", "regex", 1, now),
            ("P_recent", "count_long_context", "task", "9", "regex", 1, now),
            ("P_manual", "detect_contradiction", "task", None, "manual", 1, now),
            ("P_no_key", "preserve_list_order", "task", None, "exact", 1, now),
        ],
    )
    conn.execute(
        "INSERT INTO probe_results "
        "(probe_id, category, success, created_at) VALUES (?,?,?,?)",
        ("P_recent", "count_long_context", 1, now - 3600),
    )
    conn.commit()

    from threadkeeper.agent_status import agent_status_snapshot

    snap = agent_status_snapshot(refresh=False)
    loop = {l["id"]: l for l in snap["loops"]}["probe"]
    assert loop["backlog_count"] == 1
    assert loop["backlog_label"] == "due probes"
    assert loop["ready_backlog_min"] == 1
    assert loop["status"] == "ready"


def test_agent_status_recent_results_for_useful_completed_tasks(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _insert_completed_task(
        pkg,
        "tk_review_done",
        "You are a CANDIDATE REVIEWER for thread-keeper's extract queue.",
        "noise\nProcessed 2 candidates into durable notes.\n",
    )

    from threadkeeper.agent_status import agent_status_snapshot

    snap = agent_status_snapshot(refresh=False)

    assert snap["recent_results"][0]["task_id"] == "tk_review_done"
    assert snap["recent_results"][0]["loop_id"] == "candidate_reviewer"
    assert snap["recent_results"][0]["summary"] == (
        "Processed 2 candidates into durable notes."
    )


def test_agent_status_mcp_json_output(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _insert_task(pkg, "tk_status", "Build a compact menu-bar status app.", rss_mb=100)

    raw = _tool(pkg, "agent_status")(json_output=True, refresh=False)
    data = json.loads(raw)
    assert data["running_count"] == 1
    assert "loops" in data
    assert data["agents"][0]["task_id"] == "tk_status"
    assert data["agents"][0]["rss_mb"] == 100


def test_agent_status_text_output(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _insert_task(pkg, "tk_status", "Build a compact menu-bar status app.", rss_mb=100)

    txt = _tool(pkg, "agent_status")(json_output=False, refresh=False)
    assert "loops enabled=" in txt
    assert "Candidate reviewer" in txt
    assert "agents=1" in txt
    assert "rss_total=100MB" in txt
    assert "desc=" in txt
    assert "Build a compact menu-bar status app." in txt


def test_agent_status_ready_only_when_due(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    pkg["config"].CANDIDATE_REVIEW_INTERVAL_S = 3600
    pkg["config"].CANDIDATE_REVIEW_MIN = 3
    conn = pkg["db"].get_db()
    now = int(time.time())
    for i in range(3):
        conn.execute(
            "INSERT INTO extract_candidates "
            "(kind, content, status, created_at) VALUES "
            "('note', ?, 'pending', ?)",
            (f"candidate {i}", now),
        )
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES ('s', 'candidate_review_pass', ?, 'below_threshold n=1', ?)",
        (str(now), now),
    )
    conn.commit()

    from threadkeeper.agent_status import agent_status_snapshot

    snap = agent_status_snapshot(refresh=False)
    loop = {l["id"]: l for l in snap["loops"]}["candidate_reviewer"]
    assert loop["backlog_count"] == 3
    assert loop["status"] == "idle"
