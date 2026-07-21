from __future__ import annotations

import json
import os
import time


_FAKE_CID = "33334444-5555-6666-7777-888899990000"


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _txt(res):
    """Text payload from a tool result (str or CallToolResult, #67)."""
    if isinstance(res, str):
        return res
    return "\n".join(
        c.text for c in res.content if getattr(c, "type", None) == "text"
    )


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
    assert "auto_update" in by_loop
    assert "daily updates" in by_loop["auto_update"]["description"]
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


def test_agent_status_evolve_applier_ready_when_promoted_queue_exists(
    mp_with_cid, monkeypatch,
):
    pkg = mp_with_cid(_FAKE_CID)
    pkg["config"].EVOLVE_APPLY_INTERVAL_S = 604800
    import threadkeeper.agent_status as status_mod
    import threadkeeper.evolve_applier as applier_mod

    status_mod._CONFLICTED_PR_CACHE.update({"at": 0, "count": 0})
    status_mod._ISSUE_BACKLOG_CACHE.update({"at": 0, "count": 0})
    monkeypatch.setattr(
        applier_mod, "_conflicted_applier_prs",
        lambda repo_root=None: ([], ""),
    )
    monkeypatch.setattr(
        applier_mod, "_fetch_open_issues",
        lambda repo_root=None: ([], ""),
    )
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
    mp_with_cid, monkeypatch,
):
    pkg = mp_with_cid(_FAKE_CID)
    pkg["config"].EVOLVE_APPLY_INTERVAL_S = 604800
    import threadkeeper.agent_status as status_mod
    import threadkeeper.evolve_applier as applier_mod

    status_mod._CONFLICTED_PR_CACHE.update({"at": 0, "count": 0})
    status_mod._ISSUE_BACKLOG_CACHE.update({"at": 0, "count": 0})
    monkeypatch.setattr(
        applier_mod, "_conflicted_applier_prs",
        lambda repo_root=None: ([], ""),
    )
    monkeypatch.setattr(
        applier_mod, "_fetch_open_issues",
        lambda repo_root=None: ([], ""),
    )
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


def test_agent_status_evolve_applier_ready_when_roadmap_issue_exists(
    mp_with_cid, monkeypatch,
):
    pkg = mp_with_cid(_FAKE_CID)
    pkg["config"].EVOLVE_APPLY_INTERVAL_S = 604800
    import threadkeeper.agent_status as status_mod
    import threadkeeper.evolve_applier as applier_mod

    status_mod._ISSUE_BACKLOG_CACHE.update({"at": 0, "count": 0})
    status_mod._CONFLICTED_PR_CACHE.update({"at": 0, "count": 0})
    monkeypatch.setattr(
        applier_mod, "_conflicted_applier_prs",
        lambda repo_root=None: ([], ""),
    )
    monkeypatch.setattr(
        applier_mod, "_fetch_open_issues",
        lambda repo_root=None: (
            [{
                "number": 6,
                "title": "Telemetry dashboard",
                "labels": [{"name": "roadmap"}],
                "body": "Need counters",
                "url": "https://github.com/o/r/issues/6",
                "authorAssociation": "OWNER",
            }],
            "",
        ),
    )
    monkeypatch.setattr(
        applier_mod, "_fetch_issue_comments",
        lambda issue_number, repo_root=None: ([], ""),
    )

    from threadkeeper.agent_status import agent_status_snapshot

    snap = agent_status_snapshot(refresh=False)
    loop = {l["id"]: l for l in snap["loops"]}["evolve_apply"]
    assert loop["backlog_count"] == 1
    assert loop["status"] == "ready"


def test_agent_status_evolve_applier_ready_when_conflicted_pr_exists(
    mp_with_cid, monkeypatch,
):
    pkg = mp_with_cid(_FAKE_CID)
    pkg["config"].EVOLVE_APPLY_INTERVAL_S = 604800
    import threadkeeper.agent_status as status_mod
    import threadkeeper.evolve_applier as applier_mod

    status_mod._CONFLICTED_PR_CACHE.update({"at": 0, "count": 0})
    status_mod._ISSUE_BACKLOG_CACHE.update({"at": 0, "count": 0})
    monkeypatch.setattr(
        applier_mod, "_conflicted_applier_prs",
        lambda repo_root=None: (
            [{
                "number": 44,
                "title": "Conflicted PR",
                "headRefName": "roadmap/issue-44-conflict-aaaaaa",
                "mergeStateStatus": "DIRTY",
                "mergeable": "CONFLICTING",
            }],
            "",
        ),
    )
    monkeypatch.setattr(
        applier_mod, "_fetch_open_issues",
        lambda repo_root=None: ([], ""),
    )

    from threadkeeper.agent_status import agent_status_snapshot

    snap = agent_status_snapshot(refresh=False)
    loop = {l["id"]: l for l in snap["loops"]}["evolve_apply"]
    assert loop["backlog_count"] == 1
    assert loop["backlog_label"] == "apply work items"
    assert loop["status"] == "ready"


def test_agent_status_reports_github_budget_cooldown(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    conn = pkg["db"].get_db()
    import threadkeeper.github_budget as gb
    now = int(time.time())
    conn.execute(
        "INSERT INTO github_rate_budget "
        "(account, remaining, reset_at, cooldown_until, backoff_attempts, "
        "last_status, last_reason, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            gb.github_account_key(),
            0,
            now + 600,
            now + 300,
            2,
            403,
            "secondary_rate_limit",
            now,
        ),
    )
    conn.commit()

    from threadkeeper.agent_status import agent_status_snapshot, format_agent_status

    snap = agent_status_snapshot(refresh=False)

    assert snap["github_budget"]["cooldown_active"] is True
    assert snap["github_budget"]["cooldown_left_s"] > 0
    text = format_agent_status(snap)
    assert "github_budget=cooldown" in text
    assert "secondary_rate_limit" in text


def test_agent_status_evolve_reviewer_ready_when_due_without_legacy_backlog(
    mp_with_cid,
):
    pkg = mp_with_cid(_FAKE_CID)
    pkg["config"].EVOLVE_REVIEW_INTERVAL_S = 604800

    from threadkeeper.agent_status import agent_status_snapshot

    snap = agent_status_snapshot(refresh=False)
    loop = {l["id"]: l for l in snap["loops"]}["evolve_review"]
    assert loop["backlog_count"] == 0
    assert loop["backlog_label"] == "legacy pending suggestions"
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

    raw = _txt(_tool(pkg, "agent_status")(json_output=True, refresh=False))
    data = json.loads(raw)
    assert data["running_count"] == 1
    assert "loops" in data
    assert data["agents"][0]["task_id"] == "tk_status"
    assert data["agents"][0]["rss_mb"] == 100


def test_agent_memory_cleanup_runs_guard_and_orphan_cleanup(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import agent_status, memory_guard, process_health

    _insert_task(pkg, "tk_status", "Build a compact menu-bar status app.", rss_mb=100)
    calls = []
    monkeypatch.setattr(agent_status, "_refresh_rss", lambda conn: calls.append("refresh"))
    monkeypatch.setattr(
        memory_guard,
        "request_reclaim",
        lambda reason: calls.append(("trim", reason)) or {
            "requested": [11, 22],
            "count": 2,
            "reason": reason,
        },
    )
    monkeypatch.setattr(
        memory_guard,
        "check_once",
        lambda dry_run, notify: calls.append(("guard", dry_run, notify)) or {
            "warn": [{}],
            "kill": [{}],
            "killed": [33],
            "retired": [44],
            "failed": [],
            "aggregate": {"warn": True, "rss_mb": 2048},
            "reclaim_requests": {"count": 1, "requested": [55]},
            "local_reclaim": {
                "before_mb": 1500,
                "after_mb": 900,
                "freed_mb": 600,
            },
            "handled_controls": [],
        },
    )
    monkeypatch.setattr(
        process_health,
        "cleanup",
        lambda dry_run, force: calls.append(("orphans", dry_run, force)) or {
            "orphans": [{"pid": 66}],
            "killed": [66],
            "failed": [],
        },
    )

    result = agent_status.memory_cleanup(dry_run=False, force=True)
    text = agent_status.format_memory_cleanup(result)

    assert result["peer_trim_requested"]["count"] == 2
    assert result["guard"]["killed"] == [33]
    assert result["guard"]["retired"] == [44]
    assert result["orphans"]["killed"] == [66]
    assert ("guard", False, False) in calls
    assert ("orphans", False, True) in calls
    assert "local_reclaim before=1500MB after=900MB freed=600MB" in text


def test_agent_memory_cleanup_dry_run_does_not_request_peer_trim(
    mp_with_cid,
    monkeypatch,
):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import agent_status, memory_guard, process_health

    calls = []
    monkeypatch.setattr(agent_status, "_refresh_rss", lambda conn: None)
    monkeypatch.setattr(
        memory_guard,
        "request_reclaim",
        lambda reason: calls.append(("trim", reason)) or {
            "requested": [11],
            "count": 1,
            "reason": reason,
        },
    )
    monkeypatch.setattr(
        memory_guard,
        "check_once",
        lambda dry_run, notify: {
            "warn": [],
            "kill": [],
            "killed": [],
            "retired": [],
            "failed": [],
            "aggregate": {},
            "reclaim_requests": {},
            "local_reclaim": None,
            "handled_controls": [],
        },
    )
    monkeypatch.setattr(
        process_health,
        "cleanup",
        lambda dry_run, force: {"orphans": [], "killed": [], "failed": []},
    )

    result = agent_status.memory_cleanup(dry_run=True)

    assert result["dry_run"] is True
    assert result["peer_trim_requested"]["count"] == 0
    assert calls == []


def test_agent_memory_cleanup_tool_json_output(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import agent_status as agent_status_mod

    monkeypatch.setattr(
        "threadkeeper.tools.agent_status.memory_cleanup",
        lambda dry_run, force: {
            "dry_run": dry_run,
            "force": force,
            "before": {"running_count": 0, "child_rss_mb": 0},
            "after": {"running_count": 0, "child_rss_mb": 0},
            "peer_trim_requested": {"requested": [], "count": 0},
            "guard": {
                "warn": 0,
                "kill": 0,
                "killed": [],
                "retired": [],
                "failed": [],
                "local_reclaim": None,
            },
            "orphans": {"count": 0, "killed": [], "failed": []},
        },
    )

    raw = _tool(pkg, "agent_memory_cleanup")(
        json_output=True,
        dry_run=True,
        force=True,
    )
    data = json.loads(raw)

    assert data["dry_run"] is True
    assert data["force"] is True
    assert agent_status_mod.format_memory_cleanup(data).startswith("dry_run:")


def test_agent_status_text_output(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _insert_task(pkg, "tk_status", "Build a compact menu-bar status app.", rss_mb=100)

    txt = _txt(_tool(pkg, "agent_status")(json_output=False, refresh=False))
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


def test_is_result_line_ignores_prompt_echo_lines(mp_with_cid):
    mp_with_cid(_FAKE_CID)
    from threadkeeper.agent_status import _is_result_line

    # Prompt formatting echoed into the child log must never surface as a
    # completed result (this exact line reached menu-bar notifications live).
    assert not _is_result_line(
        "d. Output `MATERIALIZED: <slug-or-skill>` on success."
    )
    assert not _is_result_line(
        "   4. Output `MATERIALIZED: <slug-or-skill>` on success."
    )
    assert not _is_result_line(
        "After any materialization, call mark_skill_materialized("
        "thread_id, skill_path_or_lessons_md) to close the loop."
    )
    # Genuine child verdicts still surface.
    assert _is_result_line("MATERIALIZED: cross-language-matching-lessons")
    assert _is_result_line("Processed 2 candidates into durable notes.")
    assert _is_result_line(
        "Materialized skill e2e-fresh-install-per-suite with a new section."
    )


def _insert_failed_task(pkg, task_id, prompt, log_text, role="curator"):
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at, ended_at, return_code, role, chosen_cli, rss_kb, "
        "rss_updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id, os.getpid(), _FAKE_CID, f"child-{task_id}", "/tmp",
            prompt, now - 30, now - 5, 1, role, "claude", 0, now,
        ),
    )
    pkg["config"].TASK_LOG_DIR.mkdir(parents=True, exist_ok=True)
    (pkg["config"].TASK_LOG_DIR / f"{task_id}.log").write_text(log_text)
    conn.commit()


def test_recent_failures_surfaces_dead_child_with_reason(mp_with_cid, monkeypatch):
    # A spawned child that died non-zero (subscription-exhausted-mid-run) must
    # surface in recent_failures with the log-tail reason and notify=True when
    # notifications are enabled (NOTIFY_POLL_S>0, NOTIFY_LOOP_FAILURE default on).
    monkeypatch.setenv("THREADKEEPER_NOTIFY_POLL_S", "30")
    pkg = mp_with_cid(_FAKE_CID)
    _insert_failed_task(
        pkg,
        "deadchild",
        "You are the CURATOR for thread-keeper.\n\nCurate memory.",
        "starting run\nCredit balance is too low to run this model\n",
    )
    from threadkeeper.agent_status import agent_status_snapshot

    snap = agent_status_snapshot(refresh=False)
    fails = {f["task_id"]: f for f in snap["recent_failures"]}
    assert "deadchild" in fails
    item = fails["deadchild"]
    assert item["kind"] == "failure"
    assert item["notify"] is True
    assert "credit" in item["summary"].lower()
    # A live failure never leaks into the positive recent_results feed.
    assert all(r["task_id"] != "deadchild" for r in snap["recent_results"])


def test_recent_failures_notify_flag_off_when_toggle_disabled(
    mp_with_cid, monkeypatch
):
    monkeypatch.setenv("THREADKEEPER_NOTIFY_POLL_S", "30")     # master gate on
    monkeypatch.setenv("THREADKEEPER_NOTIFY_LOOP_FAILURE", "false")
    pkg = mp_with_cid(_FAKE_CID)  # factory re-imports fresh → reads the env
    _insert_failed_task(
        pkg,
        "deadchild2",
        "You are the CURATOR for thread-keeper.\n\nCurate memory.",
        "boom\nspawn_error token_budget_exceeded\n",
    )
    from threadkeeper.agent_status import agent_status_snapshot

    snap = agent_status_snapshot(refresh=False)
    fails = {f["task_id"]: f for f in snap["recent_failures"]}
    assert "deadchild2" in fails                 # still listed in the menu
    assert fails["deadchild2"]["notify"] is False  # but no banner posted
