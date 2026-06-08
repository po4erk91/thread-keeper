"""dialectic_validator — spawns an opus child that turns pending observations
into claims. Tests the scaffolding (threshold, spawn kwargs, lifecycle); the
LLM decision itself runs in production."""
from __future__ import annotations

import sys
import time
from pathlib import Path

_FAKE_CID = "aaaa1111-2222-3333-4444-555566667777"


def _bootstrap(tmp_path, monkeypatch, interval="0", min_n="5"):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CURATOR_INTERVAL_S": "0",
        "THREADKEEPER_EXTRACT_INTERVAL_S": "0",
        "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_DIALECTIC_MINE_INTERVAL_S": "0",
        "THREADKEEPER_DIALECTIC_VALIDATE_INTERVAL_S": interval,
        "THREADKEEPER_DIALECTIC_VALIDATE_MIN": min_n,
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
    from threadkeeper import db, dialectic_validator, identity
    return {"db": db, "dialectic_validator": dialectic_validator, "identity": identity}


def _seed_obs(conn, quote, age_s=60, status="pending"):
    now = int(time.time())
    conn.execute(
        "INSERT INTO dialectic_observations (dialog_uuid, user_quote, context, "
        "source_cid, status, created_at) VALUES (?,?,?,?,?,?)",
        (f"u-{now}-{abs(hash(quote)) % 99999}", quote, "ctx", "real-sess",
         status, now - age_s),
    )


def test_disabled_without_force(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    assert pkg["dialectic_validator"].run_validate_pass() == "disabled"


def test_below_threshold_no_spawn(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, min_n="5")
    conn = pkg["db"].get_db()
    for i in range(3):
        _seed_obs(conn, f"obs {i}")
    conn.commit()
    out = pkg["dialectic_validator"].run_validate_pass(force=True)
    assert out.startswith("below_threshold")
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='dialectic_validate_pass'"
    ).fetchone()[0]
    assert n == 1


def test_spawns_when_threshold_met(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, min_n="3")
    conn = pkg["db"].get_db()
    for i in range(4):
        _seed_obs(conn, f"user preference number {i}")
    conn.commit()

    import threadkeeper.tools.spawn as spawn_mod
    captured: list[dict] = []

    def fake_spawn(**kwargs):
        captured.append(kwargs)
        return "spawn task_id=fake-validator pid=0"

    monkeypatch.setattr(spawn_mod, "spawn", fake_spawn)

    out = pkg["dialectic_validator"].run_validate_pass(force=True)
    assert "fake-validator" in out
    assert len(captured) == 1
    kw = captured[0]
    assert kw["slim"] is True
    assert kw["visible"] is False
    assert kw["capture_output"] is True
    assert kw["permission_mode"] == "auto"
    assert kw["role"] == "dialectic_validator"
    assert kw["write_origin"] == "background_review"
    assert "DIALECTIC VALIDATOR" in kw["prompt"]
    assert "PENDING OBSERVATIONS (n=4)" in kw["prompt"]
    assert "user preference number 0" in kw["prompt"]
    allowed = kw["extra_allowed_tools"]
    assert "dialectic_claim" in allowed
    assert "dialectic_evidence" in allowed
    assert "dialectic_observation_resolve" in allowed
    assert "Bash" not in allowed


def test_excludes_processed_and_stale(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, min_n="1")
    conn = pkg["db"].get_db()
    _seed_obs(conn, "fresh pending", age_s=60, status="pending")
    _seed_obs(conn, "already done", age_s=60, status="processed")
    _seed_obs(conn, "ancient pending", age_s=40 * 86400, status="pending")
    conn.commit()
    dump, n = pkg["dialectic_validator"]._collect_pending(conn)
    assert n == 1
    assert "fresh pending" in dump
    assert "already done" not in dump
    assert "ancient pending" not in dump


def test_daemon_does_not_start_in_slim_child(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="3600")
    import threadkeeper.config as cfg
    monkeypatch.setattr(cfg, "SEMANTIC_AVAILABLE", False)
    pkg["dialectic_validator"]._started = False
    pkg["dialectic_validator"].start_dialectic_validator_daemon()
    assert pkg["dialectic_validator"]._started is False


def test_daemon_disabled_at_interval_zero(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="0")
    pkg["dialectic_validator"]._started = False
    pkg["dialectic_validator"].start_dialectic_validator_daemon()
    assert pkg["dialectic_validator"]._started is False
