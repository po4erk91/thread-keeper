"""dialectic_miner — mechanical capture of user replies + preceding-assistant
context into dialectic_observations. No LLM, no spawn. Same session-filtering
as extract (exclude internal-prompt + spawned-child sessions)."""
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
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CURATOR_INTERVAL_S": "0",
        "THREADKEEPER_EXTRACT_INTERVAL_S": "0",
        "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_DIALECTIC_MINE_INTERVAL_S": interval,
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_FORCE_CID": _FAKE_CID,
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import db, dialectic_miner, identity
    return {"db": db, "dialectic_miner": dialectic_miner, "identity": identity}


def _seed(conn, role, content, ts, session_id="real-sess"):
    uid = f"u-{ts}-{role}-{abs(hash(content)) % 100000}"
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, project, session_id, role, "
        "content, model, created_at) VALUES (?, 'claude-code', 'p1', ?, ?, ?, "
        "'test-model', ?)",
        (uid, session_id, role, content, ts),
    )


def test_disabled_without_force(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    assert pkg["dialectic_miner"].run_mine_pass() == "disabled"


def test_captures_user_reply_with_context(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    _seed(conn, "assistant", "Which auth method do you want?", now - 60)
    _seed(conn, "user", "use better-auth with the neon adapter", now - 50)
    conn.commit()
    out = pkg["dialectic_miner"].run_mine_pass(force=True)
    assert "captured=1" in out
    row = conn.execute(
        "SELECT user_quote, context, status FROM dialectic_observations"
    ).fetchone()
    assert row["user_quote"] == "use better-auth with the neon adapter"
    assert "Which auth method" in row["context"]
    assert row["status"] == "pending"


def test_dedup_by_dialog_uuid(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    _seed(conn, "user", "remember I prefer lean prose", now - 40)
    conn.commit()
    pkg["dialectic_miner"].run_mine_pass(force=True)
    pkg["dialectic_miner"].run_mine_pass(force=True)
    n = conn.execute("SELECT COUNT(*) FROM dialectic_observations").fetchone()[0]
    assert n == 1


def test_excludes_spawned_child_session(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    child = "child-cid"
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at) VALUES ('tk_x', 0, 'p', ?, '/x', 'You are auditing.', ?)",
        (child, now - 200),
    )
    _seed(conn, "user", "internal child utterance about X", now - 90, session_id=child)
    _seed(conn, "user", "real user preference statement here", now - 60, session_id="real-sess")
    conn.commit()
    pkg["dialectic_miner"].run_mine_pass(force=True)
    cids = [r["source_cid"] for r in conn.execute(
        "SELECT source_cid FROM dialectic_observations").fetchall()]
    assert "real-sess" in cids
    assert child not in cids


def test_cursor_advances_and_no_spawn(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    pkg["dialectic_miner"].run_mine_pass(force=True)
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='dialectic_mine_pass'"
    ).fetchone()[0]
    assert n == 1


def test_daemon_does_not_start_in_slim_child(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="3600")
    import threadkeeper.config as cfg
    monkeypatch.setattr(cfg, "BACKGROUND_DAEMONS_ALLOWED", False)
    pkg["dialectic_miner"]._started = False
    pkg["dialectic_miner"].start_dialectic_miner_daemon()
    assert pkg["dialectic_miner"]._started is False


def test_daemon_disabled_at_interval_zero(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="0")
    pkg["dialectic_miner"]._started = False
    pkg["dialectic_miner"].start_dialectic_miner_daemon()
    assert pkg["dialectic_miner"]._started is False


def test_excludes_internal_prompt_session(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    # Session opened by an internal spawn-prompt marker → whole session excluded.
    _seed(conn, "user",
          "You are a SHADOW LEARNING OBSERVER for thread-keeper. You read a "
          "slice of recent dialog and decide what to keep.",
          now - 90, session_id="shadow-sess")
    _seed(conn, "user",
          "a substantive-looking reply inside the shadow session",
          now - 80, session_id="shadow-sess")
    _seed(conn, "user",
          "genuine user preference from the real session",
          now - 60, session_id="real-sess")
    conn.commit()
    pkg["dialectic_miner"].run_mine_pass(force=True)
    cids = [r["source_cid"] for r in conn.execute(
        "SELECT source_cid FROM dialectic_observations").fetchall()]
    assert "real-sess" in cids
    assert "shadow-sess" not in cids
