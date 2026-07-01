from __future__ import annotations

import os
import stat
import sys

import pytest


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX mode bits are not portable on this platform",
)


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_get_db_hardens_default_threadkeeper_storage(tmp_path, monkeypatch):
    home = tmp_path / "home"
    tk_dir = home / ".threadkeeper"
    curator_dir = tk_dir / "curator"
    curator_dir.mkdir(parents=True)

    env_file = tk_dir / ".env"
    env_file.write_text("THREADKEEPER_CLIENT=pytest\n", encoding="utf-8")
    db_file = tk_dir / "db.sqlite"
    db_file.write_bytes(b"")
    report = curator_dir / "REPORT-20260624T000000.md"
    report.write_text("report\n", encoding="utf-8")

    tk_dir.chmod(0o755)
    db_file.chmod(0o644)
    env_file.chmod(0o644)
    report.chmod(0o644)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("THREADKEEPER_DB", raising=False)
    monkeypatch.delenv("THREADKEEPER_ENV_FILE", raising=False)
    monkeypatch.delenv("THREADKEEPER_CURATOR_REPORTS_DIR", raising=False)
    monkeypatch.setenv("THREADKEEPER_NO_EMBEDDINGS", "1")
    monkeypatch.setenv("THREADKEEPER_DISABLE_BG_DAEMONS", "1")

    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]

    from threadkeeper import config, db

    conn = db.get_db()
    try:
        sidecars = [
            config.DB_PATH.with_name(config.DB_PATH.name + "-wal"),
            config.DB_PATH.with_name(config.DB_PATH.name + "-shm"),
        ]

        assert _mode(tk_dir) == 0o700
        assert _mode(config.DB_PATH) == 0o600
        assert _mode(env_file) == 0o600
        assert _mode(report) == 0o600
        for path in sidecars:
            assert path.exists()
            assert _mode(path) == 0o600
    finally:
        conn.close()


def test_setup_hardens_threadkeeper_dir(tmp_path, monkeypatch):
    home = tmp_path / "setup-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]

    from threadkeeper import _setup

    assert "created" in _setup.install_tk_dir(dry_run=False)
    tk_dir = home / ".threadkeeper"
    assert _mode(tk_dir) == 0o700

    tk_dir.chmod(0o755)
    assert "already exists" in _setup.install_tk_dir(dry_run=False)
    assert _mode(tk_dir) == 0o700
