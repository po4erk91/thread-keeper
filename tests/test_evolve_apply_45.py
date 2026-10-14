"""Golden coverage for lesson recommendation reconciliation in brief()."""
from __future__ import annotations

import sys
import time
from pathlib import Path


_FAKE_CID = "bbbb3333-4444-5555-6666-777788889999"
_OPEN_Q = "seeded open thread remains visible"
_OLD_LESSON = "aws-sso-preflight-uses-sts"
_NEW_LESSON = "aws-sso-token-freshness-sts-is-cached"
_COMMAND = "aws sts get-caller-identity"


def _bootstrap(tmp_path, monkeypatch):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_LESSONS": str(tmp_path / "lessons.md"),
        "THREADKEEPER_DISABLE_BG_DAEMONS": "1",
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
    for name in [module for module in list(sys.modules) if module.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import _mcp, db, identity, lessons
    return {"mcp": _mcp.mcp, "db": db, "identity": identity, "lessons": lessons}


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _add_evolve(conn, suggestion):
    conn.execute(
        "INSERT INTO evolve (suggestion, applied, status, created_at) "
        "VALUES (?,?,?,?)",
        (suggestion, 0, "promoted", int(time.time())),
    )
    conn.commit()


def test_brief_surfaces_lesson_reconciliation_and_keeps_existing_sections(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()

    pkg["lessons"].append_lesson(
        _OLD_LESSON,
        body=(
            f"Run `{_COMMAND}` to gate an AWS SSO freshness check before "
            "using fixture-backed cloud tests."
        ),
        source="shadow",
    )
    pkg["lessons"].append_lesson(
        _NEW_LESSON,
        body=(
            f"Never gate SSO freshness on `{_COMMAND}`: it is cached and can "
            "return success after the token expires."
        ),
        source="shadow",
    )

    _tool(pkg, "open_thread")(question=_OPEN_Q)
    _add_evolve(conn, "a promoted suggestion that must remain visible")

    from threadkeeper.brief import render_brief
    text = render_brief(conn)

    # New behavior: a newer debunk flags the older recommendation by command
    # and lesson slug, with a concrete reconciliation action.
    assert "lesson_reconciliation" in text
    assert f"`{_COMMAND}`" in text
    assert f"debunked_by={_NEW_LESSON}" in text
    assert f"still_recommended_by={_OLD_LESSON}" in text
    assert "patch its recommendation or cross-link the correcting lesson" in text

    # Regression: the established brief sections still survive this addition.
    assert "open" in text
    assert _OPEN_Q in text
    assert "evolve_pending" in text
    assert "★ \"a promoted suggestion that must remain visible\"" in text
    assert "user-facing: paraphrase plain" in text
    assert "Do NOT cite internal IDs" in text
