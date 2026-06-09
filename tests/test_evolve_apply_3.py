"""Golden test for evolve suggestion #3: surface a `failed_paths` field per
open thread in the brief.

`kind='failed'` notes ("tried X, broke because Y") live flat in the notes
table and never reached render_brief(), so the agent kept re-walking dead
ends. The applier adds a compact `failed_paths=` sub-line beneath each open
thread that has failed notes. This test pins BOTH the new behavior and the
fact that the surrounding brief still renders.

Bootstrap mirrors tests/test_evolve_daemon.py: clean env, module reload,
render_brief(conn) called directly.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


_FAKE_CID = "cccc3333-4444-5555-6666-777788889999"


def _bootstrap(tmp_path, monkeypatch):
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
    from threadkeeper import _mcp, db, brief, identity
    return {"mcp": _mcp.mcp, "db": db, "brief": brief, "identity": identity}


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _add_evolve(conn, suggestion, status="pending"):
    conn.execute(
        "INSERT INTO evolve (suggestion, applied, status, created_at) "
        "VALUES (?,?,?,?)",
        (suggestion, 0, status, int(time.time())),
    )
    conn.commit()


# ── new behavior: failed notes surface as failed_paths per open thread ──────

def test_failed_paths_surfaces_for_open_thread(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    open_t = _tool(pkg, "open_thread")
    note = _tool(pkg, "note")

    tid = open_t(question="ship the failed_paths brief field")
    note(thread_id=tid, content="tried bumping the cosine threshold, broke recall",
         kind="failed")
    note(thread_id=tid, content="tried widening the FTS window, still misses",
         kind="failed")
    # a non-failed note must NOT leak into failed_paths
    note(thread_id=tid, content="decided to surface failed notes in the brief",
         kind="move")

    from threadkeeper.brief import render_brief
    text = render_brief(conn)

    # (a) NEW field is present and carries the failed-note content
    assert "failed_paths=" in text
    assert "tried bumping the cosine threshold" in text
    assert "tried widening the FTS window" in text
    # the field appears beneath the thread it belongs to, after the open header
    assert text.index("open") < text.index("failed_paths=")
    # the 'move' note is the last_move, not a failed_path
    fp_block = text[text.index("failed_paths="):]
    assert "decided to surface failed notes" not in fp_block


def test_failed_paths_absent_when_no_failed_notes(tmp_path, monkeypatch):
    """An open thread with only move/insight notes shows no failed_paths line."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    open_t = _tool(pkg, "open_thread")
    note = _tool(pkg, "note")
    tid = open_t(question="clean thread, no dead ends")
    note(thread_id=tid, content="made progress", kind="move")

    from threadkeeper.brief import render_brief
    text = render_brief(conn)
    assert "clean thread, no dead ends" in text
    assert "failed_paths=" not in text


# ── regression: the rest of the brief still renders ─────────────────────────

def test_brief_still_renders_existing_sections(tmp_path, monkeypatch):
    """The format change must not silently break the brief: a seeded open
    thread, the promoted evolve_pending ★, and the user-facing footer reminder
    must all still appear alongside the new failed_paths field."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    open_t = _tool(pkg, "open_thread")
    note = _tool(pkg, "note")

    tid = open_t(question="a seeded open thread that must survive")
    note(thread_id=tid, content="tried the obvious fix, it regressed", kind="failed")
    _add_evolve(conn, "a promoted suggestion that must surface", status="promoted")

    from threadkeeper.brief import render_brief
    text = render_brief(conn)

    # pre-existing: the open thread header + question
    assert "open" in text
    assert "a seeded open thread that must survive" in text
    # pre-existing: evolve_pending block with the promoted ★ marker
    assert "evolve_pending" in text
    assert "★" in text
    assert "a promoted suggestion that must surface" in text
    # pre-existing: the trailing user-facing IDs reminder
    assert "user-facing" in text
    assert "Do NOT cite internal IDs" in text
    # new: failed_paths rides alongside the untouched sections
    assert "failed_paths=" in text
    assert "tried the obvious fix, it regressed" in text
