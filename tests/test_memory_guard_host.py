"""memory_guard host supervision (Phase 1 daemon-host, Task 6).

With THREADKEEPER_DAEMON_HOST on: thin (non-host) processes are never
idle-retire candidates, and a stale host heartbeat triggers a respawn via
host.ensure_host_running().
"""
from __future__ import annotations
import sys, importlib


def _reimport(monkeypatch, tmp_path, flag="1"):
    for k, v in {"THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
                 "THREADKEEPER_DAEMON_HOST": flag,
                 "THREADKEEPER_DISABLE_BG_DAEMONS": "1"}.items():
        monkeypatch.setenv(k, v)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    importlib.import_module("threadkeeper.server")
    return importlib.import_module("threadkeeper.memory_guard")


def test_thin_not_retired_when_flag_on(monkeypatch, tmp_path):
    mg = _reimport(monkeypatch, tmp_path, flag="1")
    # Realistic process_health.scan() row shape: process_health.classify()
    # never adds a "client" key, so the check must not depend on one.
    procs = [{"pid": 111, "ppid": 1, "rss_kb": 120000,
              "parent_alive": False, "heartbeat_age_s": 99999,
              "is_self": False, "is_orphaned": True}]
    assert mg._idle_retire_candidates(procs) == []   # thin never retired under host mode


def test_stale_host_triggers_respawn(monkeypatch, tmp_path):
    mg = _reimport(monkeypatch, tmp_path, flag="1")
    from threadkeeper import host
    respawned = {"n": 0}
    monkeypatch.setattr(host, "ensure_host_running",
                        lambda: respawned.__setitem__("n", respawned["n"] + 1) or True)
    monkeypatch.setattr(mg, "_host_alive", lambda: False)
    mg.supervise_host()
    assert respawned["n"] == 1
