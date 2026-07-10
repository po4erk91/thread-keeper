# tests/test_host_env.py
"""ensure_host_running() must not let a spawned review child's env markers
leak into the detached daemon-host process (Task 8 fix #1). Left unsanitized,
an inherited THREADKEEPER_SPAWNED_CHILD=1 / non-foreground WRITE_ORIGIN would
make config.BACKGROUND_DAEMONS_ALLOWED false in the host too, so ~13 of its 18
daemon starters would self-gate off — a loop-less zombie host that still binds
the embed socket and heartbeats (looks alive, holds the election lock)."""
from __future__ import annotations
import sys, importlib


def _reimport(monkeypatch, tmp_path, **env_overrides):
    env = {"THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
           "THREADKEEPER_DAEMON_HOST": "1",
           "THREADKEEPER_DISABLE_BG_DAEMONS": "1"}
    env.update(env_overrides)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    importlib.import_module("threadkeeper.server")
    return importlib.import_module("threadkeeper.host")


def test_ensure_host_running_sanitizes_spawned_child_env(monkeypatch, tmp_path):
    # Simulate a spawned review child (a background_review evolve/curator
    # fork, carrying THREADKEEPER_SPAWNED_CHILD=1 + a non-foreground
    # WRITE_ORIGIN) being the first process to call ensure_host_running().
    host = _reimport(
        monkeypatch, tmp_path,
        THREADKEEPER_SPAWNED_CHILD="1",
        THREADKEEPER_WRITE_ORIGIN="background_review",
    )
    monkeypatch.setattr(host, "_host_alive", lambda: False)

    captured: dict = {}

    def fake_popen(args, **kwargs):
        captured["env"] = kwargs.get("env")
        return None

    monkeypatch.setattr(host.subprocess, "Popen", fake_popen)

    assert host.ensure_host_running() is True
    env = captured.get("env")
    assert env is not None
    assert "THREADKEEPER_SPAWNED_CHILD" not in env
    assert env.get("THREADKEEPER_WRITE_ORIGIN") == "foreground"
    assert env.get("THREADKEEPER_ROLE") == "host"
