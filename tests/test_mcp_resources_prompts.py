"""MCP Resources & Prompts primitives (roadmap #78).

thread-keeper historically exposed only MCP **tools**. This pins the two
newly-adopted primitives:

  * **Resources** — read-only memory snapshots (``memory://brief`` /
    ``memory://context`` / ``memory://dashboard`` / ``memory://agent-status``)
    reachable at stable URIs, returning the same content the matching tool
    renders.
  * **Prompts** — curation / audit / review flows as host-native, parameterized
    commands.

Plus the additive contract: the server advertises both capabilities, the
resource/prompt functions never leak into the tool registry, and the tool-only
fallback path (a host that ignores resources still gets ``brief()``) is intact.

Uses the ``fresh_mp`` bootstrap (clean DB + isolated CLI homes) from conftest.
The list/read/get MCP APIs are async; we drive them with ``asyncio.run`` since
each call is self-contained (no shared event loop needed).
"""
from __future__ import annotations

import asyncio

import pytest

from mcp.server.lowlevel.server import NotificationOptions


RESOURCE_URIS = {
    "memory://brief",
    "memory://context",
    "memory://dashboard",
    "memory://agent-status",
}
PROMPT_NAMES = {
    "review_recent_threads",
    "run_library_curation",
    "audit_threadkeeper",
}


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _resource_uris(pkg):
    return {str(r.uri) for r in asyncio.run(pkg["mcp"].list_resources())}


def _read_resource(pkg, uri):
    contents = asyncio.run(pkg["mcp"].read_resource(uri))
    return "".join(c.content for c in contents)


def _prompt_names(pkg):
    return {p.name for p in asyncio.run(pkg["mcp"].list_prompts())}


def _render_prompt(pkg, name, args=None):
    res = asyncio.run(pkg["mcp"].get_prompt(name, args or {}))
    return "\n".join(m.content.text for m in res.messages)


# ──────────────────────────────────────────────────────────────────────
# Resources: listing + read content
# ──────────────────────────────────────────────────────────────────────

def test_resources_are_listed_with_stable_uris(fresh_mp):
    assert RESOURCE_URIS <= _resource_uris(fresh_mp)


def test_brief_resource_returns_brief_content(fresh_mp):
    """memory://brief is backed by render_brief and reflects live thread state."""
    pkg = fresh_mp
    tid = _tool(pkg, "open_thread")(question="resource-backed brief thread")

    body = _read_resource(pkg, "memory://brief")
    assert body.startswith("ctx sess=")
    # The brief renders the live working set, so the just-opened thread shows up.
    assert tid in body
    assert "resource-backed brief thread" in body


def test_brief_resource_is_side_effect_free(fresh_mp):
    """A host pulling the brief resource must not write *_hint_shown events
    (lean render): repeated pulls leave the events table untouched."""
    pkg = fresh_mp
    conn = pkg["db"].get_db()

    def hint_events():
        return conn.execute(
            "SELECT COUNT(*) c FROM events WHERE kind LIKE '%_hint_shown'"
        ).fetchone()["c"]

    before = hint_events()
    _read_resource(pkg, "memory://brief")
    _read_resource(pkg, "memory://brief")
    assert hint_events() == before


def test_context_resource_matches_context_tool_text(fresh_mp):
    """memory://context returns the same text block the context() tool renders."""
    pkg = fresh_mp
    _tool(pkg, "open_thread")(question="ctx thread")

    body = _read_resource(pkg, "memory://context")
    assert "sess=" in body
    assert "db=" in body
    # one active thread seeded → reflected in the thread-counts segment
    assert "active=1" in body

    # The tool's structuredContent path and the resource share render_context;
    # the tool's legacy text block matches the resource body modulo the live
    # clock, so compare the stable structural prefix.
    tool_result = _tool(pkg, "context")()
    tool_text = next(
        c.text for c in tool_result.content if getattr(c, "type", None) == "text"
    )
    assert tool_text.split(" started=")[0] == body.split(" started=")[0]


def test_dashboard_and_agent_status_resources_read(fresh_mp):
    pkg = fresh_mp
    dash = _read_resource(pkg, "memory://dashboard")
    assert dash.startswith("dashboard window=")
    agent = _read_resource(pkg, "memory://agent-status")
    assert "loops" in agent


def test_memory_resource_metadata_is_private_bounded_and_fresh(fresh_mp):
    """The resource list is safe to cache only in a private, short-lived slot."""
    resources = asyncio.run(fresh_mp["mcp"].list_resources())
    by_uri = {str(resource.uri): resource for resource in resources}

    for uri in RESOURCE_URIS:
        resource = by_uri[uri]
        assert resource.annotations.audience == ["assistant"]
        assert 0.0 < resource.annotations.priority <= 1.0
        assert resource.annotations.last_modified.endswith("Z")
        assert resource.meta["cacheScope"] == "private"
        assert resource.meta["ttl"] == 30
        assert resource.meta["lastModified"] == resource.annotations.last_modified
        # Calculated from the same dynamic snapshot a client reads, not a stale
        # fixed declaration in the decorator.
        assert resource.size is not None and resource.size > 0


def test_committed_event_invalidation_map_is_narrow(fresh_mp):
    from threadkeeper.tools.resources import resources_for_event

    assert resources_for_event("open_thread") == {
        "memory://brief", "memory://context", "memory://dashboard",
    }
    assert resources_for_event("signal:whisper") == {
        "memory://brief", "memory://dashboard",
    }
    assert resources_for_event("task_kill") == {
        "memory://brief", "memory://dashboard", "memory://agent-status",
    }
    assert resources_for_event("shadow_review_pass") == {
        "memory://dashboard", "memory://agent-status",
    }
    assert resources_for_event("style_set") == {
        "memory://brief", "memory://dashboard",
    }
    assert resources_for_event("unclassified_event") == set()


class _RecordingSession:
    def __init__(self):
        self.notifications = []

    async def send_notification(self, notification):
        self.notifications.append(notification)


def _notification_payload(notification):
    return notification.params.model_dump(by_alias=True, exclude_none=True)


def test_subscriptions_emit_coalesced_private_updates_after_commit(fresh_mp, monkeypatch):
    """Two committed thread writes before a poll become one update per URI."""
    from threadkeeper.tools import resources

    monkeypatch.setattr(resources, "_SUBSCRIPTION_POLL_SECONDS", 60)
    session = _RecordingSession()

    async def exercise():
        await resources._subscriptions.subscribe("memory://brief", session)
        await resources._subscriptions.subscribe("memory://context", session)
        tid = _tool(fresh_mp, "open_thread")(question="subscription thread")
        assert _tool(fresh_mp, "note")(tid, "first committed note") == "ok id=1"
        await resources._subscriptions.poll_once()

    asyncio.run(exercise())

    assert len(session.notifications) == 2
    payloads = [_notification_payload(item) for item in session.notifications]
    assert {str(payload["uri"]) for payload in payloads} == {
        "memory://brief", "memory://context",
    }
    for payload in payloads:
        # No content can be carried in an invalidation notification.
        assert set(payload) == {"uri", "_meta"}
        assert payload["_meta"] == {
            "cacheScope": "private",
            "ttl": 30,
            "lastModified": payload["_meta"]["lastModified"],
        }
        assert payload["_meta"]["lastModified"].endswith("Z")


def test_rolled_back_events_and_unrelated_resources_do_not_notify(fresh_mp, monkeypatch):
    from threadkeeper import identity
    from threadkeeper.db import run_write
    from threadkeeper.tools import resources

    monkeypatch.setattr(resources, "_SUBSCRIPTION_POLL_SECONDS", 60)
    session = _RecordingSession()
    # Start a thread once to initialize the process session, before subscribing.
    _tool(fresh_mp, "open_thread")(question="initialization thread")

    async def exercise():
        await resources._subscriptions.subscribe("memory://context", session)
        await resources._subscriptions.subscribe("memory://agent-status", session)

        def rolled_back(conn):
            identity._emit(conn, "open_thread", target="Trollback")
            raise RuntimeError("force rollback")

        with pytest.raises(RuntimeError, match="force rollback"):
            run_write("rolled-back-resource-test", rolled_back)
        await resources._subscriptions.poll_once()

        # A committed signal changes the brief/dashboard only, not either URI
        # subscribed above.
        def signal(conn):
            identity._emit(conn, "signal:whisper", target="peer")

        run_write("unrelated-resource-test", signal)
        await resources._subscriptions.poll_once()

    asyncio.run(exercise())
    assert session.notifications == []


def test_clients_that_do_not_subscribe_keep_pull_only_behavior(fresh_mp):
    from threadkeeper.tools import resources

    assert resources._subscriptions.count == 0
    tid = _tool(fresh_mp, "open_thread")(question="pull-only resource client")
    # No session has opted into updates, while ordinary resource reads retain
    # the pre-subscription behavior and expose the committed mutation.
    assert resources._subscriptions.count == 0
    assert tid in _read_resource(fresh_mp, "memory://brief")


# ──────────────────────────────────────────────────────────────────────
# Prompts: listing + render
# ──────────────────────────────────────────────────────────────────────

def test_prompts_are_listed(fresh_mp):
    assert PROMPT_NAMES <= _prompt_names(fresh_mp)


def test_review_recent_threads_prompt_renders_with_param(fresh_mp):
    text = _render_prompt(fresh_mp, "review_recent_threads", {"limit": "7"})
    assert "7 most recent" in text
    assert "brief()" in text


def test_run_library_curation_prompt_reflects_force(fresh_mp):
    plain = _render_prompt(fresh_mp, "run_library_curation", {})
    assert "curator_review(force=false)" in plain
    forced = _render_prompt(fresh_mp, "run_library_curation", {"force": "true"})
    assert "curator_review(force=true)" in forced


def test_audit_threadkeeper_prompt_renders(fresh_mp):
    text = _render_prompt(fresh_mp, "audit_threadkeeper")
    assert "mp_dashboard()" in text
    assert "validate_threads()" in text


# ──────────────────────────────────────────────────────────────────────
# Capability negotiation + additive / fallback contract
# ──────────────────────────────────────────────────────────────────────

def test_server_advertises_resources_and_prompts_capabilities(fresh_mp):
    caps = fresh_mp["mcp"]._lowlevel_server.get_capabilities(
        NotificationOptions(), {}, protocol_version="2026-07-28",
    )
    assert caps.resources is not None
    assert caps.resources.subscribe is True
    assert caps.prompts is not None
    assert caps.tools is not None  # tools unaffected


def test_resource_and_prompt_functions_are_not_tools(fresh_mp):
    """Resources/prompts register on their own managers — they must never
    appear in the tool registry, so the #67 annotation contract is unchanged."""
    tool_names = set(fresh_mp["mcp"]._tool_manager._tools.keys())
    assert "brief" in tool_names and "context" in tool_names  # the tools stay
    for leaked in (
        "brief_resource", "context_resource", "dashboard_resource",
        "agent_status_resource", "review_recent_threads",
        "run_library_curation", "audit_threadkeeper",
    ):
        assert leaked not in tool_names, f"{leaked} leaked into tool registry"


def test_tool_only_fallback_path_still_works(fresh_mp):
    """A host that advertises no `resources` capability falls back to the
    brief() tool: it must still render the brief independently of the resource
    layer, and surface the same live thread the resource does."""
    pkg = fresh_mp
    tid = _tool(pkg, "open_thread")(question="fallback thread")

    tool_brief = _tool(pkg, "brief")()
    assert tool_brief.startswith("ctx sess=")
    assert tid in tool_brief

    resource_brief = _read_resource(pkg, "memory://brief")
    # Both channels surface the same working-set thread — the resource is an
    # additional delivery path, not a replacement for the tool.
    assert tid in resource_brief
