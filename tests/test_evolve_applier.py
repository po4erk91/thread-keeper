"""Evolve applier — unit tests.

The applier IMPLEMENTS a promoted brief-format suggestion via a spawned child
that edits brief.py, adds a golden test, runs the suite, and opens a PR. These
tests exercise the pure dispatch logic with spawn() monkeypatched — no real
child is launched, no real PR is opened. The end-to-end PR path is exercised
separately by actually running the role.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


_FAKE_CID = "aaaa1111-2222-3333-4444-555566667777"


def _bootstrap(tmp_path, monkeypatch, interval="0"):
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
        "THREADKEEPER_CURATOR_REPORTS_DIR": str(tmp_path / "curator"),
        "THREADKEEPER_EXTRACT_INTERVAL_S": "0",
        "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_PROBE_INTERVAL_S": "0",
        "THREADKEEPER_EVOLVE_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_EVOLVE_APPLY_INTERVAL_S": interval,
        "THREADKEEPER_DISABLE_BG_DAEMONS": "1",
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
    from threadkeeper import _mcp, db, evolve_applier, identity
    monkeypatch.setattr(
        evolve_applier, "_fetch_open_issues",
        lambda repo_root=None: ([], ""),
    )
    monkeypatch.setattr(
        evolve_applier, "_fetch_issue_comments",
        lambda issue_number, repo_root=None: ([], ""),
    )
    monkeypatch.setattr(
        evolve_applier, "_comment_issue_claim",
        lambda issue, repo_root=None: ("https://x/issues/0#issuecomment-1", ""),
    )
    monkeypatch.setattr(
        evolve_applier, "_open_prs_for_issue",
        lambda issue_number, repo_root=None: ([], ""),
    )
    # Note: _resolve_claim_race is NOT monkeypatched here so the new
    # multi-host tests can exercise the real implementation. With the default
    # _fetch_issue_comments returning [], the race resolver sees ≤1 active
    # claim and returns (True, "") — existing tests behave the same.
    monkeypatch.setattr(
        evolve_applier, "_delete_issue_comment",
        lambda comment_url, repo_root=None: "",
    )
    # Skip the real-time race-detection sleep in unit tests so the suite stays
    # snappy. The bootstrap defaults already make the race resolver return True
    # in the "no competing claim" common path.
    import threadkeeper.config as _cfg
    monkeypatch.setattr(_cfg, "ROADMAP_CLAIM_RACE_WINDOW_S", 0.0)
    monkeypatch.setattr(evolve_applier, "ROADMAP_CLAIM_RACE_WINDOW_S", 0.0)
    return {"mcp": _mcp.mcp, "db": db, "ea": evolve_applier, "identity": identity}


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _add_evolve(conn, suggestion, rationale=None, applied=0, status="pending",
                created_at=None):
    conn.execute(
        "INSERT INTO evolve (suggestion, rationale, applied, status, created_at) "
        "VALUES (?,?,?,?,?)",
        (suggestion, rationale, applied, status,
         created_at if created_at is not None else int(time.time())),
    )
    conn.commit()
    return conn.execute("SELECT MAX(id) AS id FROM evolve").fetchone()["id"]


def _mock_spawn(monkeypatch, calls):
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(
        spawn_mod, "spawn",
        lambda **kw: calls.update(kw)
        or "ok task=tk_ap pid=1 child_cid=abcd1234 parent_cid=ef567890",
    )


def _write_report(pkg, name="REPORT-20260611T120000.md", complete=True):
    import threadkeeper.config as cfg

    cfg.CURATOR_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = cfg.CURATOR_REPORTS_DIR / name
    tail = "\nCURATOR_PASS_COMPLETE\n" if complete else ""
    path.write_text(
        "# Curator report\n\nPATCH: stale-skill\n  reason: compact it\n" + tail,
        encoding="utf-8",
    )
    return path


def _issue(number, title, labels=("roadmap",), body="Issue body"):
    return {
        "number": number,
        "title": title,
        "labels": [{"name": name} for name in labels],
        "body": body,
        "url": f"https://github.com/o/r/issues/{number}",
    }


def _claim_comment(created_at="2026-06-14T12:00:00Z"):
    return {
        "body": "<!-- thread-keeper:evolve-applier-claim -->\nclaimed",
        "createdAt": created_at,
    }


# ── apply_evolve rejects bad / non-actionable ids ──────────────────────────

def test_apply_evolve_missing_id(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    assert pkg["ea"].apply_evolve(9999).startswith("ERR evolve_not_found")


def test_apply_evolve_rejects_non_promoted(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    pend = _add_evolve(conn, "still pending", status="pending")
    dism = _add_evolve(conn, "dismissed one", status="dismissed")
    done = _add_evolve(conn, "already applied", status="promoted", applied=1)
    for eid in (pend, dism, done):
        out = pkg["ea"].apply_evolve(eid)
        assert out.startswith("ERR not_actionable"), out


# ── apply_evolve builds the correct spawn() call ───────────────────────────

def test_apply_evolve_builds_spawn_call(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    eid = _add_evolve(
        conn, "add a failed_paths field per thread",
        rationale="kind=failed notes are flat, not surfaced", status="promoted",
    )
    calls = {}
    _mock_spawn(monkeypatch, calls)
    out = pkg["ea"].apply_evolve(eid)
    assert out.startswith(f"spawned evolve_id={eid}"), out

    # role + routing + autonomy posture
    assert calls["role"] == "evolve_applier"
    assert calls["write_origin"] == "evolve_apply"
    assert calls["permission_mode"] == "bypassPermissions"
    assert calls["visible"] is False
    assert calls["cwd"] == str(pkg["ea"]._repo_root())

    # the child can edit code, run the suite/gh, and report the PR back
    tools = calls["extra_allowed_tools"]
    assert "Bash" in tools and "Edit" in tools and "Write" in tools
    assert "evolve_mark_applied" in tools

    # prompt carries the suggestion + rationale + the required workflow
    p = calls["prompt"]
    assert "add a failed_paths field per thread" in p
    assert "kind=failed notes are flat" in p
    assert "threadkeeper/brief.py" in p and "render_brief" in p
    assert "pytest -q" in p
    assert "gh pr create" in p
    assert 'git commit -m "<type>: <short imperative summary>"' in p
    assert 'gh pr create --title "<type>: <short>"' in p
    assert 'git commit -m "evolve:' not in p
    assert 'gh pr create --title "evolve:' not in p
    assert "evolve_mark_applied" in p
    assert "NEVER" in p and "main" in p  # the no-touch-main guard
    # the slim-child sets NO_EMBEDDINGS=1, which breaks the embedding tests in
    # the full suite — the prompt must tell the child to unset it for pytest
    assert "THREADKEEPER_NO_EMBEDDINGS" in p

    # applied is NOT set just by launching — only after a real PR
    assert conn.execute(
        "SELECT applied FROM evolve WHERE id=?", (eid,)
    ).fetchone()["applied"] == 0


def test_apply_curator_report_builds_evolve_applier_spawn(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    report = _write_report(pkg)
    calls = {}
    _mock_spawn(monkeypatch, calls)

    out = pkg["ea"].apply_curator_report(str(report))

    assert out.startswith("spawned curator_report=REPORT-"), out
    assert calls["role"] == "evolve_applier"
    assert calls["write_origin"] == "evolve_apply"
    assert calls["permission_mode"] == "auto"
    assert calls["visible"] is False
    assert calls["cwd"] == str(pkg["ea"]._repo_root())
    assert "Do not open a thread" in calls["append_system"]
    tools = calls["extra_allowed_tools"]
    assert "lesson_remove" in tools
    assert "lesson_append" in tools
    assert "skill_manage" in tools
    assert "evolve_mark_curator_report_applied" in tools
    assert "Bash" not in tools and "Edit" not in tools

    prompt = calls["prompt"]
    assert "Curator REPORT" in prompt
    assert str(report.resolve()) in prompt
    assert "PATCH: stale-skill" in prompt
    assert "Do NOT call brief()" in prompt
    assert "NEVER touch entries marked [PROTECTED]" in prompt
    assert "Do not use Bash" in prompt
    assert "gh pr create" not in prompt


def test_apply_curator_report_requires_complete_unapplied_report(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    incomplete = _write_report(pkg, complete=False)
    assert (
        pkg["ea"].apply_curator_report(str(incomplete))
        == f"ERR report_incomplete={incomplete.name}"
    )
    complete = _write_report(pkg, name="REPORT-20260611T130000.md")
    out = pkg["ea"].mark_curator_report_applied(
        pkg["db"].get_db(), str(complete), "already handled"
    )
    assert "applied=1" in out
    assert (
        pkg["ea"].apply_curator_report(str(complete))
        == f"ERR report_already_applied={complete.name}"
    )


def test_mark_curator_report_applied_tool_records_idempotency_event(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    report = _write_report(pkg)
    tool = _tool(pkg, "evolve_mark_curator_report_applied")

    out = tool(report_path=str(report), summary="patched=1 skipped=2")

    assert out == f"ok report={report.name} applied=1"
    conn = pkg["db"].get_db()
    row = conn.execute(
        "SELECT target, summary FROM events "
        "WHERE kind='curator_report_applied'"
    ).fetchone()
    assert row["target"] == str(report.resolve())
    assert row["summary"] == "patched=1 skipped=2"
    assert tool(report_path=str(report), summary="again").endswith(
        "already_applied=1"
    )


def test_evolve_apply_status_includes_curator_completion_events(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="604800")
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

    out = _tool(pkg, "evolve_apply_status")()
    assert "recent apply events" in out
    assert "curator_report_applied: Applied 4 lesson consolidations" in out
    assert out.index("curator_report_applied") < out.index("evolve_apply_pass")


def test_open_roadmap_issues_prioritizes_roadmap_and_skips_applied(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    monkeypatch.setattr(
        pkg["ea"], "_fetch_open_issues",
        lambda repo_root=None: (
            [
                _issue(3, "adapter fallback", labels=("enhancement",)),
                _issue(2, "hot config", labels=("roadmap", "enhancement")),
                _issue(1, "ingest verification", labels=("roadmap",)),
            ],
            "",
        ),
    )
    pkg["ea"].mark_roadmap_issue_applied(
        conn, 1, "https://github.com/o/r/pull/10"
    )

    issues, err = pkg["ea"]._open_roadmap_issues(conn)

    assert err == ""
    assert [int(i["number"]) for i in issues] == [2, 3]


def test_open_roadmap_issues_skips_active_issue_claim(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    monkeypatch.setattr(
        pkg["ea"], "_fetch_open_issues",
        lambda repo_root=None: (
            [
                _issue(1, "claimed issue"),
                _issue(2, "free issue"),
            ],
            "",
        ),
    )

    def _comments(issue_number, repo_root=None):
        if int(issue_number) == 1:
            return ([_claim_comment()], "")
        return ([], "")

    monkeypatch.setattr(pkg["ea"], "_fetch_issue_comments", _comments)
    monkeypatch.setattr(pkg["ea"].time, "time", lambda: 1781438400.0)

    issues, err = pkg["ea"]._open_roadmap_issues(conn)

    assert err == ""
    assert [int(i["number"]) for i in issues] == [2]


def test_open_roadmap_issues_allows_stale_issue_claim(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    monkeypatch.setattr(
        pkg["ea"], "_fetch_open_issues",
        lambda repo_root=None: ([_issue(1, "stale claim")], ""),
    )
    monkeypatch.setattr(
        pkg["ea"], "_fetch_issue_comments",
        lambda issue_number, repo_root=None: (
            [_claim_comment("2026-06-12T12:00:00Z")],
            "",
        ),
    )
    monkeypatch.setattr(pkg["ea"].time, "time", lambda: 1781438400.0)

    issues, err = pkg["ea"]._open_roadmap_issues(conn)

    assert err == ""
    assert [int(i["number"]) for i in issues] == [1]


def test_apply_roadmap_issue_builds_evolve_applier_spawn(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pkg["ea"], "_fetch_open_issues",
        lambda repo_root=None: (
            [_issue(6, "Telemetry dashboard", body="Need 24h counters")],
            "",
        ),
    )
    calls = {}
    _mock_spawn(monkeypatch, calls)

    out = pkg["ea"].apply_roadmap_issue()

    assert out.startswith("spawned roadmap_issue=#6"), out
    assert calls["role"] == "evolve_applier"
    assert calls["write_origin"] == "evolve_apply"
    assert calls["permission_mode"] == "bypassPermissions"
    assert calls["cwd"] == str(pkg["ea"]._repo_root())
    tools = calls["extra_allowed_tools"]
    assert "Bash" in tools and "Edit" in tools and "Write" in tools
    assert "evolve_mark_roadmap_issue_applied" in tools
    prompt = calls["prompt"]
    assert "ISSUE #6: Telemetry dashboard" in prompt
    assert "Need 24h counters" in prompt
    assert "Closes #6" in prompt
    assert "Implement one issue only" in prompt
    assert "evolve_mark_roadmap_issue_applied" in prompt
    assert "THREADKEEPER_NO_EMBEDDINGS" in prompt
    assert "<!-- thread-keeper:evolve-applier-claim -->" in prompt


def test_apply_roadmap_issue_comments_before_spawn(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pkg["ea"], "_fetch_open_issues",
        lambda repo_root=None: ([_issue(6, "Telemetry dashboard")], ""),
    )
    order = []

    def _claim(issue, repo_root=None):
        order.append(f"claim#{int(issue['number'])}")
        return (
            f"https://x/issues/{int(issue['number'])}#issuecomment-99",
            "",
        )

    def _spawn(**kw):
        order.append("spawn")
        return "ok task=tk_ap pid=1 child_cid=abcd parent_cid=ef"

    monkeypatch.setattr(pkg["ea"], "_comment_issue_claim", _claim)
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", _spawn)

    out = pkg["ea"].apply_roadmap_issue()

    assert out.startswith("spawned roadmap_issue=#6"), out
    assert order == ["claim#6", "spawn"]


def test_apply_roadmap_issue_queue_reports_no_startable_when_claim_fails(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pkg["ea"], "_fetch_open_issues",
        lambda repo_root=None: ([_issue(6, "Telemetry dashboard")], ""),
    )
    monkeypatch.setattr(
        pkg["ea"], "_comment_issue_claim",
        lambda issue, repo_root=None: ("", "gh_issue_comment_failed: denied"),
    )

    def _boom(**kw):
        raise AssertionError("must not spawn without an issue claim")

    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", _boom)

    out = pkg["ea"].apply_roadmap_issue()

    assert out.startswith("no_roadmap_issue_startable"), out
    assert "ERR roadmap_issue_claim_failed=#6" in out


def test_apply_roadmap_issue_queue_tries_next_when_claim_fails(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pkg["ea"], "_fetch_open_issues",
        lambda repo_root=None: (
            [_issue(1, "Blocked issue"), _issue(2, "Startable issue")],
            "",
        ),
    )
    claimed = []

    def _claim(issue, repo_root=None):
        num = int(issue["number"])
        claimed.append(num)
        if num == 1:
            return "", "gh_issue_comment_failed: locked"
        return f"https://x/issues/{num}#issuecomment-{num}", ""

    monkeypatch.setattr(pkg["ea"], "_comment_issue_claim", _claim)
    calls = {}
    _mock_spawn(monkeypatch, calls)

    out = pkg["ea"].apply_roadmap_issue()

    assert out.startswith("spawned roadmap_issue=#2"), out
    assert "after_skipping=1" in out
    assert claimed == [1, 2]
    assert "ISSUE #2: Startable issue" in calls["prompt"]


def test_apply_roadmap_issue_exact_issue_does_not_switch_tasks(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pkg["ea"], "_fetch_open_issues",
        lambda repo_root=None: (
            [_issue(1, "Blocked issue"), _issue(2, "Startable issue")],
            "",
        ),
    )
    monkeypatch.setattr(
        pkg["ea"], "_comment_issue_claim",
        lambda issue, repo_root=None: ("", "gh_issue_comment_failed: locked"),
    )

    def _boom(**kw):
        raise AssertionError("exact issue mode must not spawn another issue")

    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", _boom)

    out = pkg["ea"].apply_roadmap_issue(issue_number=1)

    assert out.startswith("ERR roadmap_issue_claim_failed=#1"), out


def test_apply_roadmap_issue_aborts_when_issue_already_claimed(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pkg["ea"], "_fetch_open_issues",
        lambda repo_root=None: ([_issue(6, "Telemetry dashboard")], ""),
    )
    monkeypatch.setattr(
        pkg["ea"], "_fetch_issue_comments",
        lambda issue_number, repo_root=None: ([_claim_comment()], ""),
    )
    monkeypatch.setattr(pkg["ea"].time, "time", lambda: 1781438400.0)

    def _boom(**kw):
        raise AssertionError("must not spawn for an already claimed issue")

    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", _boom)

    out = pkg["ea"].apply_roadmap_issue(issue_number=6)

    assert out == "ERR roadmap_issue_claimed=6"


def test_mark_roadmap_issue_applied_tool_requires_pr_url(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    tool = _tool(pkg, "evolve_mark_roadmap_issue_applied")
    assert tool(issue_number=6, pr_url="").startswith("ERR pr_url_required")
    assert "applied=1" in tool(
        issue_number=6, pr_url="https://github.com/o/r/pull/6"
    )
    conn = pkg["db"].get_db()
    row = conn.execute(
        "SELECT target, summary FROM events WHERE kind='roadmap_issue_applied'"
    ).fetchone()
    assert row["target"] == "6"
    assert row["summary"] == "https://github.com/o/r/pull/6"


# ── multi-host: cross-machine conflict guards ──────────────────────────────

def test_apply_roadmap_issue_skips_when_open_pr_already_closes_it(
    tmp_path, monkeypatch,
):
    """If another host (or a prior crashed applier) already opened a PR for
    this issue, do NOT spawn or claim — fall through to the next candidate."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pkg["ea"], "_fetch_open_issues",
        lambda repo_root=None: (
            [_issue(6, "Telemetry dashboard"), _issue(7, "Free issue")],
            "",
        ),
    )
    monkeypatch.setattr(
        pkg["ea"], "_open_prs_for_issue",
        lambda issue_number, repo_root=None: (
            [{"url": "https://github.com/o/r/pull/42",
              "number": 42}] if int(issue_number) == 6 else [],
            "",
        ),
    )
    claimed = []

    def _claim(issue, repo_root=None):
        num = int(issue["number"])
        claimed.append(num)
        return f"https://x/issues/{num}#issuecomment-{num}", ""

    monkeypatch.setattr(pkg["ea"], "_comment_issue_claim", _claim)
    calls = {}
    _mock_spawn(monkeypatch, calls)

    out = pkg["ea"].apply_roadmap_issue()

    # advanced past #6 (open PR) to #7
    assert out.startswith("spawned roadmap_issue=#7"), out
    # claim was NOT posted for #6 — the open-PR check ran before claim
    assert claimed == [7]


def test_apply_roadmap_issue_exact_mode_returns_open_pr_error(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pkg["ea"], "_fetch_open_issues",
        lambda repo_root=None: ([_issue(6, "Telemetry dashboard")], ""),
    )
    monkeypatch.setattr(
        pkg["ea"], "_open_prs_for_issue",
        lambda issue_number, repo_root=None: (
            [{"url": "https://github.com/o/r/pull/42"}], "",
        ),
    )

    def _claim(issue, repo_root=None):
        raise AssertionError("must not claim when an open PR already exists")

    monkeypatch.setattr(pkg["ea"], "_comment_issue_claim", _claim)

    def _boom(**kw):
        raise AssertionError("must not spawn when an open PR already exists")

    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", _boom)

    out = pkg["ea"].apply_roadmap_issue(issue_number=6)

    assert out.startswith("ERR roadmap_issue_open_pr=#6"), out
    assert "pull/42" in out


def test_apply_roadmap_issue_retracts_claim_on_lost_race(
    tmp_path, monkeypatch,
):
    """TOCTOU: after we post our claim, a competing host's earlier claim is
    visible. We retract our own claim and let the queue advance."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pkg["ea"], "_fetch_open_issues",
        lambda repo_root=None: (
            [_issue(6, "Telemetry dashboard"), _issue(7, "Other issue")],
            "",
        ),
    )

    def _claim(issue, repo_root=None):
        return (
            f"https://x/issues/{int(issue['number'])}#issuecomment-mine",
            "",
        )

    monkeypatch.setattr(pkg["ea"], "_comment_issue_claim", _claim)

    def _race(issue_number, my_comment_url, repo_root=None):
        if int(issue_number) == 6:
            return False, ""  # lost
        return True, ""

    monkeypatch.setattr(pkg["ea"], "_resolve_claim_race", _race)
    calls = {}
    _mock_spawn(monkeypatch, calls)

    out = pkg["ea"].apply_roadmap_issue()

    assert out.startswith("spawned roadmap_issue=#7"), out
    assert "ISSUE #7: Other issue" in calls["prompt"]


def test_apply_roadmap_issue_retracts_claim_on_spawn_failure(
    tmp_path, monkeypatch,
):
    """If spawn() raises after we posted our claim, retract the claim so the
    next pass can retry the issue immediately instead of waiting 24h TTL."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pkg["ea"], "_fetch_open_issues",
        lambda repo_root=None: ([_issue(6, "Telemetry dashboard")], ""),
    )
    monkeypatch.setattr(
        pkg["ea"], "_comment_issue_claim",
        lambda issue, repo_root=None: (
            "https://x/issues/6#issuecomment-mine", "",
        ),
    )

    deleted = []
    monkeypatch.setattr(
        pkg["ea"], "_delete_issue_comment",
        lambda comment_url, repo_root=None: (
            deleted.append(comment_url) or ""
        ),
    )

    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(
        spawn_mod, "spawn",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("spawn rejected")),
    )

    out = pkg["ea"].apply_roadmap_issue(issue_number=6)

    assert out.startswith("spawn_error issue=#6"), out
    assert "spawn rejected" in out
    assert deleted == ["https://x/issues/6#issuecomment-mine"]


def test_resolve_claim_race_wins_when_oldest_active_claim_is_ours(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pkg["ea"], "_fetch_issue_comments",
        lambda issue_number, repo_root=None: (
            [
                {
                    "body": "<!-- thread-keeper:evolve-applier-claim -->\nmine",
                    "url": "https://x/issues/6#issuecomment-100",
                    "createdAt": "2026-06-14T12:00:00Z",
                },
                {
                    "body": "<!-- thread-keeper:evolve-applier-claim -->\nthem",
                    "url": "https://x/issues/6#issuecomment-200",
                    "createdAt": "2026-06-14T12:00:03Z",
                },
            ],
            "",
        ),
    )
    monkeypatch.setattr(pkg["ea"].time, "time", lambda: 1781438400.0)
    monkeypatch.setattr(pkg["ea"].time, "sleep", lambda _s: None)

    won, err = pkg["ea"]._resolve_claim_race(
        6, "https://x/issues/6#issuecomment-100",
    )
    assert err == ""
    assert won is True


def test_resolve_claim_race_loses_and_deletes_own_claim(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pkg["ea"], "_fetch_issue_comments",
        lambda issue_number, repo_root=None: (
            [
                {
                    "body": "<!-- thread-keeper:evolve-applier-claim -->\nthem",
                    "url": "https://x/issues/6#issuecomment-100",
                    "createdAt": "2026-06-14T12:00:00Z",
                },
                {
                    "body": "<!-- thread-keeper:evolve-applier-claim -->\nmine",
                    "url": "https://x/issues/6#issuecomment-200",
                    "createdAt": "2026-06-14T12:00:03Z",
                },
            ],
            "",
        ),
    )
    monkeypatch.setattr(pkg["ea"].time, "time", lambda: 1781438400.0)
    monkeypatch.setattr(pkg["ea"].time, "sleep", lambda _s: None)

    deleted = []
    monkeypatch.setattr(
        pkg["ea"], "_delete_issue_comment",
        lambda url, repo_root=None: (deleted.append(url) or ""),
    )

    won, err = pkg["ea"]._resolve_claim_race(
        6, "https://x/issues/6#issuecomment-200",
    )
    assert err == ""
    assert won is False
    assert deleted == ["https://x/issues/6#issuecomment-200"]


def test_claim_body_includes_host_pid_git_rev(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    issue = _issue(42, "Cross-host check")
    body = pkg["ea"]._roadmap_issue_claim_body(issue, now_t=1781438400.0)
    assert pkg["ea"].ROADMAP_ISSUE_CLAIM_MARKER in body
    # The new identity block fields must be present so multi-host triage works.
    assert "- Host:" in body
    assert "- PID:" in body
    assert "- Git rev:" in body
    assert "- Started:" in body
    assert "Claim TTL:" in body


def test_roadmap_branch_name_carries_host_suffix(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    branch = pkg["ea"].roadmap_issue_branch_name(7, "Hot config reload")
    assert branch.startswith("roadmap/issue-7-hot-config-reload-")
    suffix = branch.rsplit("-", 1)[-1]
    # 6 hex chars from the hostname sha1
    assert len(suffix) == 6
    assert all(c in "0123456789abcdef" for c in suffix)


def test_comment_url_to_id_parses_github_url_shape():
    """The race resolver relies on this to match our own posted claim back to
    the comments list."""
    from threadkeeper.evolve_applier import _comment_url_to_id
    assert _comment_url_to_id(
        "https://github.com/o/r/issues/6#issuecomment-12345"
    ) == "12345"
    assert _comment_url_to_id(
        "https://github.com/o/r/issues/6#issuecomment_67890"
    ) == "67890"
    assert _comment_url_to_id("https://github.com/o/r/issues/6") == ""
    assert _comment_url_to_id("") == ""


# ── single-flight: refuse while an applier child runs ──────────────────────

def test_apply_evolve_single_flight(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    eid = _add_evolve(conn, "some promoted change", status="promoted")
    import os
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at) "
        "VALUES (?,?,?,?,?)",
        ("tk_apl", os.getpid(), "/tmp",
         pkg["ea"].EVOLVE_APPLY_PROMPT_PREFIX + " implementing #1",
         int(time.time())),
    )
    conn.commit()

    def _boom(**kw):
        raise AssertionError("must not spawn while an applier runs")
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", _boom)
    assert "applier_running" in pkg["ea"].apply_evolve(eid)


def test_apply_evolve_single_flight_lock_busy(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    eid = _add_evolve(conn, "some promoted change", status="promoted")

    from contextlib import contextmanager

    @contextmanager
    def _busy_lock():
        yield False

    monkeypatch.setattr(pkg["ea"], "_apply_spawn_lock", _busy_lock)

    def _boom(**kw):
        raise AssertionError("must not spawn while lock is held")
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", _boom)
    assert "single-flight lock" in pkg["ea"].apply_evolve(eid)


def test_apply_curator_report_single_flight_lock_busy(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    report = _write_report(pkg)

    from contextlib import contextmanager

    @contextmanager
    def _busy_lock():
        yield False

    monkeypatch.setattr(pkg["ea"], "_apply_spawn_lock", _busy_lock)

    def _boom(**kw):
        raise AssertionError("must not spawn while lock is held")
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", _boom)
    assert "single-flight lock" in pkg["ea"].apply_curator_report(str(report))


# ── mark applied is the PR gate ────────────────────────────────────────────

def test_mark_applied_only_after_pr(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    eid = _add_evolve(conn, "verbatim scoring change", status="promoted")
    calls = {}
    _mock_spawn(monkeypatch, calls)
    pkg["ea"].apply_evolve(eid)
    # still unapplied right after launch
    assert conn.execute(
        "SELECT applied FROM evolve WHERE id=?", (eid,)
    ).fetchone()["applied"] == 0
    # the child reports the PR → applied flips
    out = pkg["ea"].mark_applied(conn, eid, "https://github.com/o/r/pull/7")
    assert "applied=1" in out
    assert conn.execute(
        "SELECT applied FROM evolve WHERE id=?", (eid,)
    ).fetchone()["applied"] == 1
    # the PR url is recorded as an event
    ev = conn.execute(
        "SELECT summary FROM events WHERE kind='evolve_applied' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ev["summary"] == "https://github.com/o/r/pull/7"


def test_evolve_mark_applied_tool_requires_pr_url(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    eid = _add_evolve(conn, "promoted thing", status="promoted")
    tool = _tool(pkg, "evolve_mark_applied")
    assert tool(evolve_id=eid, pr_url="").startswith("ERR pr_url_required")
    assert tool(evolve_id=eid, pr_url="   ").startswith("ERR pr_url_required")
    # unchanged by the rejected calls
    assert conn.execute(
        "SELECT applied FROM evolve WHERE id=?", (eid,)
    ).fetchone()["applied"] == 0
    # missing id
    assert tool(
        evolve_id=99999, pr_url="https://github.com/o/r/pull/1"
    ).startswith("ERR evolve_not_found")
    # valid → applied
    assert "applied=1" in tool(
        evolve_id=eid, pr_url="https://github.com/o/r/pull/1")


# ── daemon pass dispatch ────────────────────────────────────────────────────

def test_run_apply_pass_disabled(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="0")
    assert pkg["ea"].run_evolve_apply_pass() == "disabled"


def test_run_apply_pass_no_promoted(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_evolve(conn, "pending only", status="pending")
    assert pkg["ea"].run_evolve_apply_pass(force=True) == "no_apply_work"


def test_run_apply_pass_skips_empty_until_interval(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="604800")
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?, 'evolve_apply_pass', ?, 'no_apply_work', ?)",
        ("s_prev", str(now), now),
    )
    conn.commit()

    assert pkg["ea"].run_evolve_apply_pass() == "not_due"
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='evolve_apply_pass'"
    ).fetchone()[0] == 1


def test_run_apply_pass_skips_promoted_backlog_until_interval(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="604800")
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?, 'evolve_apply_pass', ?, 'no_apply_work', ?)",
        ("s_prev", str(now), now),
    )
    older = _add_evolve(conn, "older promoted", status="promoted",
                        created_at=1000)
    calls = {}
    _mock_spawn(monkeypatch, calls)

    out = pkg["ea"].run_evolve_apply_pass()

    assert out == "not_due"
    assert calls == {}


def test_run_apply_pass_picks_curator_report_before_evolve(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_evolve(conn, "older promoted code change", status="promoted",
                created_at=1000)
    report = _write_report(pkg)
    calls = {}
    _mock_spawn(monkeypatch, calls)

    out = pkg["ea"].run_evolve_apply_pass(force=True)

    assert f"curator_report={report.name}" in out
    assert "Curator REPORT" in calls["prompt"]
    assert "older promoted code change" not in calls["prompt"]
    ev = conn.execute(
        "SELECT summary FROM events WHERE kind='evolve_apply_pass' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert f"curator_report={report.name}" in ev["summary"]


def test_run_apply_pass_picks_roadmap_issue_before_curator_and_evolve(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_evolve(conn, "older promoted code change", status="promoted",
                created_at=1000)
    _write_report(pkg)
    monkeypatch.setattr(
        pkg["ea"], "_fetch_open_issues",
        lambda repo_root=None: ([_issue(2, "Hot config")], ""),
    )
    calls = {}
    _mock_spawn(monkeypatch, calls)

    out = pkg["ea"].run_evolve_apply_pass(force=True)

    assert out.startswith("spawned roadmap_issue=#2"), out
    assert "ISSUE #2: Hot config" in calls["prompt"]
    assert "Curator REPORT" not in calls["prompt"]


def test_run_apply_pass_skips_unstartable_issue_and_spawns_next(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pkg["ea"], "_fetch_open_issues",
        lambda repo_root=None: (
            [_issue(1, "Locked issue"), _issue(2, "Startable issue")],
            "",
        ),
    )

    def _claim(issue, repo_root=None):
        num = int(issue["number"])
        if num == 1:
            return "", "gh_issue_comment_failed: locked"
        return f"https://x/issues/{num}#issuecomment-{num}", ""

    monkeypatch.setattr(pkg["ea"], "_comment_issue_claim", _claim)
    calls = {}
    _mock_spawn(monkeypatch, calls)

    out = pkg["ea"].run_evolve_apply_pass(force=True)

    assert out.startswith("spawned roadmap_issue=#2"), out
    assert "after_skipping=1" in out
    assert "ISSUE #2: Startable issue" in calls["prompt"]


def test_run_apply_pass_falls_back_to_curator_when_no_issue_startable(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    report = _write_report(pkg)
    monkeypatch.setattr(
        pkg["ea"], "_fetch_open_issues",
        lambda repo_root=None: ([_issue(1, "Locked issue")], ""),
    )
    monkeypatch.setattr(
        pkg["ea"], "_comment_issue_claim",
        lambda issue, repo_root=None: ("", "gh_issue_comment_failed: locked"),
    )
    calls = {}
    _mock_spawn(monkeypatch, calls)

    out = pkg["ea"].run_evolve_apply_pass(force=True)

    assert f"curator_report={report.name}" in out
    assert "Curator REPORT" in calls["prompt"]
    assert "ISSUE #1" not in calls["prompt"]


def test_run_apply_pass_picks_oldest_promoted(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    # newer promoted first in insertion order, but older created_at must win
    newer = _add_evolve(conn, "newer promoted", status="promoted",
                        created_at=2000)
    older = _add_evolve(conn, "older promoted", status="promoted",
                        created_at=1000)
    _add_evolve(conn, "a pending one", status="pending", created_at=500)
    calls = {}
    _mock_spawn(monkeypatch, calls)
    out = pkg["ea"].run_evolve_apply_pass(force=True)
    assert f"id={older}" in out, out
    assert "older promoted" in calls["prompt"]
    assert "newer promoted" not in calls["prompt"]
    # pass was recorded
    assert pkg["ea"]._last_apply_ts(conn) > 0


# ── repo-root resolution + auto-provisioning ───────────────────────────────

def test_repo_root_prefers_env_override(tmp_path, monkeypatch):
    """An explicit THREADKEEPER_EVOLVE_REPO_ROOT pins the checkout and skips
    auto-resolution."""
    external = tmp_path / "external_repo"
    external.mkdir()
    monkeypatch.setenv("THREADKEEPER_EVOLVE_REPO_ROOT", str(external))
    pkg = _bootstrap(tmp_path, monkeypatch)
    assert pkg["ea"]._repo_root() == external


def test_repo_root_defaults_to_package_checkout(tmp_path, monkeypatch):
    """With no override, an editable-from-checkout install resolves to the
    package's parent dir (which carries a .git entry)."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    from pathlib import Path as _P
    expected = _P(pkg["ea"].__file__).resolve().parent.parent
    assert pkg["ea"]._repo_root() == expected


def test_managed_repo_dir_is_under_db_dir(tmp_path, monkeypatch):
    """The auto-cloned checkout lives next to the DB so it is stable across
    restarts (PyPI/site-packages installs resolve here)."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    assert pkg["ea"]._managed_repo_dir() == (
        pkg["ea"].DB_PATH.parent / "evolve-repo"
    )


def test_ensure_repo_ready_uses_existing_checkout(tmp_path, monkeypatch):
    """The common path: the resolved root is already a git tree (editable
    install / dev checkout) → ready, no provisioning."""
    pkg = _bootstrap(tmp_path, monkeypatch)

    def _no_provision(dest):
        raise AssertionError("must not provision when a checkout exists")
    monkeypatch.setattr(pkg["ea"], "_provision_managed_repo", _no_provision)

    root, err = pkg["ea"]._ensure_repo_ready()
    assert err == ""
    assert root == pkg["ea"]._repo_root()


def test_ensure_repo_ready_auto_clones_when_missing(tmp_path, monkeypatch):
    """Default behaviour on a PyPI install: no checkout → auto-provision a
    managed one. Works out of the box, no env var required."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    managed = tmp_path / "managed-repo"
    monkeypatch.setattr(pkg["ea"], "_resolve_repo_root", lambda: managed)
    monkeypatch.setattr(pkg["ea"], "_is_git_repo", lambda path: False)
    provisioned = []
    monkeypatch.setattr(
        pkg["ea"], "_provision_managed_repo",
        lambda dest: provisioned.append(dest) or "",
    )

    root, err = pkg["ea"]._ensure_repo_ready()
    assert err == ""
    assert root == managed
    assert provisioned == [managed]


def test_ensure_repo_ready_disabled_flag_blocks_auto_clone(tmp_path, monkeypatch):
    """The flag only DISABLES the default: with auto-clone off and no checkout,
    the loops report a clear error and never provision."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    managed = tmp_path / "managed-repo"
    monkeypatch.setattr(pkg["ea"], "_resolve_repo_root", lambda: managed)
    monkeypatch.setattr(pkg["ea"], "_is_git_repo", lambda path: False)
    monkeypatch.setattr(pkg["ea"], "EVOLVE_AUTO_CLONE", False)

    def _no_provision(dest):
        raise AssertionError("must not provision when auto-clone is disabled")
    monkeypatch.setattr(pkg["ea"], "_provision_managed_repo", _no_provision)

    root, err = pkg["ea"]._ensure_repo_ready()
    assert root == managed
    assert err.startswith("ERR evolve_repo_unavailable="), err
    assert "THREADKEEPER_EVOLVE_AUTO_CLONE" in err


def test_ensure_repo_ready_override_not_checkout_errors(tmp_path, monkeypatch):
    """An explicit override that isn't a checkout is never auto-cloned into —
    the user is told to fix the path."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    bad = tmp_path / "not-a-repo"
    bad.mkdir()
    monkeypatch.setattr(pkg["ea"], "EVOLVE_REPO_ROOT", str(bad))
    monkeypatch.setattr(pkg["ea"], "_is_git_repo", lambda path: False)

    def _no_provision(dest):
        raise AssertionError("must not provision into an explicit override path")
    monkeypatch.setattr(pkg["ea"], "_provision_managed_repo", _no_provision)

    root, err = pkg["ea"]._ensure_repo_ready()
    assert root == bad
    assert err.startswith("ERR repo_root_not_git="), err
    assert "THREADKEEPER_EVOLVE_REPO_ROOT" in err


def test_apply_evolve_blocks_when_repo_unavailable(tmp_path, monkeypatch):
    """Code/PR path surfaces a repo-provisioning error and never spawns."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    eid = _add_evolve(conn, "some promoted change", status="promoted")
    from pathlib import Path as _P
    monkeypatch.setattr(
        pkg["ea"], "_ensure_repo_ready",
        lambda: (_P("/x"), "ERR evolve_repo_clone_failed=/x: network down"),
    )

    def _boom(**kw):
        raise AssertionError("must not spawn without a ready checkout")
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", _boom)

    out = pkg["ea"].apply_evolve(eid)
    assert out == "ERR evolve_repo_clone_failed=/x: network down"


def test_apply_roadmap_issue_blocks_when_repo_unavailable(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    from pathlib import Path as _P
    monkeypatch.setattr(
        pkg["ea"], "_ensure_repo_ready",
        lambda: (_P("/x"), "ERR evolve_repo_unavailable=/x (... auto-clone ...)"),
    )

    def _boom(**kw):
        raise AssertionError("must not spawn without a ready checkout")
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", _boom)

    out = pkg["ea"].apply_roadmap_issue()
    assert out.startswith("ERR evolve_repo_unavailable="), out


def test_run_apply_pass_single_flight(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_evolve(conn, "promoted change", status="promoted")
    import os
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at) "
        "VALUES (?,?,?,?,?)",
        ("tk_apl2", os.getpid(), "/tmp",
         pkg["ea"].EVOLVE_APPLY_PROMPT_PREFIX + " working", int(time.time())),
    )
    conn.commit()

    def _boom(**kw):
        raise AssertionError("must not spawn while an applier runs")
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", _boom)
    assert "applier_running" in pkg["ea"].run_evolve_apply_pass(force=True)
