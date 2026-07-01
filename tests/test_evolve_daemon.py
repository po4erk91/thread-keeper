"""Evolve reviewer daemon — autonomous roadmap audit.

The daemon never implements code. It spawns a child that audits thread-keeper,
does web research where useful, creates/updates GitHub roadmap issues, and may
triage legacy evolve suggestions. Tests exercise dispatch with spawn
monkeypatched; no real child is launched.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


_FAKE_CID = "dddd4444-5555-6666-7777-888899990000"


def _bootstrap(tmp_path, monkeypatch, interval="0", review_min="2"):
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
    from threadkeeper import _mcp, db, evolve_daemon, identity
    return {"mcp": _mcp.mcp, "db": db, "ed": evolve_daemon, "identity": identity}


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
    assert "sort=created" in prompt
    assert "direction=asc" in prompt
    assert "pull_request" in prompt


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
    calls = {}
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn",
                        lambda **kw: calls.update(kw) or "ok task=tk_ev pid=1")

    out = pkg["ed"].run_evolve_pass(force=True)

    assert out.startswith("spawned research")
    expected = str(Path(pkg["ed"].__file__).resolve().parent.parent)
    assert calls["cwd"] == expected


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
    # the failed pass is recorded so the daemon throttles retries
    assert pkg["ed"]._last_evolve_ts(conn) > 0


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
