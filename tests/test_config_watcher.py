"""Hot-config reload (issue #2): config.reload_settings + config_watcher.

Contract:
  * reload_settings re-reads env and propagates a changed constant into
    every loaded threadkeeper module that imported a copy.
  * config_watcher polls a settings.json, debounces on mtime, and on a real
    change hot-applies the env knobs (the acceptance test: change the shadow
    interval, see shadow_review_status reflect it without a restart).
  * malformed/half-written JSON is skipped (cursor not advanced) and retried.
  * deleting a key reverts the knob to its default.
  * daemon_sleep never busy-spins when an interval is hot-reloaded to 0.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest


def _bootstrap(tmp_path, monkeypatch, *, watch_interval="2", shadow="0"):
    """Fresh re-import of threadkeeper with the watcher pointed at a tmp
    settings.json so the real ~/.claude/settings.json is never touched."""
    settings_json = tmp_path / "settings.json"
    settings_json.write_text(
        json.dumps({"env": {"THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": shadow}})
    )
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_DISABLE_BG_DAEMONS": "1",
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",
        "THREADKEEPER_MEMORY_GUARD_POLL_S": "0",
        "THREADKEEPER_AUTO_UPDATE_INTERVAL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": shadow,
        "THREADKEEPER_CONFIG_WATCH_INTERVAL_S": watch_interval,
        "THREADKEEPER_CONFIG_WATCH_PATH": str(settings_json),
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_FORCE_CID": "aaaa1111-2222-3333-4444-555566667777",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401  (registers tools)
    from threadkeeper import config, config_watcher, shadow_review
    from threadkeeper.tools import shadow_review as shadow_tool
    return {
        "config": config,
        "watcher": config_watcher,
        "shadow_review": shadow_review,
        "shadow_tool": shadow_tool,
        "settings_json": settings_json,
    }


def _write_env(path: Path, env: dict) -> None:
    """Rewrite settings.json and bump mtime so the watcher sees a change."""
    path.write_text(json.dumps({"env": env}))
    # Some filesystems have coarse mtime resolution; nudge it forward.
    st = path.stat()
    import os
    os.utime(path, (st.st_atime, st.st_mtime + 1))


# ── config.reload_settings ────────────────────────────────────────────────

def test_reload_settings_propagates_to_consumer_module(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, shadow="0")
    config = pkg["config"]
    sr = pkg["shadow_review"]
    assert config.SHADOW_REVIEW_INTERVAL_S == 0.0
    assert sr.SHADOW_REVIEW_INTERVAL_S == 0.0

    monkeypatch.setenv("THREADKEEPER_SHADOW_REVIEW_INTERVAL_S", "777")
    changed = config.reload_settings()

    assert "SHADOW_REVIEW_INTERVAL_S" in changed
    assert changed["SHADOW_REVIEW_INTERVAL_S"]["new"] == 777.0
    # The module that did `from .config import SHADOW_REVIEW_INTERVAL_S` sees it.
    assert config.SHADOW_REVIEW_INTERVAL_S == 777.0
    assert sr.SHADOW_REVIEW_INTERVAL_S == 777.0


def test_reload_settings_no_change_returns_empty(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, shadow="60")
    changed = pkg["config"].reload_settings()
    assert changed == {}


# ── config_watcher pass semantics ─────────────────────────────────────────

def test_first_pass_initializes_without_reload(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, shadow="600")
    w = pkg["watcher"]
    assert w._last_mtime is None
    assert w.run_config_watch_pass() == "initialized"
    assert w._last_mtime is not None
    # baseline captured the present key so a later deletion can be reverted
    assert "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S" in w._applied_keys


def test_unchanged_file_is_debounced(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, shadow="600")
    w = pkg["watcher"]
    w.run_config_watch_pass()  # initialize
    assert w.run_config_watch_pass() == "unchanged"


def test_change_hot_applies_new_interval(tmp_path, monkeypatch):
    """The acceptance test from the issue: change the interval in
    settings.json and confirm shadow_review_status reflects it — no restart."""
    pkg = _bootstrap(tmp_path, monkeypatch, shadow="600")
    w = pkg["watcher"]
    w.run_config_watch_pass()  # initialize baseline

    _write_env(pkg["settings_json"],
               {"THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "1234"})
    out = w.run_config_watch_pass()
    assert out.startswith("reloaded changed=")

    assert pkg["config"].SHADOW_REVIEW_INTERVAL_S == 1234.0
    assert pkg["shadow_review"].SHADOW_REVIEW_INTERVAL_S == 1234.0
    # status MCP tool (reads its own imported copy) reflects the new value
    from threadkeeper._mcp import mcp
    status = mcp._tool_manager._tools["shadow_review_status"].fn()
    assert "interval_s=1234" in status


def test_malformed_json_is_skipped_and_retried(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, shadow="600")
    w = pkg["watcher"]
    w.run_config_watch_pass()  # initialize
    cursor_before = w._last_mtime

    # Half-written file: invalid JSON.
    pkg["settings_json"].write_text('{"env": {"THREADKEEPER_')
    st = pkg["settings_json"].stat()
    import os
    os.utime(pkg["settings_json"], (st.st_atime, st.st_mtime + 1))

    out = w.run_config_watch_pass()
    assert out.startswith("parse_error")
    # cursor NOT advanced -> next clean tick still reloads
    assert w._last_mtime == cursor_before


def test_deleting_key_reverts_to_default(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, shadow="600")
    config = pkg["config"]
    w = pkg["watcher"]
    w.run_config_watch_pass()  # initialize (key present, interval 600)
    assert config.SHADOW_REVIEW_INTERVAL_S == 600.0

    # User removes the knob entirely -> revert to the field default (0.0).
    _write_env(pkg["settings_json"], {})
    out = w.run_config_watch_pass()
    assert out.startswith("reloaded")
    assert config.SHADOW_REVIEW_INTERVAL_S == 0.0
    assert "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S" not in w._applied_keys


def test_disabled_watcher_is_noop(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, watch_interval="0", shadow="600")
    w = pkg["watcher"]
    assert w.run_config_watch_pass() == "disabled"
    # force overrides the disable gate
    assert w.run_config_watch_pass(force=True) != "disabled"


# ── daemon enable on interval 0 → >0 ──────────────────────────────────────

def test_enable_transition_starts_daemon(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, shadow="0")
    w = pkg["watcher"]
    w.run_config_watch_pass()  # initialize (shadow disabled)

    calls = []
    monkeypatch.setattr(
        pkg["shadow_review"], "start_shadow_daemon",
        lambda: calls.append("started"),
    )
    _write_env(pkg["settings_json"],
               {"THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "300"})
    w.run_config_watch_pass()
    assert calls == ["started"]


def test_enable_transition_starts_retention_daemon(tmp_path, monkeypatch):
    """Enabling retention live (0 → >0) hot-starts its daemon, same as the
    other loops — no server restart needed to begin pruning."""
    # Ensure a clean 0 baseline regardless of the runner's own environment:
    # a host that already exports THREADKEEPER_RETENTION_INTERVAL_S (e.g. it's
    # set in the developer's ~/.claude/settings.json) would otherwise make the
    # 0→>0 transition invisible at import time.
    monkeypatch.delenv("THREADKEEPER_RETENTION_INTERVAL_S", raising=False)
    pkg = _bootstrap(tmp_path, monkeypatch, shadow="0")
    w = pkg["watcher"]
    w.run_config_watch_pass()  # initialize (retention disabled by default)

    from threadkeeper import retention
    calls = []
    monkeypatch.setattr(
        retention, "start_retention_daemon", lambda: calls.append("started")
    )
    _write_env(pkg["settings_json"],
               {"THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
                "THREADKEEPER_RETENTION_INTERVAL_S": "86400"})
    w.run_config_watch_pass()
    assert calls == ["started"]
    assert pkg["config"].RETENTION_INTERVAL_S == 86400.0


def test_interval_change_does_not_restart_running_daemon(tmp_path, monkeypatch):
    """600 → 900 is not an enable transition; the running loop self-adjusts."""
    pkg = _bootstrap(tmp_path, monkeypatch, shadow="600")
    w = pkg["watcher"]
    w.run_config_watch_pass()

    calls = []
    monkeypatch.setattr(
        pkg["shadow_review"], "start_shadow_daemon",
        lambda: calls.append("started"),
    )
    _write_env(pkg["settings_json"],
               {"THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "900"})
    w.run_config_watch_pass()
    assert calls == []
    assert pkg["config"].SHADOW_REVIEW_INTERVAL_S == 900.0


# ── start_config_watcher guards ───────────────────────────────────────────

def test_start_config_watcher_disabled_when_interval_zero(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, watch_interval="0")
    w = pkg["watcher"]
    w.start_config_watcher()
    assert w._started is False


def test_start_config_watcher_refuses_in_non_foreground(tmp_path, monkeypatch):
    # DISABLE_BG_DAEMONS=1 (set in _bootstrap) => BACKGROUND_DAEMONS_ALLOWED False
    pkg = _bootstrap(tmp_path, monkeypatch, watch_interval="2")
    w = pkg["watcher"]
    assert pkg["config"].BACKGROUND_DAEMONS_ALLOWED is False
    w.start_config_watcher()
    assert w._started is False


# ── daemon_sleep busy-spin guard ──────────────────────────────────────────

def test_daemon_sleep_idles_when_interval_zero(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    from threadkeeper import helpers
    seen = []
    monkeypatch.setattr(helpers.time, "sleep", lambda s: seen.append(s))
    helpers.daemon_sleep(0, idle_s=0.05)
    helpers.daemon_sleep(7.5)
    helpers.daemon_sleep(-3, idle_s=0.05)
    # interval<=0 idles on idle_s (never 0 → no busy-spin); interval>0 sleeps
    # the interval. Each is now jittered by ±_JITTER_FRAC (#86), so the sleeps
    # land within the band around their nominal value rather than exactly on it.
    frac = helpers._JITTER_FRAC
    assert len(seen) == 3
    for got, nominal in zip(seen, [0.05, 7.5, 0.05]):
        assert nominal * (1 - frac) <= got <= nominal * (1 + frac)
        assert got > 0


# ── MCP tools ─────────────────────────────────────────────────────────────

def test_config_reload_tool(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, shadow="600")
    pkg["watcher"].run_config_watch_pass()  # initialize
    _write_env(pkg["settings_json"],
               {"THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "4321"})
    from threadkeeper._mcp import mcp
    out = mcp._tool_manager._tools["config_reload"].fn()
    assert out.startswith("reloaded")
    assert pkg["config"].SHADOW_REVIEW_INTERVAL_S == 4321.0


def test_config_watch_status_tool(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, shadow="600")
    pkg["watcher"].run_config_watch_pass()
    from threadkeeper._mcp import mcp
    out = mcp._tool_manager._tools["config_watch_status"].fn()
    assert "interval_s=" in out
    assert "settings.json" in out
