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
        "THREADKEEPER_DIALECTIC_MINE_INTERVAL_S": "0",
        "THREADKEEPER_DIALECTIC_VALIDATE_INTERVAL_S": "0",
        "THREADKEEPER_DIALECTIC_VALIDATE_MIN": "5",
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


def test_mine_run_forces_capture(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, project, session_id, role, "
        "content, model, created_at) VALUES "
            "('uX','claude-code','p','real-sess','user','я предпочитаю X', 'm', ?)",
        (now - 30,),
    )
    conn.commit()
    tool = pkg["mcp"]._tool_manager._tools["dialectic_mine_run"].fn
    out = tool()
    assert "captured=1" in out


def test_validate_status_reports_pending(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO dialectic_observations (dialog_uuid, user_quote, context, "
        "source_cid, status, created_at) VALUES ('u1','q','c','s','pending',?)",
        (now,),
    )
    conn.commit()
    tool = pkg["mcp"]._tool_manager._tools["dialectic_validate_status"].fn
    out = tool()
    assert "pending_now=1" in out
    assert "claimed_now=0" in out
    assert "min=5" in out
    assert "batch_size=50" in out
