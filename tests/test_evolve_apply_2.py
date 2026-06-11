"""Golden coverage for evolve suggestion #2: verbatim quote reuse ranking."""
from __future__ import annotations

import sys
import time
from pathlib import Path


_FAKE_CID = "dddd4444-5555-6666-7777-888899990002"
_OPEN_Q = "brief still shows open thread"
_STICKY_QUOTE = "old quote tied to work we keep reviving"


def _bootstrap(tmp_path, monkeypatch):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
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
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import _mcp, db, identity
    return {"mcp": _mcp.mcp, "db": db, "identity": identity}


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def test_verbatim_ranks_reactivated_quotes_and_keeps_brief_shape(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    open_t = _tool(pkg, "open_thread")
    sticky_tid = open_t(question="sticky quoted workflow")
    open_t(question=_OPEN_Q)

    conn = pkg["db"].get_db()
    sess = pkg["identity"]._session_id or "pytest"
    now = int(time.time())
    old_at = now - 100
    conn.execute(
        "INSERT INTO verbatim (speaker, content, thread_id, created_at, "
        "session_id) VALUES (?,?,?,?,?)",
        ("user", _STICKY_QUOTE, sticky_tid, old_at, sess),
    )
    for i in range(6):
        conn.execute(
            "INSERT INTO verbatim (speaker, content, thread_id, created_at, "
            "session_id) VALUES (?,?,?,?,?)",
            ("user", f"new unreactivated quote {i}", None, now - 60 + i, sess),
        )
    for kind, ts in (
        ("idle_thread", now - 50),
        ("note:move", now - 49),
        ("close_thread", now - 40),
        ("note:insight", now - 39),
    ):
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?,?,?,?,?)",
            (sess, kind, sticky_tid, kind, ts),
        )
    conn.execute(
        "INSERT INTO evolve (suggestion, rationale, applied, created_at, "
        "status) VALUES (?,?,?,?,?)",
        ("promoted brief safeguard", "existing evolve_pending section", 0,
         now, "promoted"),
    )
    conn.commit()

    from threadkeeper.brief import render_brief
    text = render_brief(conn)

    assert "verbatim (reactivated first)" in text
    assert f"react=2 user> \"{_STICKY_QUOTE}\"" in text
    assert "new unreactivated quote 0" not in text
    assert text.index(_STICKY_QUOTE) < text.index("new unreactivated quote 5")

    assert _OPEN_Q in text
    assert "evolve_pending" in text
    assert "★ \"promoted brief safeguard\"" in text
    assert "user-facing: paraphrase plain" in text
