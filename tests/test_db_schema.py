from __future__ import annotations

import logging
import os
import re
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
    skill_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(skill_usage)")
    }
    assert {
        "availability",
        "unavailable_since",
        "unavailable_prior_state",
    } <= skill_columns
    conn.close()

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("legacy column migration should be version-gated")

    monkeypatch.setattr(db, "_apply_column_migration", fail_if_called)
    conn = db.get_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == (
        db.CURRENT_SCHEMA_VERSION
    )
    conn.close()


_IDEMPOTENT_CREATE = re.compile(
    r"\s*(--[^\n]*\n\s*)*CREATE\s+(VIRTUAL\s+)?(TABLE|INDEX|UNIQUE\s+INDEX|TRIGGER)"
    r"\s+IF\s+NOT\s+EXISTS\b",
    re.IGNORECASE,
)


def test_schema_contains_only_idempotent_creates(fresh_mp):
    db = fresh_mp["db"]

    non_idempotent = [
        statement
        for statement in db._iter_sql_statements(db.SCHEMA)
        if not _IDEMPOTENT_CREATE.match(statement)
    ]

    assert non_idempotent == []


def test_bootstrap_adds_schema_table_missing_from_current_version_db(fresh_mp):
    db = fresh_mp["db"]
    conn = db.get_db()
    conn.execute("DROP TABLE memory_authority")
    conn.commit()
    conn.close()

    db._BOOTSTRAPPED = False
    db.bootstrap_db()

    conn = db.get_db()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (
            db.CURRENT_SCHEMA_VERSION
        )
        restored = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE tbl_name='memory_authority'"
            )
        }
        assert {
            "memory_authority",
            "memory_authority_immutable",
            "idx_memory_authority_visible",
        } <= restored
    finally:
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
    from threadkeeper import db as tk_db

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
    assert raw.execute("PRAGMA user_version").fetchone()[0] == (
        tk_db.CURRENT_SCHEMA_VERSION
    )
    assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    raw.close()


def test_v5_database_without_namespaces_gets_them_and_keeps_memory_global(tmp_path, monkeypatch):
    import sqlite3 as sqlite

    path = tmp_path / "pre-namespace.sqlite"
    conn = sqlite.connect(path)
    conn.executescript(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, question TEXT NOT NULL, "
        "state TEXT NOT NULL, opened_at INTEGER NOT NULL, last_touched_at INTEGER NOT NULL);"
        "CREATE TABLE core_memory (key TEXT PRIMARY KEY, content TEXT NOT NULL, "
        "priority INTEGER NOT NULL DEFAULT 50, updated_at INTEGER NOT NULL);"
        "INSERT INTO threads VALUES ('T1', 'old thread', 'active', 1, 1);"
        "INSERT INTO core_memory VALUES ('rule', 'keep me', 100, 1);"
        "PRAGMA user_version=5;"
    )
    conn.close()
    monkeypatch.setenv("THREADKEEPER_DB", str(path))
    import importlib
    import sys

    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    db = importlib.import_module("threadkeeper.db")

    live = db.get_db()

    thread = live.execute("SELECT memory_scope, project_id FROM threads WHERE id='T1'").fetchone()
    rule = live.execute("SELECT memory_scope FROM core_memory WHERE key='rule'").fetchone()
    assert tuple(thread) == ("global", None)
    assert rule[0] == "global"
    report = live.execute(
        "SELECT global_rows FROM project_scope_migration_report WHERE table_name='threads'"
    ).fetchone()
    assert report[0] == 1
