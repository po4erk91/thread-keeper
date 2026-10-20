"""Server-side MCP tool profiles and deferred capability discovery."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _unload_profiled_package_after_test():
    yield
    for name in [name for name in sys.modules if name.startswith("threadkeeper")]:
        del sys.modules[name]


def _load_profile(tmp_path, monkeypatch, profile: str, **overrides):
    monkeypatch.setenv("THREADKEEPER_DB", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("THREADKEEPER_TASK_LOG_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("THREADKEEPER_DISABLE_BG_DAEMONS", "1")
    monkeypatch.setenv("THREADKEEPER_NO_EMBEDDINGS", "1")
    monkeypatch.setenv("THREADKEEPER_TOOL_PROFILE", profile)
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    Path(tmp_path / "projects").mkdir()
    for name in [name for name in sys.modules if name.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import _mcp

    return {"mcp": _mcp.mcp}


def test_core_profile_advertises_only_the_daily_memory_workflow(tmp_path, monkeypatch):
    pkg = _load_profile(tmp_path, monkeypatch, "core")
    from threadkeeper.tool_profiles import PROFILE_TOOL_NAMES

    mcp = pkg["mcp"]
    listed = asyncio.run(mcp.list_tools())
    listed_names = {tool.name for tool in listed}

    assert listed_names == PROFILE_TOOL_NAMES["core"]
    assert {"brief", "context", "open_thread", "note", "search", "wait"} <= listed_names
    assert "evolve_apply" not in listed_names
    assert "curator_run" not in listed_names
    assert "sync_now" not in listed_names
    assert "register_probe" not in listed_names
    assert "db_compact" not in listed_names
    with pytest.raises(Exception, match="Unknown tool"):
        asyncio.run(mcp.call_tool("evolve_apply", {}))


def test_full_profile_preserves_the_complete_registry(tmp_path, monkeypatch):
    pkg = _load_profile(tmp_path, monkeypatch, "full")
    names = set(pkg["mcp"]._tool_manager._tools)

    assert {"evolve_apply", "curator_run", "sync_now", "db_compact"} <= names
    assert "capability_search" in names


@pytest.mark.parametrize("profile", ["coordination", "memory-admin", "learning-loops"])
def test_named_profile_membership_is_pinned(tmp_path, monkeypatch, profile):
    pkg = _load_profile(tmp_path, monkeypatch, profile)
    from threadkeeper.tool_profiles import PROFILE_TOOL_NAMES

    assert set(pkg["mcp"]._tool_manager._tools) == PROFILE_TOOL_NAMES[profile]


def test_overrides_are_exact_and_fail_closed():
    from threadkeeper.tool_profiles import ToolProfileError, resolve_tool_names

    available = {"brief", "evolve_apply", "note"}
    assert resolve_tool_names(
        "core", include="evolve_apply", exclude="note", available_names=available,
    ) == {"brief", "evolve_apply"}
    with pytest.raises(ToolProfileError, match="unknown tool override"):
        resolve_tool_names("core", include="not_a_tool", available_names=available)
    with pytest.raises(ToolProfileError, match="both included and excluded"):
        resolve_tool_names(
            "core", include="note", exclude="note", available_names=available,
        )


def test_invalid_server_override_does_not_start_a_wider_registry(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="unknown tool override"):
        _load_profile(
            tmp_path, monkeypatch, "core",
            THREADKEEPER_TOOL_INCLUDE="not_a_tool",
        )


def test_child_profile_is_intersected_with_its_parent_surface():
    from threadkeeper.tool_profiles import (
        DECLARED_TOOL_NAMES,
        TOOL_PARENT_ALLOWLIST_ENV,
        TOOL_PROFILE_ENV,
        child_profile_environment,
        resolve_tool_names,
    )

    parent_tools = {"brief", "context", "spawn"}
    child_env = child_profile_environment(parent_tools, "evolve_applier")
    child_tools = resolve_tool_names(
        child_env[TOOL_PROFILE_ENV],
        parent_allowlist=child_env[TOOL_PARENT_ALLOWLIST_ENV],
        available_names=DECLARED_TOOL_NAMES | parent_tools,
    )

    assert child_env[TOOL_PROFILE_ENV] == "full"
    assert child_tools == parent_tools
    assert child_tools <= parent_tools


@pytest.mark.parametrize(("role", "profile"), [
    ("archivist", "learning-loops"),
    ("probe_runner", "learning-loops"),
    ("evolve_researcher", "full"),
    ("memory_admin", "memory-admin"),
])
def test_spawn_roles_choose_their_smallest_profile(role, profile):
    from threadkeeper.tool_profiles import child_profile_for_role

    assert child_profile_for_role(role) == profile


def test_setup_registers_new_hosts_with_core_profile(monkeypatch):
    from threadkeeper import _setup, adapters

    captured = {}

    class Adapter:
        name = "test"

        def register_mcp_server(self, **kwargs):
            captured.update(kwargs)
            return "ok"

    monkeypatch.setattr(adapters, "installed_adapters", lambda: [Adapter()])
    _setup.install_mcp_servers(dry_run=True)

    assert captured["env"]["THREADKEEPER_TOOL_PROFILE"] == "core"


def test_capability_search_returns_bounded_family_metadata(tmp_path, monkeypatch):
    pkg = _load_profile(tmp_path, monkeypatch, "core")
    search = pkg["mcp"]._tool_manager._tools["capability_search"].fn

    result = search("evolve")

    assert result == {
        "query": "evolve",
        "matches": [{
            "family": "evolve automation",
            "summary": "Review and apply roadmap-backed repository improvements.",
            "profile": "full",
        }],
        "truncated": False,
    }
    assert "evolve_apply" not in str(result)
