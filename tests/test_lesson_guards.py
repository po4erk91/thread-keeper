"""Lesson guards need owner review and proof at the real MCP write boundary."""
from __future__ import annotations

import json

import pytest


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


@pytest.fixture(autouse=True)
def _local_guard_test(fresh_mp, monkeypatch):
    from threadkeeper.tools import threads
    monkeypatch.setattr(threads, "_embed", lambda _text: None)
    monkeypatch.setenv("THREADKEEPER_PROJECT_NAMESPACE", "prj_aaaaaaaaaaaaaaaaaaaa")


def _row(pkg, slug):
    with pkg["db"].read_db() as conn:
        return dict(conn.execute("SELECT * FROM lesson_guards WHERE slug=?", (slug,)).fetchone())


def _propose(pkg, slug, value="unsafe", **kwargs):
    return _tool(pkg, "lesson_guard_manage")(
        action="propose", slug=slug,
        trigger={"tool": "note", "field": "content", "operator": "equals", "value": value}, **kwargs,
    )


def _active(pkg):
    slug = _eligible(pkg)
    tid = _tool(pkg, "open_thread")(question="Guard boundary test")
    assert "state=proposed" in _propose(pkg, slug)
    manage = _tool(pkg, "lesson_guard_manage")
    assert "state=verified" in manage(
        action="verify", slug=slug, expected_revision=1,
        allow_payload={"thread_id": tid, "content": "safe"},
        deny_payload={"thread_id": tid, "content": "unsafe"},
    )
    assert "state=active" in manage(action="activate", slug=slug, expected_revision=2, owner_confirm=True)
    return slug, tid


def _eligible(pkg):
    from threadkeeper import lessons
    from threadkeeper.lesson_violations import record_violation
    lessons.append_lesson(title="Block the unsafe marker", body="Do not save the unsafe marker.", source="shadow")
    slug = next(lessons.iter_lessons())["slug"]
    conn = pkg["db"].get_db()
    for index in range(3):
        record_violation(conn, slug, "synthetic repeat", session_id=f"s-{index}", origin="foreground")
    conn.close()
    return slug


def test_unknown_lesson_cannot_create_guard(fresh_mp):
    result = _tool(fresh_mp, "lesson_guard_manage")(
        action="propose", slug="missing", trigger={"tool": "note", "field": "content", "operator": "equals", "value": "unsafe"},
    )
    assert result == "ERR unknown_lesson"


def test_ordinary_lesson_cannot_create_guard(fresh_mp):
    from threadkeeper import lessons
    lessons.append_lesson(title="Ordinary rule", body="Remember this rule.", source="shadow")
    slug = next(lessons.iter_lessons())["slug"]
    result = _tool(fresh_mp, "lesson_guard_manage")(
        action="propose", slug=slug, trigger={"tool": "note", "field": "content", "operator": "equals", "value": "unsafe"},
    )
    assert result == "ERR lesson_not_memory_insufficient"


def test_verified_guard_denies_real_tool_and_allows_control(fresh_mp):
    slug, tid = _active(fresh_mp)
    note = _tool(fresh_mp, "note")
    assert note(thread_id=tid, content="unsafe") == f"ERR lesson_guard_denied slug={slug}"
    assert note(thread_id=tid, content="safe").startswith("ok")
    with fresh_mp["db"].read_db() as conn:
        assert [r[0] for r in conn.execute("SELECT content FROM notes")] == ["safe"]
        row = _row(fresh_mp, slug)
        assert all(json.loads(row["verification_json"]).values())
        assert row["verified_at"] and row["runtime_digest"] and row["lesson_digest"]


def test_proposal_is_inert_and_activation_requires_proof_and_owner(fresh_mp):
    slug = _eligible(fresh_mp)
    tid = _tool(fresh_mp, "open_thread")(question="Proposal is not enforcement")
    assert "state=proposed" in _propose(fresh_mp, slug)
    assert _tool(fresh_mp, "note")(thread_id=tid, content="unsafe").startswith("ok")
    manage = _tool(fresh_mp, "lesson_guard_manage")
    assert manage(action="activate", slug=slug, expected_revision=1) == "ERR owner_confirmation_required"
    assert manage(action="activate", slug=slug, expected_revision=1, owner_confirm=True) == "ERR verified_guard_required"
    assert manage(action="verify", slug=slug, expected_revision=1) == "ERR preflight_cases_required"


def test_bad_preflight_is_persisted_without_payload_samples(fresh_mp):
    slug = _eligible(fresh_mp)
    tid = _tool(fresh_mp, "open_thread")(question="Failed preflight")
    _propose(fresh_mp, slug)
    result = _tool(fresh_mp, "lesson_guard_manage")(
        action="verify", slug=slug, expected_revision=1,
        allow_payload={"thread_id": tid, "content": "unsafe"},
        deny_payload={"thread_id": tid, "content": "private sample that must not persist"},
    )
    assert "state=failed" in result
    assert _row(fresh_mp, slug)["failure"] == "preflight_failed"
    with fresh_mp["db"].read_db() as conn:
        history = " ".join(r[0] for r in conn.execute("SELECT snapshot_json FROM lesson_guard_history"))
    assert "private sample" not in history


def test_escape_is_bound_to_exact_payload_and_consumed_once(fresh_mp):
    slug, tid = _active(fresh_mp)
    manage = _tool(fresh_mp, "lesson_guard_manage")
    assert "escape=one_call_60s" in manage(
        action="escape", slug=slug, expected_revision=3, owner_confirm=True,
        deny_payload={"thread_id": tid, "content": "unsafe"},
    )
    note = _tool(fresh_mp, "note")
    assert "lesson_guard_denied" in note(thread_id=tid, content="unsafe", kind="insight")
    assert note(thread_id=tid, content="unsafe").startswith("ok")
    assert "lesson_guard_denied" in note(thread_id=tid, content="unsafe")


def test_escape_expires_and_child_cannot_use_owner_exception(fresh_mp, monkeypatch):
    from threadkeeper import lesson_guards, memory_access
    slug, tid = _active(fresh_mp)
    now = lesson_guards.time.time()
    _tool(fresh_mp, "lesson_guard_manage")(
        action="escape", slug=slug, expected_revision=3, owner_confirm=True,
        deny_payload={"thread_id": tid, "content": "unsafe"},
    )
    policy = memory_access.MemoryAccess(access="write", child=True, namespace="prj_aaaaaaaaaaaaaaaaaaaa")
    monkeypatch.setattr(memory_access, "current_access", lambda: policy)
    assert _tool(fresh_mp, "lesson_guard_manage")(action="retire", slug=slug, expected_revision=3, owner_confirm=True) == "ERR owner_required"
    assert "lesson_guard_denied" in _tool(fresh_mp, "note")(thread_id=tid, content="unsafe")
    monkeypatch.setattr(memory_access, "current_access", lambda: memory_access.MemoryAccess(access="write", child=False, origin="foreground", namespace="*"))
    monkeypatch.setattr(lesson_guards.time, "time", lambda: now + 61)
    assert "lesson_guard_denied" in _tool(fresh_mp, "note")(thread_id=tid, content="unsafe")


def test_stale_source_or_runtime_never_claims_active_coverage(fresh_mp, monkeypatch):
    from threadkeeper import lesson_guards
    slug, tid = _active(fresh_mp)
    monkeypatch.setattr(lesson_guards, "RUNTIME_DIGEST", "changed-code")
    status = _tool(fresh_mp, "lesson_guard_status")()
    assert "state=stale" in status and "runtime_changed" in status
    assert "lesson_guard_stale" in _tool(fresh_mp, "note")(thread_id=tid, content="safe")


def test_contract_tampering_is_detected_before_call(fresh_mp):
    slug, tid = _active(fresh_mp)
    conn = fresh_mp["db"].get_db()
    conn.execute("UPDATE lesson_guards SET trigger_json='broken' WHERE slug=?", (slug,))
    conn.commit()
    assert "lesson_guard_stale" in _tool(fresh_mp, "note")(thread_id=tid, content="unsafe")
    assert "contract_changed" in _tool(fresh_mp, "lesson_guard_status")()
    conn.close()


def test_retirement_rollback_and_stale_updates_preserve_history(fresh_mp):
    slug, tid = _active(fresh_mp)
    manage = _tool(fresh_mp, "lesson_guard_manage")
    assert _propose(fresh_mp, slug, "new unsafe", expected_revision=3) == "ERR owner_confirmation_required"
    assert manage(action="retire", slug=slug, expected_revision=2, owner_confirm=True) == "ERR stale_revision"
    assert "state=retired" in manage(action="retire", slug=slug, expected_revision=3, owner_confirm=True)
    assert _tool(fresh_mp, "note")(thread_id=tid, content="unsafe").startswith("ok")
    assert "state=proposed" in manage(action="rollback", slug=slug, expected_revision=4, owner_confirm=True)
    with fresh_mp["db"].read_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM lesson_guard_history WHERE slug=?", (slug,)).fetchone()[0] == 5
    assert _row(fresh_mp, slug)["verified_at"] is None


@pytest.mark.parametrize("adapter,event", [("claude-code", "PreToolUse"), ("copilot", "preToolUse"), ("codex", "before_tool")])
def test_unverified_native_host_contracts_are_explicitly_unsupported(fresh_mp, adapter, event):
    slug = _eligible(fresh_mp)
    assert _propose(fresh_mp, slug, adapter=adapter, event=event) == "ERR unsupported_adapter_or_event"
    assert "native_cli_hooks=unsupported" in _tool(fresh_mp, "lesson_guard_status")()


@pytest.mark.parametrize("field,operator,value", [
    ("missing", "equals", "value"),
    ("content", "regex", ".*"),
    ("content", "path_glob", "../private/*"),
    ("content", "equals", "/Users/example/private/file"),
    ("content", "equals", "password=synthetic-secret"),
])
def test_unsafe_or_unknown_trigger_contract_is_refused(fresh_mp, field, operator, value):
    slug = _eligible(fresh_mp)
    out = _tool(fresh_mp, "lesson_guard_manage")(
        action="propose", slug=slug, trigger={"tool": "note", "field": field, "operator": operator, "value": value},
    )
    assert out.startswith("ERR ")
    assert value not in out


def test_active_guard_suppresses_duplicate_escalation_and_status_is_read_only(fresh_mp):
    from threadkeeper import curator
    slug, _ = _active(fresh_mp)
    out = _tool(fresh_mp, "lesson_violation")(slug, "synthetic repeat")
    assert "guard=active recommend=none" in out
    assert "lesson_guards active=1" in _tool(fresh_mp, "mp_dashboard")()
    line = curator._format_lesson({"slug": slug, "body": "rule"}, violations=3, guard_state="active")
    assert "[GUARD-ACTIVE:MCP]" in line and "[MEMORY-INSUFFICIENT]" not in line
    with fresh_mp["db"].read_db() as conn:
        before = conn.execute("SELECT COUNT(*) FROM lesson_guard_history").fetchone()[0]
    _tool(fresh_mp, "lesson_guard_status")()
    with fresh_mp["db"].read_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM lesson_guard_history").fetchone()[0] == before


def test_literal_command_prefix_and_normalized_file_glob(fresh_mp):
    from threadkeeper import guard_rules
    rule = {"tool": "note", "field": "content", "operator": "command_prefix", "value": "git push --force"}
    assert guard_rules.matches(rule, {"content": "git   push '--force' origin branch"})
    assert not guard_rules.matches(rule, {"content": "git push --force-with-lease"})
    path = dict(rule, operator="path_glob", value="**/.env")
    assert guard_rules.matches(path, {"content": ".env"})
    assert guard_rules.matches(path, {"content": "src/../.env"})
    assert not guard_rules.matches(path, {"content": "src/.env.example"})


def test_source_change_marks_guard_stale_and_restart_keeps_verified_records(fresh_mp):
    import importlib
    from threadkeeper import lesson_guards, lessons
    slug, tid = _active(fresh_mp)
    importlib.reload(lesson_guards)
    assert "state=active" in _tool(fresh_mp, "lesson_guard_status")()
    assert "lesson_guard_denied" in _tool(fresh_mp, "note")(thread_id=tid, content="unsafe")
    assert lessons.patch_lesson(slug, "Do not save the unsafe marker.", "Review the marker before saving it.") == "ok"
    assert "lesson_changed_or_missing" in _tool(fresh_mp, "lesson_guard_status")()
    assert "lesson_guard_stale" in _tool(fresh_mp, "note")(thread_id=tid, content="safe")


def test_one_escape_cannot_authorize_two_concurrent_calls(fresh_mp):
    from concurrent.futures import ThreadPoolExecutor
    slug, tid = _active(fresh_mp)
    _tool(fresh_mp, "lesson_guard_manage")(
        action="escape", slug=slug, expected_revision=3, owner_confirm=True,
        deny_payload={"thread_id": tid, "content": "unsafe"},
    )
    note = _tool(fresh_mp, "note")
    with ThreadPoolExecutor(max_workers=2) as executor:
        outputs = list(executor.map(lambda _: note(thread_id=tid, content="unsafe"), range(2)))
    assert sum(result.startswith("ok") for result in outputs) == 1
    assert sum("lesson_guard_denied" in result for result in outputs) == 1
    with fresh_mp["db"].read_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1


def test_current_schema_install_materializes_missing_registry(fresh_mp):
    conn = fresh_mp["db"].get_db()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for table in ("lesson_guards", "lesson_guard_history", "lesson_guard_exemptions"):
        conn.execute(f"DROP TABLE {table}")
    conn.commit()
    conn.close()
    fresh_mp["db"].bootstrap_db(force=True)
    with fresh_mp["db"].read_db() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert conn.execute("SELECT COUNT(*) FROM lesson_guards").fetchone()[0] == 0


def test_async_write_boundary_uses_the_same_verified_contract(fresh_mp):
    import asyncio
    from threadkeeper._mcp import write_tool
    called = []
    @write_tool()
    async def guard_async_probe(payload: str) -> str:
        called.append(payload)
        return "allowed"
    slug = _eligible(fresh_mp)
    manage = _tool(fresh_mp, "lesson_guard_manage")
    assert "state=proposed" in manage(action="propose", slug=slug, trigger={
        "tool": "guard_async_probe", "field": "payload", "operator": "equals", "value": "unsafe",
    })
    assert "state=verified" in manage(action="verify", slug=slug, expected_revision=1,
        allow_payload={"payload": "safe"}, deny_payload={"payload": "unsafe"})
    manage(action="activate", slug=slug, expected_revision=2, owner_confirm=True)
    probe = _tool(fresh_mp, "guard_async_probe")
    assert "lesson_guard_denied" in asyncio.run(probe(payload="unsafe"))
    assert called == []
    assert asyncio.run(probe(payload="safe")) == "allowed"
    assert called == ["safe"]


def test_write_capability_cannot_edit_owner_guard_tables(fresh_mp, monkeypatch):
    import sqlite3
    from threadkeeper.memory_access import token_digest
    slug, tid = _active(fresh_mp)
    conn = fresh_mp["db"].get_db()
    conn.execute("INSERT INTO child_memory_grants VALUES(?,?,?,?,?,?,?)", (
        "guard-child", "tk_guard_test", token_digest("test-token"), "write", "*", "spawned", 1,
    ))
    conn.commit()
    conn.close()
    monkeypatch.setenv("THREADKEEPER_FORCE_CID", "guard-child")
    monkeypatch.setenv("THREADKEEPER_MEMORY_TOKEN", "test-token")
    child = fresh_mp["db"].get_db()
    with pytest.raises(sqlite3.DatabaseError):
        child.execute("UPDATE lesson_guards SET state='retired' WHERE slug=?", (slug,))
    assert _tool(fresh_mp, "lesson_guard_manage")(action="retire", slug=slug, expected_revision=3, owner_confirm=True) == "ERR owner_required"
    assert _tool(fresh_mp, "note")(thread_id=tid, content="safe").startswith("ok")
    assert "lesson_guard_denied" in _tool(fresh_mp, "note")(thread_id=tid, content="unsafe")
    child.close()


def test_unavailable_guard_refuses_before_handler(fresh_mp, monkeypatch):
    import sqlite3
    from threadkeeper import lesson_guards
    _, tid = _active(fresh_mp)
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic registry unavailable")
    monkeypatch.setattr(lesson_guards, "_check_call", unavailable)
    assert _tool(fresh_mp, "note")(thread_id=tid, content="safe") == "ERR lesson_guard_unavailable"
    with fresh_mp["db"].read_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0


def test_mcp_transport_enforces_verified_guard_in_isolated_server(tmp_path):
    import asyncio
    import importlib.util
    import os
    from pathlib import Path
    import re
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    isolation_spec = importlib.util.spec_from_file_location("guard_test_isolation", Path(__file__).with_name("conftest.py"))
    isolation = importlib.util.module_from_spec(isolation_spec)
    isolation_spec.loader.exec_module(isolation)
    env = dict(os.environ, **isolation._force_clean_env(tmp_path / "sdk-server"))
    env.update(THREADKEEPER_NO_EMBEDDINGS="1", THREADKEEPER_MENUBAR_AUTO_LAUNCH="0",
               THREADKEEPER_FORCE_CID="", PYTHONPATH=str(Path(__file__).parents[1]))
    parameters = StdioServerParameters(command=sys.executable,
        args=["-m", "threadkeeper.server"], env=env)

    async def exercise():
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                async def call(name, arguments):
                    result = await client.call_tool(name, arguments)
                    assert not result.is_error
                    return "\n".join(block.text for block in result.content if hasattr(block, "text"))
                result = await call("lesson_append", {"title": "SDK guard rule", "body": "Refuse the unsafe marker."})
                assert "ok" in result
                slug = "sdk-guard-rule"
                for _ in range(3):
                    await call("session_end", {})
                    assert "ok" in await call("lesson_violation", {"slug": slug, "evidence": "synthetic repeat"})
                tid = await call("open_thread", {"question": "SDK guard control", "memory_scope": "global"})
                tid = re.search(r"T[0-9a-z]+", tid).group(0)
                rule = {"tool": "note", "field": "content", "operator": "equals", "value": "unsafe"}
                assert "state=proposed" in await call("lesson_guard_manage", {"action": "propose", "slug": slug, "trigger": rule})
                assert "state=verified" in await call("lesson_guard_manage", {
                    "action": "verify", "slug": slug, "expected_revision": 1,
                    "allow_payload": {"thread_id": tid, "content": "safe"},
                    "deny_payload": {"thread_id": tid, "content": "unsafe"},
                })
                assert "state=active" in await call("lesson_guard_manage", {
                    "action": "activate", "slug": slug, "expected_revision": 2, "owner_confirm": True,
                })
                assert "lesson_guard_denied" in await call("note", {"thread_id": tid, "content": "unsafe"})
                assert "ok" in await call("note", {"thread_id": tid, "content": "safe"})
                assert "state=active" in await call("lesson_guard_status", {"slug": slug})
    asyncio.run(asyncio.wait_for(exercise(), timeout=30))


def test_advertised_alias_is_guarded_and_signature_changes_invalidate_proof(fresh_mp, monkeypatch):
    from threadkeeper._mcp import write_tool
    called = []
    @write_tool(name="guard_alias")
    def internal_probe(payload: str) -> str:
        called.append(payload)
        return "allowed"
    slug = _eligible(fresh_mp)
    manage = _tool(fresh_mp, "lesson_guard_manage")
    assert "state=proposed" in manage(action="propose", slug=slug, trigger={
        "tool": "guard_alias", "field": "payload", "operator": "equals", "value": "unsafe",
    })
    assert "state=verified" in manage(action="verify", slug=slug, expected_revision=1,
        allow_payload={"payload": "safe"}, deny_payload={"payload": "unsafe"})
    manage(action="activate", slug=slug, expected_revision=2, owner_confirm=True)
    assert "lesson_guard_denied" in _tool(fresh_mp, "guard_alias")(payload="unsafe")
    assert called == []
    assert _tool(fresh_mp, "guard_alias")(payload="safe") == "allowed"
    def changed_schema(payload: str, extra: str = "") -> str:
        return "unreachable"
    monkeypatch.setattr(fresh_mp["mcp"]._tool_manager._tools["guard_alias"], "fn", changed_schema)
    assert "state=stale" in _tool(fresh_mp, "lesson_guard_status")()
