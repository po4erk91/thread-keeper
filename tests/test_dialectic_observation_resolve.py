from __future__ import annotations

import sys
import time
from pathlib import Path

_FAKE_CID = "aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000"


def _bootstrap(tmp_path, monkeypatch):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_FORCE_CID": _FAKE_CID,
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import _mcp, db
    return {"mcp": _mcp.mcp, "db": db}


def test_resolve_marks_processed(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO dialectic_observations (dialog_uuid, user_quote, context, "
        "source_cid, status, created_at) VALUES ('u1','q','c','s','pending',?)",
        (now,),
    )
    conn.commit()
    oid = conn.execute("SELECT id FROM dialectic_observations").fetchone()["id"]
    tool = pkg["mcp"]._tool_manager._tools["dialectic_observation_resolve"].fn
    out = tool(id=oid, note="chit-chat")
    assert "ok" in out
    row = conn.execute(
        "SELECT status, processed_at FROM dialectic_observations WHERE id=?",
        (oid,),
    ).fetchone()
    assert row["status"] == "processed"
    assert row["processed_at"] is not None


def test_resolve_unknown_id_errors(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    tool = pkg["mcp"]._tool_manager._tools["dialectic_observation_resolve"].fn
    assert tool(id=99999).startswith("ERR")
