"""Golden coverage for surfacing the surgical lesson_patch action in brief().

The format should teach a fresh agent to make a narrow lesson correction with
the atomic operation rather than overwrite an otherwise-correct lesson body.
The seeded fixture also pins nearby long-lived brief sections.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


_FAKE_CID = "eeee3333-4444-5555-6666-777788889999"


def _bootstrap(tmp_path, monkeypatch):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "THREADKEEPER_LESSONS": str(tmp_path / "lessons.md"),
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


def _add_evolve(conn, suggestion):
    conn.execute(
        "INSERT INTO evolve (suggestion, applied, status, created_at) "
        "VALUES (?,?,?,?)",
        (suggestion, 0, "promoted", int(time.time())),
    )
    conn.commit()


def test_brief_surfaces_lesson_patch_and_preserves_existing_sections(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    lesson_append = _tool(pkg, "lesson_append")
    open_thread = _tool(pkg, "open_thread")

    lesson_append(
        title="repair stale cross-links",
        body="Use the current cross-link target.",
        summary="Keep links current.",
        source="foreground",
    )
    open_thread(question="a seeded open thread that must survive")
    _add_evolve(conn, "a promoted suggestion that must surface")

    from threadkeeper.brief import render_brief

    text = render_brief(conn)

    # New: a precise, atomic lesson-edit action is available at session start.
    assert "lesson_patch" in text
    assert "lesson_patch(slug, old_string, new_string)" in text
    assert "unique lesson-body substring" in text

    # Existing: the added section leaves the surrounding brief intact.
    assert "open" in text
    assert "a seeded open thread that must survive" in text
    assert "evolve_pending" in text
    assert "★" in text
    assert "a promoted suggestion that must surface" in text
    assert "user-facing" in text
    assert "Do NOT cite internal IDs" in text
