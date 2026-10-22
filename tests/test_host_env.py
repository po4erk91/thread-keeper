# tests/test_host_env.py
"""ensure_host_running() must not let a spawned review child's env markers
leak into the detached daemon-host process (Task 8 fix #1). Left unsanitized,
an inherited THREADKEEPER_SPAWNED_CHILD=1 / non-foreground WRITE_ORIGIN would
make config.BACKGROUND_DAEMONS_ALLOWED false in the host too, so ~13 of its 18
daemon starters would self-gate off — a loop-less zombie host that still binds
the embed socket and heartbeats (looks alive, holds the election lock)."""
from __future__ import annotations
import sys, importlib, uuid


def _reimport(monkeypatch, tmp_path, **env_overrides):
    for key in (
        "THREADKEEPER_ROLE",
        "THREADKEEPER_SPAWNED_CHILD",
        "THREADKEEPER_WRITE_ORIGIN",
    ):
        monkeypatch.delenv(key, raising=False)
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
    # The test env keeps THREADKEEPER_DISABLE_BG_DAEMONS=1 so the import stays
    # quiet; clear the derived flag so ensure_host_running() reaches the spawn
    # whose env this test is about.
    monkeypatch.setattr(host.config, "DISABLE_BG_DAEMONS", False)
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


def test_ensure_host_running_skips_when_daemons_disabled(monkeypatch, tmp_path):
    """An explicit operator pause must not spawn a (loop-less) host.

    The spawned-host env sanitization below deliberately clears
    THREADKEEPER_DISABLE_BG_DAEMONS for the child, so the pause gate has to
    fire before the spawn — otherwise the menu-bar power button would be a
    no-op under daemon-host mode."""
    host = _reimport(monkeypatch, tmp_path)
    assert host.config.DISABLE_BG_DAEMONS is True  # from the test env
    monkeypatch.setattr(host, "_host_alive", lambda: False)

    def _boom(*args, **kwargs):
        raise AssertionError("paused install must not spawn a host")

    monkeypatch.setattr(host.subprocess, "Popen", _boom)

    assert host.ensure_host_running() is False


_CHILD_CID = "11111111-2222-3333-4444-555555555555"
_PER_CHILD_IDENTITY = {
    "THREADKEEPER_CURATOR_PASS_ID": "20261012T101949",
    "THREADKEEPER_CURATOR_SNAPSHOT_DIR": "/tmp/curator-snapshot",
    "THREADKEEPER_MODEL_ROUTER_PASS_ID": "mr-1-abc123",
    "THREADKEEPER_EGRESS_CONSUMER": "codex",
}


def _host_spawn_env(monkeypatch, tmp_path, **env_overrides):
    host = _reimport(monkeypatch, tmp_path, **env_overrides)
    monkeypatch.setattr(host.config, "DISABLE_BG_DAEMONS", False)
    monkeypatch.setattr(host, "_host_alive", lambda: False)
    captured: dict = {}
    monkeypatch.setattr(host.subprocess, "Popen",
                        lambda args, **kwargs: captured.setdefault("env", kwargs["env"]))
    assert host.ensure_host_running() is True
    return captured["env"]


def test_ensure_host_running_drops_per_child_identity(monkeypatch, tmp_path):
    # A curator or router child's thin MCP server can be the process that
    # spawns the host; its pass identity must not become the host's default.
    env = _host_spawn_env(monkeypatch, tmp_path, **_PER_CHILD_IDENTITY)
    leaked = sorted(key for key in _PER_CHILD_IDENTITY if key in env)
    assert leaked == []
    assert env["THREADKEEPER_DB"] == str(tmp_path / "db.sqlite")


def test_ensure_host_running_gives_the_host_its_own_cid(monkeypatch, tmp_path):
    env = _host_spawn_env(monkeypatch, tmp_path, THREADKEEPER_FORCE_CID=_CHILD_CID)
    host_cid = env["THREADKEEPER_FORCE_CID"]
    assert host_cid != _CHILD_CID
    assert uuid.UUID(host_cid).version == 4

