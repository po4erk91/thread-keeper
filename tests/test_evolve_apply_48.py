"""Golden coverage for evolve suggestion #48: guard shared worktrees.

When a full brief sees a live peer, it must make the one-session-per-worktree
boundary explicit before the session can stage, commit, or push. The fixture
also pins surrounding sections so the added safety field cannot break the
existing brief shape.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


_FAKE_CID = "48484848-1111-2222-3333-444444444444"
_PEER_CID = "49494949-1111-2222-3333-444444444444"


def _bootstrap(tmp_path, monkeypatch):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_DISABLE_BG_DAEMONS": "1",
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_MEMORY_GUARD_POLL_S": "0",
        "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CURATOR_INTERVAL_S": "0",
        "THREADKEEPER_EXTRACT_INTERVAL_S": "0",
        "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_PROBE_INTERVAL_S": "0",
        "THREADKEEPER_EVOLVE_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_THREAD_JANITOR_INTERVAL_S": "0",
        "THREADKEEPER_BRIEF_LEAN": "0",
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_FORCE_CID": _FAKE_CID,
        "THREADKEEPER_NO_EMBEDDINGS": "1",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import _mcp, db
    return {"mcp": _mcp.mcp, "db": db}


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def test_live_peer_adds_worktree_safety_without_breaking_brief(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())

    _tool(pkg, "open_thread")(question="seeded open thread still renders")
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, project, session_id, "
        "role, content, model, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("peer-message", "codex", "repo", _PEER_CID, "user",
         "work on a separate task", "?", now),
    )
    conn.execute(
        "INSERT INTO evolve (suggestion, applied, status, created_at) "
        "VALUES (?,?,?,?)",
        ("promoted suggestion still renders", 0, "promoted", now),
    )
    conn.commit()

    from threadkeeper.brief import render_brief
    text = render_brief(conn)

    # New safety field: concurrent work requires an isolated worktree before
    # any git mutation that could mix peer changes.
    assert "worktree_safety peers=1" in text
    assert "own git worktree" in text
    assert "do NOT stage broadly, commit, or push" in text

    # Existing sections still render beside the new field.
    assert "open" in text
    assert "seeded open thread still renders" in text
    assert "evolve_pending" in text
    assert "★ \"promoted suggestion still renders\"" in text
    assert "user-facing: paraphrase plain" in text
