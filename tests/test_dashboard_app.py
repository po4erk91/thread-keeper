"""Portable MCP App dashboard discovery, privacy, and read-purity contract."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name]


def _read_resource(pkg, uri):
    contents = asyncio.run(pkg["mcp"].read_resource(uri))
    return "".join(content.content for content in contents)


def test_dashboard_app_is_discoverable_and_bound_to_its_open_tool(fresh_mp):
    from threadkeeper.dashboard_app import DASHBOARD_URI

    pkg = fresh_mp
    resources = {str(resource.uri): resource for resource in asyncio.run(pkg["mcp"].list_resources())}
    resource = resources[DASHBOARD_URI]
    tool = _tool(pkg, "open_dashboard")

    assert resource.mime_type == "text/html;profile=mcp-app"
    assert resource.meta["ui"]["csp"] == {
        "connectDomains": [],
        "resourceDomains": [],
        "frameDomains": [],
        "baseUriDomains": [],
    }
    assert tool.meta["ui"] == {
        "resourceUri": DASHBOARD_URI,
        "visibility": ["model", "app"],
    }
    assert tool.annotations.read_only_hint is True


def test_dashboard_app_html_is_sandboxed_and_validates_messages(fresh_mp):
    from threadkeeper.dashboard_app import DASHBOARD_URI

    html = _read_resource(fresh_mp, DASHBOARD_URI)

    assert "connect-src 'none'" in html
    assert "event.source !== window.parent" in html
    assert "event.data.jsonrpc !== \"2.0\"" in html
    assert "innerHTML" not in html
    assert "tools/call" in html
    assert "open_dashboard" in html


def test_dashboard_tool_falls_back_when_the_host_has_no_app_capability(fresh_mp):
    result = _tool(fresh_mp, "open_dashboard").fn()
    text = result.content[0].text

    assert "does not advertise MCP Apps" in text
    assert result.structured_content["available"] is False


def test_dashboard_tool_uses_app_path_for_a_supporting_host(fresh_mp):
    from threadkeeper.dashboard_app import (
        _APP_CAPABILITY,
        _APP_MIME_TYPE,
        _CLIENT_CAPABILITIES_META,
    )

    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            meta={
                _CLIENT_CAPABILITIES_META: {
                    "extensions": {
                        _APP_CAPABILITY: {"mimeTypes": [_APP_MIME_TYPE]},
                    }
                }
            },
            session=None,
        )
    )
    result = _tool(fresh_mp, "open_dashboard").fn(ctx=ctx)

    assert result.content[0].text.startswith("ThreadKeeper dashboard opened")


def test_dashboard_refresh_is_observationally_pure(fresh_mp):
    pkg = fresh_mp
    conn = pkg["db"].get_db()

    def counts():
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sessions", "presence", "events", "tasks")
        }

    before = counts()
    _tool(pkg, "open_dashboard").fn()
    _tool(pkg, "open_dashboard").fn()

    assert counts() == before


def test_dashboard_snapshot_omits_prompts_paths_and_child_identifiers(fresh_mp):
    pkg = fresh_mp
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at, role, rss_kb) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            "task-private",
            0,
            "/private/agent/worktree",
            "secret prompt must never reach the dashboard",
            1,
            "curator",
            2048,
        ),
    )
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?,?,?,?,?)",
        ("session-private", "lesson_append", "", "secret event summary", 1),
    )
    conn.commit()

    result = _tool(pkg, "open_dashboard").fn()
    payload = str(result.structured_content)

    assert "secret prompt" not in payload
    assert "/private/agent/worktree" not in payload
    assert "task-private" not in payload
    assert "session-private" not in payload
    assert "secret event summary" not in payload


def test_dashboard_snapshot_does_not_create_a_database_or_session(tmp_path, monkeypatch):
    from threadkeeper import dashboard_app

    missing_db = tmp_path / "missing.sqlite"
    monkeypatch.setattr(dashboard_app, "DB_PATH", missing_db)

    snapshot = dashboard_app.dashboard_snapshot()

    assert snapshot["available"] is False
    assert not missing_db.exists()
