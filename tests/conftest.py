"""Test isolation. Each test gets a fresh sqlite at $TMPDIR + a stub
~/.claude/projects so live ingest never touches the user's real data."""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest


def _force_clean_env(tmp_root: Path) -> dict[str, str]:
    """Env knobs that must be set before threadkeeper.config imports.

    All background daemons are disabled in tests. Each test calls
    re-import via `del sys.modules` for isolation; daemons started
    inside that re-imported module survive the reload as zombie
    threads (Python keeps them alive as daemon=True) and continue
    pinging sqlite with stale references. Across hundreds of tests
    this becomes DB lock contention and eventually a hang.
    """
    return {
        "THREADKEEPER_DB": str(tmp_root / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_root / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",   # disable bg ingest daemon
        "THREADKEEPER_INGEST_CAP": "0",          # don't ingest at session start
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",  # disable skill_watcher
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",     # disable spawn_budget daemon
        "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",     # disable search_proxy daemon
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",  # disable shadow daemon
        "THREADKEEPER_LESSONS": str(tmp_root / "lessons.md"),  # tempdir lessons
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_root / "tasks"),
        "THREADKEEPER_CLIENT": "pytest",
    }


def _bootstrap_mp(tmp_path, monkeypatch, force_cid: str = ""):
    """Shared bootstrap: clean env, optional FORCE_CID, fresh package import."""
    env = _force_clean_env(tmp_path)
    if force_cid:
        env["THREADKEEPER_FORCE_CID"] = force_cid
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)

    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]

    import threadkeeper.server  # noqa: F401
    from threadkeeper import _mcp, identity, db, brief, config

    return {
        "mcp": _mcp.mcp,
        "identity": identity,
        "db": db,
        "brief": brief,
        "config": config,
        "tmp": tmp_path,
    }


@pytest.fixture()
def fresh_mp(tmp_path, monkeypatch):
    """Re-import the whole threadkeeper package against a clean DB.

    The package keeps process-wide state (FastMCP singleton, _session_id,
    background ingester thread). For test isolation we wipe sys.modules
    of every threadkeeper submodule and re-import. Each test thus gets
    its own DB, its own session, and a clean tool registry.
    """
    return _bootstrap_mp(tmp_path, monkeypatch)


@pytest.fixture()
def mp_with_cid(tmp_path, monkeypatch):
    """Variant of fresh_mp that pins a known self_cid via FORCE_CID env.

    Returns a callable: `mp_with_cid(cid_str)` → bootstrapped pkg dict.
    Use when tests need self_cid-keyed state (tasks.parent_cid, signals
    routing, spawn_hint counters).
    """
    def _build(cid: str):
        return _bootstrap_mp(tmp_path, monkeypatch, force_cid=cid)
    return _build


def all_tool_names_from_mcp(mcp):
    """List registered tool names from FastMCP. The mcp.list_tools is async,
    we use the internal _tool_manager to avoid event-loop boilerplate."""
    tm = mcp._tool_manager
    return sorted(tm._tools.keys())
