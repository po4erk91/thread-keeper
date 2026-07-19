"""Evolve reviewer daemon — autonomous roadmap audit.

The daemon never implements code. It spawns a child that audits thread-keeper,
does web research where useful, creates/updates GitHub roadmap issues, and may
triage legacy evolve suggestions. Tests exercise dispatch with spawn
monkeypatched; no real child is launched.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import subprocess
import sys
import threading
import time
from pathlib import Path


_FAKE_CID = "dddd4444-5555-6666-7777-888899990000"


def _bootstrap(tmp_path, monkeypatch, interval="0", review_min="2",
               pin_repo=True):
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
        "THREADKEEPER_PROBE_INTERVAL_S": "0",
        "THREADKEEPER_EVOLVE_REVIEW_INTERVAL_S": interval,
        "THREADKEEPER_EVOLVE_REVIEW_MIN": review_min,
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
    from threadkeeper import _mcp, db, evolve_applier, evolve_daemon, identity
    orig = {
        "_git_worktree_precondition": evolve_daemon._git_worktree_precondition,
        "_open_roadmap_doc_prs": evolve_daemon._open_roadmap_doc_prs,
    }
    monkeypatch.setattr(
        evolve_daemon, "_git_worktree_precondition",
        lambda conn, repo_root, actor: "",
    )
    monkeypatch.setattr(
        evolve_daemon, "_open_roadmap_doc_prs",
        lambda repo_root, branch: ([], ""),
    )
    # Pin a ready tmp checkout so the reviewer's _ensure_repo_ready() gate does
    # not run a real `git clone` + venv + `pip install` (~30 s/test) against the
    # shared managed checkout. `_resolve_repo_root`/`_is_git_repo` live in
    # evolve_applier; the daemon resolves them there. Tests exercising the
    # provisioning/error path pass pin_repo=False.
    if pin_repo:
        _repo = tmp_path / "evolve-repo"
        monkeypatch.setattr(evolve_applier, "_resolve_repo_root", lambda: _repo)
        monkeypatch.setattr(evolve_applier, "_is_git_repo", lambda p: True)
    return {
        "mcp": _mcp.mcp,
        "db": db,
        "ea": evolve_applier,
        "ed": evolve_daemon,
        "identity": identity,
        "orig": orig,
    }


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _add_evolve(conn, suggestion, rationale=None, applied=0, status="pending"):
    conn.execute(
        "INSERT INTO evolve (suggestion, rationale, applied, status, created_at) "
        "VALUES (?,?,?,?,?)",
        (suggestion, rationale, applied, status, int(time.time())),
    )
    conn.commit()


def _seed_research(pkg, conn, text="- idea: adopt Y\n  sources: https://ex.com\n"):
    """Make run_evolve_pass take the AUDIT branch on its next call: record a
    prior research spawn so _last_spawn_phase()=='research', and write a fresh
    digest file the audit phase will fence into its prompt."""
    now = int(time.time())
    rdir = pkg["ed"]._research_dir()
    rdir.mkdir(parents=True, exist_ok=True)
    f = rdir / f"RESEARCH-{now}.md"
    f.write_text(text, encoding="utf-8")
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?, 'evolve_review_pass', ?, ?, ?)",
        ("s_prev", str(now), "spawned research file=RESEARCH.md ok task=tk pid=1",
         now),
    )
    conn.commit()
    return f, text


def test_audit_prompt_uses_paginated_issue_dedup(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    prompt = pkg["ed"].EVOLVE_AUDIT_PROMPT

    assert "gh issue list --state open --limit 50" not in prompt
    assert "gh api --include --paginate" in prompt
    assert "state=all" in prompt
    assert "state=open" not in prompt
    assert "sort=created" in prompt
    assert "direction=asc" in prompt
    assert "not_planned" in prompt
    assert "state_reason" in prompt
    assert "pull_request" in prompt
    assert "evolve_issue_create" in prompt
    assert "Do not call `gh issue create` directly" in prompt
    assert "git fetch origin {base_branch}" in prompt
    assert (
        "git fetch origin {roadmap_branch}:refs/remotes/origin/{roadmap_branch}"
        in prompt
    )
    assert "git checkout -b {roadmap_branch} {base_ref}" in prompt
    assert "gh pr list --state open --json" in prompt
    assert "docs/ROADMAP.md" in prompt
    assert "append/skip" in prompt


def test_issue_dedup_filters_existing_closed_and_within_pass(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    existing = [
        {
            "number": 67,
            "title": (
                "Adopt MCP tool annotations readOnly/destructive hints "
                "and structured output"
            ),
            "state": "OPEN",
            "body": (
                "Expose readOnlyHint and destructiveHint metadata for every "
                "MCP tool and return structuredContent where appropriate."
            ),
        },
        {
            "number": 73,
            "title": (
                "Fence untrusted ingested transcript content to prevent "
                "memory poisoning in auto-loaded skills and lessons"
            ),
            "state": "CLOSED",
            "state_reason": "not_planned",
            "body": (
                "Keep ingested transcript data from becoming instructions "
                "inside auto-loaded skills, lessons, and review prompts."
            ),
        },
    ]
    candidates = [
        {
            "title": (
                "Add MCP annotations for readOnly and destructive tool hints "
                "with structured output"
            ),
            "body": (
                "The MCP server should advertise readOnlyHint and "
                "destructiveHint metadata and include structuredContent."
            ),
        },
        {
            "title": (
                "Fence transcript data before it reaches auto loaded skills "
                "or lessons"
            ),
            "body": (
                "Untrusted ingested transcript content can poison prompts; "
                "keep it fenced as data in skills, lessons, and reviewers."
            ),
        },
        {
            "title": "Persist reviewer issue fingerprints before filing",
            "body": (
                "Record normalized reviewer issue fingerprints in a local "
                "ledger so the next reviewer pass skips repeated gaps."
            ),
        },
        {
            "title": "Persist reviewer issue fingerprints before filing",
            "body": (
                "Record normalized reviewer issue fingerprints in a local "
                "ledger so the next reviewer pass skips repeated gaps."
            ),
        },
    ]

    accepted, skipped = pkg["ed"].dedupe_candidate_issues(
        candidates, existing_issues=existing
    )

    assert [item["title"] for item in accepted] == [
        "Persist reviewer issue fingerprints before filing"
    ]
    assert [item["reason"] for item in skipped] == [
        "near_duplicate", "near_duplicate", "within_pass",
    ]
    assert skipped[0]["match"]["number"] == 67
    assert skipped[1]["match"]["number"] == 73
    assert skipped[1]["match"]["state_reason"] == "not_planned"


def test_evolve_issue_create_skips_closed_not_planned_match(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    closed = [{
        "number": 70,
        "title": (
            "Adopt MCP tool annotations readOnly destructive hints and "
            "structured output"
        ),
        "state": "closed",
        "state_reason": "not_planned",
        "body": (
            "Use MCP readOnlyHint destructiveHint and structuredContent "
            "metadata for tools."
        ),
    }]
    monkeypatch.setattr(
        pkg["ed"], "_fetch_github_issues_for_dedup",
        lambda repo_root=None: (closed, ""),
    )

    out = pkg["ed"].create_reviewer_issue(
        title="Add MCP tool annotations with readOnly destructive hints",
        body=(
            "Expose readOnlyHint destructiveHint and structuredContent "
            "metadata for every MCP tool."
        ),
        labels="enhancement,roadmap",
        repo_root=tmp_path,
    )

    assert out.startswith("skipped duplicate")
    assert "match=#70" in out
    row = conn.execute(
        "SELECT summary FROM events WHERE kind=?",
        (pkg["ed"].EVOLVE_ISSUE_SKIPPED_KIND,),
    ).fetchone()
    assert row is not None
    assert "state_reason=not_planned" in row["summary"]
    assert conn.execute("SELECT COUNT(*) FROM evolve_issues").fetchone()[0] == 0


def test_evolve_issue_create_records_ledger_and_second_pass_skips(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    monkeypatch.setattr(
        pkg["ed"], "_fetch_github_issues_for_dedup",
        lambda repo_root=None: ([], ""),
    )
    calls = []

    def fake_run_gh(cmd, *, cwd, timeout=30):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, "https://github.com/o/r/issues/123\n", ""
        )

    monkeypatch.setattr(pkg["ed"], "_run_gh", fake_run_gh)
    title = "Persist reviewer issue fingerprints before filing"
    body = (
        "Record normalized reviewer issue fingerprints in a local ledger "
        "so unchanged reviewer passes skip repeated gaps."
    )

    first = pkg["ed"].create_reviewer_issue(
        title=title, body=body, labels="enhancement,roadmap", repo_root=tmp_path
    )
    second = pkg["ed"].create_reviewer_issue(
        title=title, body=body, labels="enhancement,roadmap", repo_root=tmp_path
    )

    assert first.startswith("created #123")
    assert second.startswith("skipped duplicate")
    assert len(calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM evolve_issues").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind=?",
        (pkg["ed"].EVOLVE_ISSUE_FILED_KIND,),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind=?",
        (pkg["ed"].EVOLVE_ISSUE_SKIPPED_KIND,),
    ).fetchone()[0] == 1


def test_evolve_issue_create_sanitizes_public_title_and_body(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pkg["ed"], "_fetch_github_issues_for_dedup",
        lambda repo_root=None: ([], ""),
    )
    seen = {}

    def fake_run_gh(cmd, *, cwd, timeout=30):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0, "https://github.com/o/r/issues/124\n", ""
        )

    monkeypatch.setattr(pkg["ed"], "_run_gh", fake_run_gh)

    out = pkg["ed"].create_reviewer_issue(
        title="Do not leak /Users/alice/private in reviewer issues",
        body="Token ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa from /Users/alice/.env",
        labels="enhancement,roadmap",
        repo_root=tmp_path,
    )

    assert out.startswith("created #124")
    title = seen["cmd"][seen["cmd"].index("--title") + 1]
    body = seen["cmd"][seen["cmd"].index("--body") + 1]
    assert "/Users/alice" not in title
    assert "/Users/alice" not in body
    assert "ghp_" not in body
    assert "[REDACTED_HOME_PATH]" in title
    assert "[REDACTED_HOME_PATH]" in body
    assert "[REDACTED_SECRET]" in body


def test_roadmap_doc_branch_name_reuses_same_daily_branch(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    ts1 = int(datetime(2026, 7, 5, 1, tzinfo=timezone.utc).timestamp())
    ts2 = int(datetime(2026, 7, 5, 23, 59, tzinfo=timezone.utc).timestamp())
    ts3 = int(datetime(2026, 7, 6, 0, tzinfo=timezone.utc).timestamp())

    assert pkg["ed"].roadmap_doc_branch_name(ts1) == (
        "docs/roadmap-audit-2026-07-05"
    )
    assert pkg["ed"].roadmap_doc_branch_name(ts2) == (
        "docs/roadmap-audit-2026-07-05"
    )
    assert pkg["ed"].roadmap_doc_branch_name(ts3) == (
        "docs/roadmap-audit-2026-07-06"
    )


def test_audit_prompt_reports_existing_roadmap_doc_pr(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, review_min="2")
    conn = pkg["db"].get_db()
    _seed_research(pkg, conn)
    monkeypatch.setattr(
        pkg["ed"], "_open_roadmap_doc_prs",
        pkg["orig"]["_open_roadmap_doc_prs"],
    )
    seen = {}

    def fake_run_gh(cmd, *, cwd, timeout=30):
        seen["cmd"] = cmd
        data = [{
            "number": 123,
            "url": "https://github.com/o/r/pull/123",
            "headRefName": "docs/roadmap-audit-2026-07-05",
            "title": "docs: update roadmap audit",
            "author": {"login": "po4erk91"},
            "body": "",
            "files": [{"path": "docs/ROADMAP.md"}],
        }]
        return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")

    monkeypatch.setattr(pkg["ed"], "_run_gh", fake_run_gh)
    calls = {}
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn",
                        lambda **kw: calls.update(kw) or "ok task=tk_ev pid=1")

    out = pkg["ed"].run_evolve_pass(force=True)

    assert out.startswith("spawned audit")
    assert seen["cmd"][:4] == ["gh", "pr", "list", "--state"]
    assert any("files" in part for part in seen["cmd"])
    assert "Existing open reviewer roadmap-doc PR found: #123" in calls["prompt"]
    assert "https://github.com/o/r/pull/123" in calls["prompt"]
    assert "Use that branch; do not create a second roadmap-doc PR" in (
        calls["prompt"]
    )


# ── pending selection ──────────────────────────────────────────────────

def test_pending_excludes_applied_and_decided(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_evolve(conn, "pending one")
    _add_evolve(conn, "already applied", applied=1)
    _add_evolve(conn, "already dismissed", status="dismissed")
    _add_evolve(conn, "already promoted", status="promoted")
    pend = pkg["ed"]._pending(conn)
    sugg = [r["suggestion"] for r in pend]
    assert sugg == ["pending one"]


# ── evolve_decide tool ─────────────────────────────────────────────────

def test_evolve_decide_promote(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_evolve(conn, "make briefs shorter")
    eid = conn.execute("SELECT id FROM evolve").fetchone()["id"]
    out = _tool(pkg, "evolve_decide")(evolve_id=eid, decision="promote",
                                      reason="clear win")
    assert "status=promoted" in out
    row = conn.execute("SELECT status, review_reason, reviewed_at FROM evolve "
                       "WHERE id=?", (eid,)).fetchone()
    assert row["status"] == "promoted"
    assert row["review_reason"] == "clear win"
    assert row["reviewed_at"] is not None


def test_evolve_decide_dismiss_and_bad_inputs(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_evolve(conn, "dup suggestion")
    eid = conn.execute("SELECT id FROM evolve").fetchone()["id"]
    assert "status=dismissed" in _tool(pkg, "evolve_decide")(
        evolve_id=eid, decision="dismiss", reason="duplicate of #1")
    assert _tool(pkg, "evolve_decide")(
        evolve_id=eid, decision="banana").startswith("ERR bad_decision")
    assert _tool(pkg, "evolve_decide")(
        evolve_id=9999, decision="promote").startswith("ERR evolve_not_found")


# ── run_evolve_pass dispatch ────────────────────────────────────────────

def test_run_evolve_pass_disabled(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    assert pkg["ed"].run_evolve_pass() == "disabled"


def test_run_evolve_pass_force_spawns_research_first(
    tmp_path, monkeypatch,
):
    """With no prior spawn, the first pass is the read-only research phase — not
    the privileged audit phase (#79)."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    calls = {}
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn",
                        lambda **kw: calls.update(kw) or "ok task=tk_ev pid=1")

    out = pkg["ed"].run_evolve_pass(force=True)

    assert out.startswith("spawned research")
    assert "research phase" in calls["prompt"]
    assert "untrusted DATA" in calls["prompt"]
    # research is read-only web: web tools yes, privilege/shell no
    assert "WebSearch" in calls["extra_allowed_tools"]
    assert "WebFetch" in calls["extra_allowed_tools"]
    assert calls["permission_mode"] != "bypassPermissions"
    assert "Bash" not in calls["extra_allowed_tools"]


def test_run_evolve_pass_skips_empty_until_interval(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="604800")
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?, 'evolve_review_pass', ?, 'no_pending', ?)",
        ("s_prev", str(now), now),
    )
    conn.commit()

    assert pkg["ed"].run_evolve_pass() == "not_due"
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='evolve_review_pass'"
    ).fetchone()[0] == 1


def test_run_evolve_pass_below_min(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, review_min="2")
    conn = pkg["db"].get_db()
    _add_evolve(conn, "only one")
    calls = {}
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn",
                        lambda **kw: calls.update(kw) or "ok task=tk_ev pid=1")

    out = pkg["ed"].run_evolve_pass(force=True)

    # A forced pass spawns regardless of the suggestion count; the first phase is
    # research (the legacy queue is consumed later, in the audit phase).
    assert out.startswith("spawned research")


def test_run_evolve_pass_skips_legacy_backlog_until_interval(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="604800", review_min="2")
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?, 'evolve_review_pass', ?, 'no_pending', ?)",
        ("s_prev", str(now), now),
    )
    _add_evolve(conn, "suggestion alpha")
    _add_evolve(conn, "suggestion beta")
    calls = {}
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(
        spawn_mod, "spawn",
        lambda **kw: calls.update(kw) or "ok task=tk_ev pid=1",
    )

    out = pkg["ed"].run_evolve_pass()

    assert out == "not_due"
    assert calls == {}


def test_run_evolve_pass_research_phase_is_read_only(tmp_path, monkeypatch):
    """Phase 1 (research): web access but no privilege/shell — it cannot
    exfiltrate, so the untrusted web content it reads can't complete the
    trifecta (#79)."""
    pkg = _bootstrap(tmp_path, monkeypatch, review_min="2")
    conn = pkg["db"].get_db()
    _add_evolve(conn, "suggestion alpha")
    calls = {}
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn",
                        lambda **kw: calls.update(kw) or "ok task=tk_ev pid=1")
    out = pkg["ed"].run_evolve_pass(force=True)
    assert out.startswith("spawned research")
    assert calls["write_origin"] == "evolve"
    assert calls["role"] == "evolve_researcher"
    # web research yes; bypass/shell/GitHub-write no
    assert "WebSearch" in calls["extra_allowed_tools"]
    assert "WebFetch" in calls["extra_allowed_tools"]
    assert calls["permission_mode"] != "bypassPermissions"
    assert "Bash" not in calls["extra_allowed_tools"]
    assert "Edit" not in calls["extra_allowed_tools"]
    assert "evolve_decide" not in calls["extra_allowed_tools"]
    assert pkg["ed"]._last_evolve_ts(conn) > 0


def test_run_evolve_pass_audit_phase_no_web_consumes_fenced_research(
    tmp_path, monkeypatch,
):
    """Phase 2 (audit): privileged (bypass + Bash/Edit/Write) but NO web tools;
    it consumes the prior research digest as fenced untrusted data (#79)."""
    pkg = _bootstrap(tmp_path, monkeypatch, review_min="2")
    conn = pkg["db"].get_db()
    _add_evolve(conn, "suggestion alpha", rationale="friction A")
    _add_evolve(conn, "suggestion beta")
    _seed_research(pkg, conn, text="- idea: adopt thing Z\n  sources: https://z\n")
    calls = {}
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn",
                        lambda **kw: calls.update(kw) or "ok task=tk_ev pid=1")
    out = pkg["ed"].run_evolve_pass(force=True)
    assert out.startswith("spawned audit pending=2")
    assert calls["write_origin"] == "evolve"
    assert calls["role"] == "evolve_reviewer"
    assert calls["permission_mode"] == "bypassPermissions"
    # privileged write tools present; reviewer still cannot apply issues
    assert "Bash" in calls["extra_allowed_tools"]
    assert "Edit" in calls["extra_allowed_tools"]
    assert "Write" in calls["extra_allowed_tools"]
    assert "evolve_decide" in calls["extra_allowed_tools"]
    assert "skill_manage" not in calls["extra_allowed_tools"]
    assert "evolve_mark_roadmap_issue_applied" not in calls["extra_allowed_tools"]
    # no web tools in the privileged child
    assert "WebSearch" not in calls["extra_allowed_tools"]
    assert "WebFetch" not in calls["extra_allowed_tools"]
    # legacy suggestions reach the audit prompt
    assert "suggestion alpha" in calls["prompt"]
    assert "suggestion beta" in calls["prompt"]
    assert "friction A" in calls["prompt"]
    assert "<evolve_legacy_suggestions_data>" in calls["prompt"]
    assert "</evolve_legacy_suggestions_data>" in calls["prompt"]
    # research is embedded, fenced, and explicitly flagged untrusted
    assert "adopt thing Z" in calls["prompt"]
    assert "untrusted data" in calls["prompt"].lower()
    assert pkg["ed"].EVOLVE_RESEARCH_FENCE in calls["prompt"]
    assert "git fetch origin main" in calls["prompt"]
    assert "git checkout -b docs/roadmap-audit-" in calls["prompt"]
    assert "origin/main" in calls["prompt"]
    assert "No open reviewer roadmap-doc PR touching docs/ROADMAP.md" in (
        calls["prompt"]
    )


def test_run_evolve_pass_audit_skips_dirty_worktree_and_records_event(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, review_min="2")
    conn = pkg["db"].get_db()
    _add_evolve(conn, "suggestion alpha")
    _seed_research(pkg, conn)
    monkeypatch.setattr(
        pkg["ed"], "_git_worktree_precondition",
        pkg["orig"]["_git_worktree_precondition"],
    )
    monkeypatch.setattr(
        pkg["ea"], "_tracked_worktree_status",
        lambda repo_root: (" M docs/ROADMAP.md", ""),
    )
    monkeypatch.setattr(
        pkg["ea"], "_running_git_writer_children",
        lambda conn: [],
    )
    monkeypatch.setattr(
        pkg["ea"], "_managed_repo_auto_recovery_allowed", lambda repo: False,
    )

    def _boom(**kw):
        raise AssertionError("must not spawn reviewer audit from dirty checkout")

    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", _boom)

    out = pkg["ed"].run_evolve_pass(force=True)

    assert out == "skipped_dirty_worktree mode=git"
    safety = conn.execute(
        "SELECT target, summary FROM events WHERE kind=?",
        (pkg["ea"].EVOLVE_GIT_SAFETY_KIND,),
    ).fetchone()
    assert safety["target"] == "evolve_reviewer_audit"
    assert safety["summary"] == "skipped_dirty_worktree mode=git"
    review = conn.execute(
        "SELECT summary FROM events WHERE kind='evolve_review_pass' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert review["summary"] == "skipped_dirty_worktree mode=git"


def test_web_research_and_privileged_write_never_cogranted(tmp_path, monkeypatch):
    """The core #79 invariant: across both phases, no single reviewer child holds
    web research (WebSearch/WebFetch) AND the bypassPermissions + Bash/Write
    capability at the same time."""
    pkg = _bootstrap(tmp_path, monkeypatch, review_min="2")
    conn = pkg["db"].get_db()
    _add_evolve(conn, "suggestion alpha")
    captured = []
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(
        spawn_mod, "spawn",
        lambda **kw: captured.append(dict(kw)) or "ok task=tk_ev pid=1",
    )

    # Phase 1 records a real "spawned research" pass, so phase 2 takes the audit
    # branch on the next forced call.
    out1 = pkg["ed"].run_evolve_pass(force=True)
    assert out1.startswith("spawned research")
    out2 = pkg["ed"].run_evolve_pass(force=True)
    assert out2.startswith("spawned audit")

    assert len(captured) == 2
    for kw in captured:
        tools = kw["extra_allowed_tools"]
        has_web = "WebSearch" in tools or "WebFetch" in tools
        privileged = (
            kw.get("permission_mode") == "bypassPermissions"
            and ("Bash" in tools or "Write" in tools)
        )
        assert not (has_web and privileged), (
            "lethal trifecta: a single child holds web research + "
            "bypassPermissions + Bash/Write"
        )


def test_run_evolve_pass_runs_reviewer_in_repo_root(tmp_path, monkeypatch):
    """The reviewer child must run with cwd pinned to the repo checkout, not the
    host CLI's working directory."""
    pkg = _bootstrap(tmp_path, monkeypatch, review_min="1")
    conn = pkg["db"].get_db()
    _add_evolve(conn, "s1")
    # Pin a ready checkout (the isolation default resolves to an unprovisioned
    # managed dir) so the reviewer reaches spawn; the point of the test is that
    # cwd is the resolved checkout, not the host CLI's working dir.
    repo = tmp_path / "evolve-repo"
    repo.mkdir()
    monkeypatch.setattr(pkg["ed"], "_ensure_repo_ready", lambda: (repo, ""))
    calls = {}
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn",
                        lambda **kw: calls.update(kw) or "ok task=tk_ev pid=1")

    out = pkg["ed"].run_evolve_pass(force=True)

    assert out.startswith("spawned research")
    assert calls["cwd"] == str(repo)


def test_run_evolve_pass_blocks_when_repo_unavailable(tmp_path, monkeypatch):
    """When the checkout can't be provisioned (e.g. auto-clone disabled on a
    PyPI install), the reviewer refuses to spawn and records an actionable
    error instead of running gh/file reads against the wrong dir."""
    pkg = _bootstrap(tmp_path, monkeypatch, review_min="1")
    conn = pkg["db"].get_db()
    _add_evolve(conn, "s1")
    monkeypatch.setattr(
        pkg["ed"], "_ensure_repo_ready",
        lambda: (Path("/x"), "ERR evolve_repo_unavailable=/x (... auto-clone ...)"),
    )

    def _boom(**kw):
        raise AssertionError("must not spawn without a ready checkout")
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", _boom)

    out = pkg["ed"].run_evolve_pass(force=True)
    assert out.startswith("ERR evolve_repo_unavailable="), out
    # A no-spawn outcome must not consume the review slot: the cursor stays
    # unset so the short transient-retry cadence re-checks as soon as the
    # operator fixes the config, while the outcome row is still recorded for
    # observability (consecutive same-class rows collapse, so retries can't
    # flood events).
    assert pkg["ed"]._last_evolve_ts(conn) == 0
    summary = conn.execute(
        "SELECT summary FROM events WHERE kind='evolve_review_pass' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()["summary"]
    assert summary.startswith("ERR evolve_repo_unavailable=")


def test_run_evolve_pass_single_flight(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, review_min="1")
    conn = pkg["db"].get_db()
    _add_evolve(conn, "s1")
    import os
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at) "
        "VALUES (?,?,?,?,?)",
        ("tk_evr", os.getpid(), "/tmp",
         "You are an EVOLVE REVIEWER triaging the queue.", int(time.time())),
    )
    conn.commit()

    def _boom(**kw):
        raise AssertionError("must not spawn while a reviewer runs")
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", _boom)
    assert "reviewer_running" in pkg["ed"].run_evolve_pass(force=True)


def test_run_evolve_pass_single_flight_lock_race(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, review_min="1")
    repo = tmp_path / "evolve-repo"
    repo.mkdir()
    monkeypatch.setattr(pkg["ed"], "_ensure_repo_ready", lambda: (repo, ""))
    import threadkeeper.tools.spawn as spawn_mod

    entered_spawn = threading.Event()
    release_spawn = threading.Event()
    results: list[str] = []
    errors: list[BaseException] = []
    calls: list[dict] = []

    def fake_spawn(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            entered_spawn.set()
            assert release_spawn.wait(timeout=5)
        return "ok task=tk_ev pid=1"

    def run_pass():
        try:
            out = pkg["ed"].run_evolve_pass(force=True)
            results.append(out)
        except BaseException as e:  # pragma: no cover - surfaced below
            errors.append(e)
            release_spawn.set()

    monkeypatch.setattr(spawn_mod, "spawn", fake_spawn)

    t = threading.Thread(target=run_pass)
    t.start()
    assert entered_spawn.wait(timeout=5)
    results.append(pkg["ed"].run_evolve_pass(force=True))
    release_spawn.set()
    t.join(timeout=5)
    assert not t.is_alive()

    assert not errors
    assert len(calls) == 1
    assert len(results) == 2
    assert sum(r.startswith("spawned research") for r in results) == 1
    assert results.count("reviewer_running n=1 (single-flight lock)") == 1


# ── brief surfaces promoted ★ first, drops dismissed ───────────────────

def test_brief_evolve_promoted_marked_dismissed_hidden(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _add_evolve(conn, "promoted one", status="promoted")
    _add_evolve(conn, "pending one", status="pending")
    _add_evolve(conn, "dismissed one", status="dismissed")
    from threadkeeper.brief import render_brief
    text = render_brief(conn)
    # suggestion text is wrapped by q(); assert on the ★ marker + substring
    assert "★" in text
    assert "promoted one" in text
    assert "pending one" in text
    assert "dismissed one" not in text
    # the ★ marker attaches to the promoted suggestion, not the pending one
    assert text.index("★") < text.index("promoted one")
    # promoted sorts before pending
    assert text.index("promoted one") < text.index("pending one")


def test_transient_outcome_preserves_review_slot(tmp_path, monkeypatch):
    """A no-spawn outcome must not consume the weekly review cursor."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    ed = pkg["ed"]
    ed._record_evolve_pass(conn, 1000, "spawned research file=R.md ok")
    assert ed._last_evolve_ts(conn) == 1000

    ed._record_transient_evolve_pass(conn, "skipped_dirty_worktree mode=git")
    assert ed._last_evolve_ts(conn) == 1000  # cursor preserved

    n_before = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='evolve_review_pass'"
    ).fetchone()[0]
    # Same outcome class collapses (edge-triggered, no hourly retry flood)…
    ed._record_transient_evolve_pass(conn, "skipped_dirty_worktree mode=git")
    n_same = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='evolve_review_pass'"
    ).fetchone()[0]
    assert n_same == n_before
    # …while a different class still lands, cursor still preserved.
    ed._record_transient_evolve_pass(conn, "reviewer_running n=1")
    assert ed._last_evolve_ts(conn) == 1000
    n_after = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='evolve_review_pass'"
    ).fetchone()[0]
    assert n_after == n_before + 1


def test_last_spawn_phase_not_buried_by_transient_rows(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    ed = pkg["ed"]
    ed._record_evolve_pass(conn, 1000, "spawned research file=R.md ok")
    for i in range(40):
        ed._record_evolve_pass(
            conn, 1000, f"skipped_dirty_worktree mode=git attempt={i}"
        )
    assert ed._last_spawn_phase(conn) == "research"
