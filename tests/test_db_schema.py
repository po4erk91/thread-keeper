from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest


def test_get_db_sets_user_version_and_skips_legacy_alters(
    fresh_mp, monkeypatch
):
    db = fresh_mp["db"]

    conn = db.get_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == (
        db.CURRENT_SCHEMA_VERSION
    )
    conn.close()

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("legacy column migration should be version-gated")

    monkeypatch.setattr(db, "_apply_column_migration", fail_if_called)
    conn = db.get_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == (
        db.CURRENT_SCHEMA_VERSION
    )
    conn.close()


def test_non_duplicate_column_migration_error_surfaces(
    fresh_mp, monkeypatch, caplog
):
    db = fresh_mp["db"]
    bad_ddl = "ALTER TABLE missing_table ADD COLUMN bad_column TEXT"
    monkeypatch.setattr(db, "LEGACY_COLUMN_MIGRATIONS", (bad_ddl,))

    with caplog.at_level(logging.WARNING, logger="threadkeeper.db"):
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            db.get_db()

    assert "SQLite schema migration DDL failed" in caplog.text
    assert bad_ddl in caplog.text


def _subprocess_env(tmp_path: Path) -> dict[str, str]:
    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update(
        {
            "THREADKEEPER_DB": str(tmp_path / "concurrent.sqlite"),
            "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
            "THREADKEEPER_DISABLE_BG_DAEMONS": "1",
            "THREADKEEPER_AUTO_UPDATE_INTERVAL_S": "0",
            "THREADKEEPER_SKILL_UPDATE_INTERVAL_S": "0",
            "THREADKEEPER_INGEST_INTERVAL_S": "0",
            "THREADKEEPER_INGEST_CAP": "0",
            "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
            "THREADKEEPER_CLIENT": "pytest",
        }
    )
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo), env["PYTHONPATH"]] if env.get("PYTHONPATH") else [str(repo)]
    )
    return env


def test_concurrent_get_db_schema_migration_is_safe(tmp_path):
    code = """
import os
import time
from threadkeeper import db

if os.environ.get("TK_SLOW_SCHEMA") == "1":
    original = db._run_schema_migrations

    def slow_schema_migrations(conn, from_version):
        time.sleep(0.5)
        return original(conn, from_version)

    db._run_schema_migrations = slow_schema_migrations

conn = db.get_db()
version = conn.execute("PRAGMA user_version").fetchone()[0]
retry_col = conn.execute(
    "SELECT COUNT(*) FROM pragma_table_info('tasks') "
    "WHERE name='retry_attempt'"
).fetchone()[0]
print(f"version={version} retry_attempt={retry_col}")
"""
    env = _subprocess_env(tmp_path)
    repo = Path(__file__).resolve().parents[1]
    slow_env = {**env, "TK_SLOW_SCHEMA": "1"}

    first = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=repo,
        env=slow_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.1)
    second = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    first_out, first_err = first.communicate(timeout=10)
    second_out, second_err = second.communicate(timeout=10)

    assert first.returncode == 0, first_err
    assert second.returncode == 0, second_err
    assert "retry_attempt=1" in first_out
    assert "retry_attempt=1" in second_out

    raw = sqlite3.connect(env["THREADKEEPER_DB"])
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
    assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    raw.close()
