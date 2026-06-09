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
    assert "evolve_mark_applied" in p
    assert "NEVER" in p and "main" in p  # the no-touch-main guard

    # applied is NOT set just by launching — only after a real PR
    assert conn.execute(
        "SELECT applied FROM evolve WHERE id=?", (eid,)
    ).fetchone()["applied"] == 0


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
    assert pkg["ea"].run_evolve_apply_pass(force=True) == "no_promoted"


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
