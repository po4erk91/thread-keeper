"""core_memory tier (Letta-style RAM): always-present brief lines."""
from __future__ import annotations


def _tools(fresh_mp):
    return fresh_mp["mcp"]._tool_manager._tools


def test_core_set_get_list(fresh_mp):
    t = _tools(fresh_mp)
    assert t["core_set"].fn(key="user_role", content="data scientist", priority=90).startswith("ok")
    out = t["core_get"].fn(key="user_role")
    assert "data scientist" in out
    listed = t["core_list"].fn()
    assert "user_role" in listed


def test_core_set_overwrites(fresh_mp):
    t = _tools(fresh_mp)
    t["core_set"].fn(key="x", content="v1", priority=50)
    t["core_set"].fn(key="x", content="v2", priority=80)
    out = t["core_get"].fn(key="x")
    assert "v2" in out
    assert "v1" not in out


def test_core_remove(fresh_mp):
    t = _tools(fresh_mp)
    t["core_set"].fn(key="ephemeral", content="will be gone", priority=10)
    t["core_remove"].fn(key="ephemeral")
    out = t["core_get"].fn(key="ephemeral")
    # Either an error string or an empty marker — both indicate "not there"
    assert "ephemeral" not in out or "not_found" in out.lower() or "no_" in out.lower()


def test_core_appears_in_brief(fresh_mp):
    t = _tools(fresh_mp)
    t["core_set"].fn(key="probe_key", content="THIS_MUST_APPEAR", priority=99)
    brief = t["brief"].fn()
    assert "THIS_MUST_APPEAR" in brief or "probe_key" in brief
