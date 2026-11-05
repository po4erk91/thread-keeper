"""Progressive evidence delivery must preserve privacy and read purity."""
from __future__ import annotations

import asyncio
import json
import re
import time
from types import SimpleNamespace

import jsonschema
import pytest
from mcp.server.mcpserver.exceptions import ResourceNotFoundError


PROJECT_A = "prj_aaaaaaaaaaaaaaaaaaaa"
PROJECT_B = "prj_bbbbbbbbbbbbbbbbbbbb"


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


@pytest.fixture()
def store(fresh_mp, monkeypatch):
    from threadkeeper import retrieval

    monkeypatch.setenv("THREADKEEPER_PROJECT_NAMESPACE", PROJECT_A)
    monkeypatch.setattr(retrieval.config, "SEMANTIC_AVAILABLE", False)
    return fresh_mp


def _note(pkg, content="needle complete note", scope="project", project=PROJECT_A):
    conn = pkg["db"].get_db()
    now = int(time.time())
    tid = f"T-{project}-{scope}"
    conn.execute(
        "INSERT OR IGNORE INTO threads(id, question, state, opened_at, last_touched_at, "
        "project_id, memory_scope) VALUES (?, 'evidence question', 'active', ?, ?, ?, ?)",
        (tid, now, now, project, scope),
    )
    cur = conn.execute(
        "INSERT INTO notes(thread_id, content, kind, created_at, project_id, memory_scope) "
        "VALUES (?, ?, 'insight', ?, ?, ?)", (tid, content, now, project, scope),
    )
    note_id = str(cur.lastrowid)
    conn.commit()
    conn.close()
    return tid, note_id


def _dialogs(pkg, *, session="conversation", project=PROJECT_A, source="test", count=9):
    conn = pkg["db"].get_db()
    now = int(time.time())
    for index in range(count):
        conn.execute(
            "INSERT INTO dialog_messages(uuid, source, session_id, role, content, "
            "created_at, project_id, memory_scope) VALUES (?, ?, ?, 'user', ?, ?, ?, 'project')",
            (f"{session}-{project}-{source}-{index}", source, session,
             f"{'needle' if index == 4 else 'context'} message {index}",
             now + index, project),
        )
    conn.commit()
    conn.close()


def _read(pkg, uri, context=None):
    contents = list(asyncio.run(pkg["mcp"].read_resource(uri, context)))
    assert contents[0].mime_type == "application/json"
    assert contents[0].meta == {"cacheScope": "private", "ttl": 0}
    return json.loads(contents[0].content)


def _hit(pkg, tool="search", **kwargs):
    return _tool(pkg, tool)("needle", **kwargs).structured_content["hits"][0]


def _context(name):
    return SimpleNamespace(request_context=SimpleNamespace(
        meta={"io.modelcontextprotocol/clientInfo": {"name": name, "version": "1"},
              "io.modelcontextprotocol/clientCapabilities": {}},
        protocol_version="2026-07-28",
    ))


def test_mixed_text_links_and_structured_schema_preserve_text_fallback(store):
    tid, note_id = _note(store, "needle " + "full evidence beyond snippet " * 30)
    result = asyncio.run(store["mcp"].call_tool("search", {"query": "needle"}))
    tool = store["mcp"]._tool_manager._tools["search"]
    jsonschema.validate(result.structured_content, tool.output_schema)
    hit = result.structured_content["hits"][0]
    assert result.content[0].type == "text"
    assert result.content[0].text == result.structured_content["text"]
    assert result.content[0].text.startswith(f"{tid} insight s=")
    assert hit["id"] == note_id
    assert hit["matched_by"] == ["fts"]
    assert [block.type for block in result.content] == ["text", "resource_link", "resource_link"]
    assert {str(block.uri) for block in result.content[1:]} == {hit["resource_uri"], hit["thread_uri"]}
    assert len(_read(store, hit["resource_uri"])["items"][0]["content"]) > 240
    assert _read(store, hit["thread_uri"])["thread"]["id"] == tid
    listed = asyncio.run(store["mcp"].list_resources())
    assert hit["resource_uri"] not in {str(resource.uri) for resource in listed}
    assert "ctx" not in tool.parameters["properties"]


@pytest.mark.parametrize("kwargs,text", [({}, "no_matches"), ({"mode": "broken"}, "ERR bad_mode=broken")])
def test_empty_and_invalid_search_keep_text_and_empty_metadata(store, kwargs, text):
    name = "dialog_search" if kwargs else "search"
    result = _tool(store, name)("absent", **kwargs)
    assert result.content[0].text.startswith(text)
    assert result.structured_content["hits"] == []
    assert len(result.content) == 1


def test_opaque_links_refuse_guessing_traversal_other_callers_and_expiry(store, monkeypatch):
    from threadkeeper import evidence

    _note(store)
    owner = _context("owner")
    uri = _hit(store, ctx=owner)["resource_uri"]
    assert re.fullmatch(r"memory://evidence/[A-Za-z0-9_-]{43}", uri)
    assert _read(store, uri, owner)["kind"] == "note"
    for invalid in ("memory://evidence/1", "memory://evidence/../notes/1", uri + "?id=2",
                    evidence.EVIDENCE_PREFIX + "a" * 43):
        with pytest.raises(ResourceNotFoundError):
            _read(store, invalid, owner)
    with pytest.raises(ResourceNotFoundError):
        _read(store, uri, _context("other"))
    with pytest.raises(ResourceNotFoundError):
        _read(store, uri)
    now = evidence._clock()
    monkeypatch.setattr(evidence, "_clock", lambda: now + evidence.EVIDENCE_TTL_SECONDS + 1)
    with pytest.raises(ResourceNotFoundError):
        _read(store, uri, owner)


def test_links_bind_current_project_and_explicit_cross_project_recall(store, monkeypatch):
    _note(store, project=PROJECT_A)
    _note(store, project=PROJECT_B)
    _note(store, scope="global", project=None)
    _note(store, scope="unresolved", project=None)
    result = _tool(store, "search")("needle", k=10)
    assert {hit["project_id"] for hit in result.structured_content["hits"]} == {PROJECT_A, None}
    uri = result.structured_content["hits"][0]["resource_uri"]
    cross = _tool(store, "search")("needle", k=10, cross_project=True)
    hit_b = next(hit for hit in cross.structured_content["hits"] if hit["project_id"] == PROJECT_B)
    assert _read(store, hit_b["resource_uri"])["items"][0]["project_id"] == PROJECT_B
    monkeypatch.setenv("THREADKEEPER_PROJECT_NAMESPACE", PROJECT_B)
    for link in (uri, hit_b["resource_uri"]):
        with pytest.raises(ResourceNotFoundError):
            _read(store, link)


@pytest.mark.parametrize("action", ["invalidate", "forget", "rescope"])
def test_note_and_thread_links_recheck_live_authority_erasure_and_scope(store, action):
    from threadkeeper.authority import invalidate_descendants, record_root
    from threadkeeper.forget import forget_selector

    tid, note_id = _note(store)
    hit = _hit(store)
    if action == "forget":
        assert "ERR" not in forget_selector(tid, selector_type="thread_id", dry_run=False)
    else:
        conn = store["db"].get_db()
        if action == "invalidate":
            record_root(conn, "note", note_id, authority="observed", principal="writer", channel="test")
            invalidate_descendants(conn, [("note", note_id)])
        else:
            conn.execute("UPDATE notes SET project_id=? WHERE id=?", (PROJECT_B, note_id))
        conn.commit()
        conn.close()
    for uri in (hit["resource_uri"], hit["thread_uri"]):
        with pytest.raises(ResourceNotFoundError):
            _read(store, uri)


def test_dialog_window_has_fixed_membership_order_and_scope(store):
    _dialogs(store)
    _dialogs(store, project=PROJECT_B)
    _dialogs(store, session="other")
    _dialogs(store, source="other")
    result = _tool(store, "dialog_search")("needle", mode="fts", k=1)
    schema = store["mcp"]._tool_manager._tools["dialog_search"].output_schema
    jsonschema.validate(result.structured_content, schema)
    hit = result.structured_content["hits"][0]
    uri = hit["resource_uri"]
    document = _read(store, uri)
    assert [item["content"] for item in document["items"]] == [
        "context message 2", "context message 3", "needle message 4",
        "context message 5", "context message 6",
    ]
    assert len({item["session_id"] for item in document["items"]}) == 1
    assert {item["project_id"] for item in document["items"]} == {PROJECT_A}
    assert hit["id"] in {item["id"] for item in document["items"]}
    conn = store["db"].get_db()
    conn.execute(
        "INSERT INTO dialog_messages(uuid, source, session_id, role, content, created_at, project_id) "
        "SELECT 'later-arrival', source, session_id, role, 'late row', created_at, project_id "
        "FROM dialog_messages WHERE uuid=?", (hit["id"],),
    )
    conn.commit()
    conn.close()
    assert _read(store, uri) == document


@pytest.mark.parametrize("action", ["retention", "expiry", "forget", "invalidate"])
def test_dialog_links_refuse_missing_or_expired_window_members(store, monkeypatch, action):
    from threadkeeper import evidence
    from threadkeeper.authority import invalidate_descendants, record_root
    from threadkeeper.forget import forget_selector
    from threadkeeper.retention import _prune_dialog

    _dialogs(store)
    hit = _hit(store, "dialog_search", mode="fts")
    first_id = _read(store, hit["resource_uri"])["items"][0]["id"]
    if action == "forget":
        assert "ERR" not in forget_selector(first_id, selector_type="uuid", dry_run=False)
    else:
        conn = store["db"].get_db()
        if action in {"retention", "expiry"}:
            conn.execute("UPDATE dialog_messages SET created_at=1 WHERE uuid=?", (first_id,))
            conn.commit()
        if action == "retention":
            assert _prune_dialog(conn, int(time.time()) - 86400) == 1
        if action == "expiry":
            monkeypatch.setattr(evidence.config, "DIALOG_RETENTION_DAYS", 1)
        if action == "invalidate":
            record_root(conn, "dialog", first_id, authority="observed", principal="writer", channel="test")
            invalidate_descendants(conn, [("dialog", first_id)])
        conn.commit()
        conn.close()
    with pytest.raises(ResourceNotFoundError):
        _read(store, hit["resource_uri"])


@pytest.mark.parametrize("body", ["界" * 100000, "\"\\\n" * 20000])
def test_evidence_reads_are_query_only_and_byte_bounded(store, monkeypatch, body):
    from threadkeeper import evidence

    _note(store, "needle " + body)
    statements = []
    original = store["db"]._open_connection

    def traced(*args, **kwargs):
        conn, vec = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn, vec

    monkeypatch.setattr(store["db"], "_open_connection", traced)
    hit = _hit(store)
    for uri in (hit["resource_uri"], hit["thread_uri"]):
        document = _read(store, uri)
        assert document["items"][0]["truncated"] is True
        assert len(json.dumps(document, ensure_ascii=False).encode()) <= evidence.MAX_RESOURCE_BYTES
    forbidden = ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ")
    assert not [sql for sql in statements if sql.lstrip().upper().startswith(forbidden)]
    assert sum("PRAGMA query_only=ON" in sql for sql in statements) >= 3


def test_like_fallback_keeps_links_and_text(store, monkeypatch):
    from threadkeeper.tools import dialog

    _dialogs(store)
    monkeypatch.setattr(dialog, "SEMANTIC_AVAILABLE", False)
    monkeypatch.setattr(dialog, "retrieve_dialogs", lambda *args, **kwargs: [])
    result = _tool(store, "dialog_search")("needle", mode="fts")
    assert result.structured_content["hits"][0]["matched_by"] == ["like"]
    assert result.content[0].text.startswith("user@conversa ")
    assert _read(store, result.structured_content["hits"][0]["resource_uri"])["kind"] == "dialog"


def test_link_registry_is_bounded_and_retains_no_memory_bodies(store, monkeypatch):
    from threadkeeper import evidence

    _note(store)
    monkeypatch.setattr(evidence, "MAX_EVIDENCE_LINKS", 2)
    old = _hit(store)["resource_uri"]
    current = _hit(store)["resource_uri"]
    assert len(evidence._references) == 2
    assert "needle" not in repr(evidence._references)
    assert _read(store, current)["kind"] == "note"
    with pytest.raises(ResourceNotFoundError):
        _read(store, old)


def test_thread_excerpt_bounds_notes_and_keeps_anchor(store):
    from threadkeeper import evidence

    tid, anchor_id = _note(store)
    for index in range(15):
        _note(store, content=f"other note {index}")
    hit = _hit(store)
    document = _read(store, hit["thread_uri"])
    assert len(document["items"]) == evidence.MAX_THREAD_NOTES
    assert anchor_id in {item["id"] for item in document["items"]}
    assert {item["thread_id"] for item in document["items"]} == {tid}
