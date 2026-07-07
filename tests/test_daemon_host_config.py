"""Phase 1 config knobs (daemon-host)."""
from __future__ import annotations
import importlib, sys
from pathlib import Path


def _reimport(monkeypatch, tmp_path, **env):
    base = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "THREADKEEPER_DISABLE_BG_DAEMONS": "1",
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
    }
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    return importlib.import_module("threadkeeper.config")


def test_defaults_are_dark(monkeypatch, tmp_path):
    cfg = _reimport(monkeypatch, tmp_path)
    assert cfg.DAEMON_HOST_ENABLED is False
    assert cfg.PROCESS_ROLE == "server"
    assert cfg.THIN_EMBED_FALLBACK == "fts"
    assert cfg.HOST_SOCK_PATH == (tmp_path / "host.sock")
    assert cfg.HOST_LOCK_PATH == (tmp_path / "host.lock")
    assert cfg.HOST_HEARTBEAT_TTL_S > 0


def test_flag_and_role_and_sock_override(monkeypatch, tmp_path):
    sock = tmp_path / "custom.sock"
    cfg = _reimport(
        monkeypatch, tmp_path,
        THREADKEEPER_DAEMON_HOST="1",
        THREADKEEPER_ROLE="host",
        THREADKEEPER_HOST_SOCK=str(sock),
        THREADKEEPER_THIN_EMBED_FALLBACK="local",
    )
    assert cfg.DAEMON_HOST_ENABLED is True
    assert cfg.PROCESS_ROLE == "host"
    assert cfg.HOST_SOCK_PATH == sock
    assert cfg.THIN_EMBED_FALLBACK == "local"
