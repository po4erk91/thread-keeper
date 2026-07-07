from __future__ import annotations

import base64
import json
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError


def _bootstrap(tmp_path, monkeypatch, *, interval="86400", disable_bg="1"):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_AUTO_UPDATE_INTERVAL_S": interval,
        "THREADKEEPER_AUTO_UPDATE_RESTART": "0",
        "THREADKEEPER_SKILL_UPDATE_INTERVAL_S": "0",
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


def _release_metadata(version="0.9.3", filename="threadkeeper-0.9.3.tar.gz"):
    return {
        "info": {"version": version},
        "releases": {
            version: [
                {
                    "filename": filename,
                    "digests": {"sha256": "a" * 64},
                    "yanked": False,
                }
            ]
        },
    }


def _provenance(
    filename="threadkeeper-0.9.3.tar.gz",
    sha256="a" * 64,
    *,
    repository="po4erk91/thread-keeper",
    workflow="publish.yml",
    environment="pypi",
):
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {
                "name": filename,
                "digest": {"sha256": sha256},
            }
        ],
        "predicateType": "https://docs.pypi.org/attestations/publish/v1",
        "predicate": None,
    }
    encoded_statement = base64.b64encode(
        json.dumps(statement).encode("utf-8")
    ).decode("ascii")
    return {
        "version": 1,
        "attestation_bundles": [
            {
                "publisher": {
                    "kind": "GitHub",
                    "repository": repository,
                    "workflow": workflow,
                    "environment": environment,
                    "claims": None,
                },
                "attestations": [
                    {
                        "version": 1,
                        "envelope": {
                            "statement": encoded_statement,
                            "signature": "sig",
                        },
                        "verification_material": {
                            "certificate": "cert",
                            "transparency_entries": [],
                        },
                    }
                ],
            }
        ],
    }


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


def test_auto_update_setup_check_noops_when_config_already_matches(
    tmp_path,
    monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    stdout = """thread-keeper setup (dry-run)
  [dir] ~/.threadkeeper: already exists
  [mcp_server[codex]] codex: already current
  [instructions[codex]] AGENTS.md: managed block already current
  [hooks] hooks: tk-brief.sh already current

Done. Restart connected CLIs for instructions + MCP changes to take effect.
"""

    def fake_run(args, *, cwd=None, timeout=None):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(pkg["auto_update"], "_run", fake_run)

    out = pkg["auto_update"]._run_setup()

    assert out == " setup=checked status=unchanged"
    assert calls == [[sys.executable, "-m", "threadkeeper._setup", "--dry-run"]]


def test_auto_update_setup_check_logs_pending_config_rewrite(
    tmp_path,
    monkeypatch,
    caplog,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    caplog.set_level(logging.WARNING, logger="threadkeeper.auto_update")
    stdout = """thread-keeper setup (dry-run)
  [mcp_server[codex]] codex: would create config.toml with mcp section
  [instructions[codex]] AGENTS.md: would prepend managed block

Done. Restart connected CLIs for instructions + MCP changes to take effect.
"""

    monkeypatch.setattr(
        pkg["auto_update"],
        "_run",
        lambda args, *, cwd=None, timeout=None: SimpleNamespace(
            returncode=0,
            stdout=stdout,
            stderr="",
        ),
    )

    out = pkg["auto_update"]._run_setup()

    assert out == " setup=checked status=changes_pending"
    assert any(
        "setup dry-run found pending CLI config changes" in rec.getMessage()
        for rec in caplog.records
        if rec.name == "threadkeeper.auto_update"
    )


def test_auto_update_setup_apply_mode_runs_full_setup(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    calls: list[list[str]] = []

    def fake_run(args, *, cwd=None, timeout=None):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pkg["auto_update"], "AUTO_UPDATE_SETUP", "apply")
    monkeypatch.setattr(pkg["auto_update"], "_run", fake_run)

    out = pkg["auto_update"]._run_setup()

    assert out == " setup=ok"
    assert calls == [[sys.executable, "-m", "threadkeeper._setup"]]


def test_auto_update_setup_skip_mode_avoids_subprocess(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)

    monkeypatch.setattr(pkg["auto_update"], "AUTO_UPDATE_SETUP", "skip")
    monkeypatch.setattr(
        pkg["auto_update"],
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("setup subprocess should not run")
        ),
    )

    assert pkg["auto_update"]._run_setup() == " setup=skipped mode=skip"


def test_setup_dry_run_detects_legacy_migration_status(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)

    assert pkg["auto_update"]._setup_dry_run_would_change(
        "  [mcp_server[copilot]] copilot: migrated legacy schema + updated thread-keeper\n"
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
    monkeypatch.setattr(
        pkg["auto_update"],
        "_pypi_project_metadata",
        lambda: _release_metadata(),
    )
    monkeypatch.setattr(
        pkg["auto_update"],
        "_fetch_pypi_provenance",
        lambda version, filename: _provenance(filename),
    )
    monkeypatch.setattr(pkg["auto_update"], "_run", fake_run)
    monkeypatch.setattr(pkg["auto_update"], "_run_setup", lambda: " setup=ok")

    out = pkg["auto_update"]._update_installed_package()

    assert out == "updated mode=pip old=0.9.2 new=0.9.3 setup=ok"
    assert calls == [
        [sys.executable, "-m", "pip", "install", "--upgrade", "threadkeeper"]
    ]


def test_pip_update_refuses_missing_provenance_and_suppresses_restart(
    tmp_path,
    monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    scheduled = {"value": False}
    pip_calls: list[list[str]] = []

    def fake_run(args, *, cwd=None, timeout=None):
        pip_calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def missing_provenance(version, filename):
        raise HTTPError("https://pypi.org/provenance", 404, "Not Found", None, None)

    monkeypatch.setattr(pkg["auto_update"], "_is_git_checkout", lambda repo: False)
    monkeypatch.setattr(pkg["auto_update"], "_installed_version", lambda: "0.9.2")
    monkeypatch.setattr(
        pkg["auto_update"],
        "_pypi_project_metadata",
        lambda: _release_metadata(),
    )
    monkeypatch.setattr(
        pkg["auto_update"],
        "_fetch_pypi_provenance",
        missing_provenance,
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

    assert out == (
        "refused mode=pip version=0.9.3 "
        "reason=provenance_missing file=threadkeeper-0.9.3.tar.gz"
    )
    assert scheduled["value"] is False
    assert pip_calls == []
    row = pkg["db"].get_db().execute(
        "SELECT summary FROM events WHERE kind='auto_update_pass' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["summary"] == out


def test_pip_update_refuses_mismatched_provenance(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    scheduled = {"value": False}
    pip_calls: list[list[str]] = []

    monkeypatch.setattr(pkg["auto_update"], "_is_git_checkout", lambda repo: False)
    monkeypatch.setattr(pkg["auto_update"], "_installed_version", lambda: "0.9.2")
    monkeypatch.setattr(
        pkg["auto_update"],
        "_pypi_project_metadata",
        lambda: _release_metadata(),
    )
    monkeypatch.setattr(
        pkg["auto_update"],
        "_fetch_pypi_provenance",
        lambda version, filename: _provenance(
            filename,
            repository="attacker/thread-keeper",
        ),
    )
    monkeypatch.setattr(
        pkg["auto_update"],
        "_run",
        lambda args, *, cwd=None, timeout=None: pip_calls.append(args),
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
        "refused mode=pip version=0.9.3 "
        "reason=publisher_mismatch file=threadkeeper-0.9.3.tar.gz"
    )
    assert scheduled["value"] is False
    assert pip_calls == []
    row = pkg["db"].get_db().execute(
        "SELECT summary FROM events WHERE kind='auto_update_pass' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["summary"] == out


def test_daemon_does_not_start_when_background_daemons_disabled(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="86400", disable_bg="1")

    pkg["auto_update"].start_auto_update_daemon()

    assert pkg["auto_update"]._started is False
