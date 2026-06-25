from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace


def _bootstrap(tmp_path, monkeypatch, *, interval="86400", disable_bg="1"):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_AUTO_UPDATE_INTERVAL_S": interval,
        "THREADKEEPER_AUTO_UPDATE_RESTART": "0",
        "THREADKEEPER_DISABLE_BG_DAEMONS": disable_bg,
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
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_NO_EMBEDDINGS": "1",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import auto_update, db

    return {"auto_update": auto_update, "db": db}


def test_disabled_without_force(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="0")

    assert pkg["auto_update"].run_auto_update_pass() == "disabled"


def test_force_pass_records_event(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="86400")
    monkeypatch.setattr(
        pkg["auto_update"],
        "_request_and_apply_update",
        lambda: "no_update mode=test",
    )

    out = pkg["auto_update"].run_auto_update_pass(
        force=True,
        restart_on_update=False,
    )

    assert out == "no_update mode=test"
    row = pkg["db"].get_db().execute(
        "SELECT summary FROM events WHERE kind='auto_update_pass' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["summary"] == "no_update mode=test"


def test_healthy_update_runs_smoke_and_schedules_restart(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="86400")
    scheduled = {"value": False}
    calls: list[list[str]] = []

    def fake_run(args, *, cwd=None, timeout=None):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        pkg["auto_update"],
        "_request_and_apply_update",
        lambda: "updated mode=test setup=ok",
    )
    monkeypatch.setattr(pkg["auto_update"], "_run", fake_run)
    monkeypatch.setattr(
        pkg["auto_update"],
        "_schedule_restart",
        lambda: scheduled.__setitem__("value", True),
    )

    out = pkg["auto_update"].run_auto_update_pass(
        force=True,
        restart_on_update=True,
    )

    assert out == "updated mode=test setup=ok smoke=ok"
    assert scheduled["value"] is True
    assert calls == [
        [sys.executable, "-c", pkg["auto_update"].SMOKE_IMPORT_CODE],
    ]


def test_smoke_failure_suppresses_restart_and_records_failure(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="86400")
    scheduled = {"value": False}

    monkeypatch.setattr(
        pkg["auto_update"],
        "_request_and_apply_update",
        lambda: "updated mode=test setup=ok",
    )
    monkeypatch.setattr(
        pkg["auto_update"],
        "_run",
        lambda args, *, cwd=None, timeout=None: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="broken import",
        ),
    )
    monkeypatch.setattr(
        pkg["auto_update"],
        "_schedule_restart",
        lambda: scheduled.__setitem__("value", True),
    )

    out = pkg["auto_update"].run_auto_update_pass(
        force=True,
        restart_on_update=True,
    )

    assert out == (
        "updated mode=test setup=ok smoke=failed err=broken import "
        "restart=suppressed"
    )
    assert scheduled["value"] is False
    row = pkg["db"].get_db().execute(
        "SELECT summary FROM events WHERE kind='auto_update_pass' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["summary"] == out


def test_setup_failure_suppresses_restart_and_records_failure(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="86400")
    scheduled = {"value": False}
    smoke_called = {"value": False}

    monkeypatch.setattr(
        pkg["auto_update"],
        "_request_and_apply_update",
        lambda: "updated mode=test setup=failed err=boom",
    )
    monkeypatch.setattr(
        pkg["auto_update"],
        "_run_post_update_smoke_check",
        lambda: smoke_called.__setitem__("value", True) or " smoke=ok",
    )
    monkeypatch.setattr(
        pkg["auto_update"],
        "_schedule_restart",
        lambda: scheduled.__setitem__("value", True),
    )

    out = pkg["auto_update"].run_auto_update_pass(
        force=True,
        restart_on_update=True,
    )

    assert out == "updated mode=test setup=failed err=boom restart=suppressed"
    assert smoke_called["value"] is False
    assert scheduled["value"] is False
    row = pkg["db"].get_db().execute(
        "SELECT summary FROM events WHERE kind='auto_update_pass' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["summary"] == out


def test_recent_pass_makes_daemon_tick_not_due(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="86400")
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES ('s', 'auto_update_pass', '', 'no_update', ?)",
        (int(time.time()),),
    )
    conn.commit()
    called = {"value": False}

    def fake_update():
        called["value"] = True
        return "updated mode=test"

    monkeypatch.setattr(pkg["auto_update"], "_request_and_apply_update", fake_update)

    out = pkg["auto_update"].run_auto_update_pass(restart_on_update=False)

    assert out.startswith("not_due age_s=")
    assert called["value"] is False


def test_git_checkout_with_dirty_tracked_files_is_skipped(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)

    def fake_git_stdout(repo, *args, timeout=60):
        if args[:2] == ("status", "--porcelain"):
            return 0, " M threadkeeper/server.py", ""
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(pkg["auto_update"], "_git_stdout", fake_git_stdout)

    assert (
        pkg["auto_update"]._update_git_checkout(tmp_path)
        == "skipped_dirty_checkout mode=git"
    )


def test_pip_update_runs_setup_when_version_changes(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    versions = iter(["0.9.2", "0.9.3"])
    calls: list[list[str]] = []

    def fake_run(args, *, cwd=None, timeout=None):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pkg["auto_update"], "_installed_version", lambda: next(versions))
    monkeypatch.setattr(pkg["auto_update"], "_package_spec", lambda: "threadkeeper")
    monkeypatch.setattr(pkg["auto_update"], "_run", fake_run)
    monkeypatch.setattr(pkg["auto_update"], "_run_setup", lambda: " setup=ok")

    out = pkg["auto_update"]._update_installed_package()

    assert out == "updated mode=pip old=0.9.2 new=0.9.3 setup=ok"
    assert calls == [
        [sys.executable, "-m", "pip", "install", "--upgrade", "threadkeeper"]
    ]


def test_daemon_does_not_start_when_background_daemons_disabled(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="86400", disable_bg="1")

    pkg["auto_update"].start_auto_update_daemon()

    assert pkg["auto_update"]._started is False
