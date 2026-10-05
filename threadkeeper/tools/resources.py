"""Read-only MCP **Resources** for thread-keeper (roadmap #78).

MCP defines three core server primitives. thread-keeper historically exposed
only **tools** (model-controlled, may act). This module adds the second
primitive — **Resources** (application-controlled, read-only, safe for a host to
pull automatically) — for the genuinely read-only memory snapshots:

  * ``memory://brief``        — the session-start memory brief (``render_brief``)
  * ``memory://context``      — runtime context (session id, age, thread counts)
  * ``memory://dashboard``    — whole-system telemetry rollup (``mp_dashboard``)
  * ``memory://agent-status`` — autonomous-loop status snapshot

Why resources and not just the existing tools: on hookless CLIs (Codex /
Antigravity / Copilot) the managed instructions block asks the
agent to *remember* to call ``brief()`` before answering, and the project's own
docs note agents focused on their task often skip such calls. A Resource lets
the host surface the brief as attachable / ``@``-mentionable read-only context
through a mechanical channel, independent of whether the agent calls a tool. The
hook-injected brief and the ``brief()`` tool remain the fallback for hosts that
don't advertise the ``resources`` capability — nothing here changes the tool
surface (no tool is added, removed, or altered).

These resources are deliberately **side-effect-free**. ``memory://brief`` renders
with ``lean=True`` so the behavioral nudge/hint blocks (which ``INSERT``
``*_hint_shown`` events) never fire on an automatic host pull — a resource a host
refreshes on a timer must not mutate the escalation counters. The static memory
(core_memory, style, verbatim, user_model) is still rendered. ``memory://
agent-status`` uses ``refresh=False`` so a pull never triggers a process re-scan.

URIs are static on purpose: the spec's resource *templates* (``{param}``) are
still unevenly implemented across hosts, so parameterized URIs are left as a
later, host-gated step (see roadmap #78).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any

from mcp import types
from mcp.types import Annotations

from .._mcp import mcp
from ..db import get_db, read_db
from ..identity import _ensure_session
from ..brief import render_brief, render_context
from .dashboard import mp_dashboard
from ..agent_status import agent_status_snapshot, format_agent_status


logger = logging.getLogger(__name__)

MEMORY_RESOURCE_URIS = frozenset({
    "memory://brief",
    "memory://context",
    "memory://dashboard",
    "memory://agent-status",
})

# Memory snapshots can contain personal and project context. They are useful
# briefly, but must never be placed in a shared intermediary cache.
RESOURCE_CACHE_SCOPE = "private"
RESOURCE_TTL_SECONDS = 30
_RESOURCE_PRIORITIES = {
    "memory://brief": 1.0,
    "memory://context": 0.85,
    "memory://dashboard": 0.60,
    "memory://agent-status": 0.70,
}
_RESOURCE_BOOTSTRAPPED_AT = int(datetime.now(timezone.utc).timestamp())
_SUBSCRIPTION_POLL_SECONDS = 0.15


def _iso_timestamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def resources_for_event(kind: str) -> frozenset[str]:
    """Return the smallest memory-resource set changed by an event kind.

    The event log is written in the same SQLite transaction as each mutation,
    so this map is also the commit boundary for resource notifications. Unknown
    events intentionally refresh nothing: expanding a new mutation's resource
    impact is an explicit compatibility decision, not an accidental broadcast.
    """
    normalized = (kind or "").strip().lower()

    # Thread edits change the working set and its aggregate counts.
    if (
        normalized in {
            "open_thread", "close_thread", "idle_thread", "skill_materialized",
        }
        or normalized.startswith("note:")
    ):
        return frozenset({
            "memory://brief",
            "memory://context",
            "memory://dashboard",
        })

    # Signals are surfaced in the brief inbox; the dashboard reports their
    # aggregate count. They do not alter thread counts or process status.
    if normalized.startswith("signal:"):
        return frozenset({"memory://brief", "memory://dashboard"})

    # Task lifecycle and capacity events feed the live working set, dashboard,
    # and autonomous-status snapshot.
    if normalized.startswith(("spawn", "task_", "tournament", "spawn_budget")):
        return frozenset({
            "memory://brief",
            "memory://dashboard",
            "memory://agent-status",
        })

    # Learning-loop passes and their materialized outputs are dashboard and
    # agent-status data. A lesson/skill write is not injected into brief()
    # directly, so it must not spuriously refresh the brief resource.
    if (
        normalized.endswith("_pass")
        or normalized.startswith((
            "lesson_", "candidate_", "curator_", "dialectic_", "tier_", "extract_",
            "shadow_review", "evolve_apply", "roadmap_issue_",
        ))
    ):
        return frozenset({"memory://dashboard", "memory://agent-status"})

    # These tables are rendered by the brief, while the dashboard shows their
    # inventory/event aggregates. They don't change runtime context or daemon
    # liveness.
    if normalized.startswith((
        "core_", "style_", "verbatim_", "concept_", "distill_", "evolve_",
    )):
        return frozenset({"memory://brief", "memory://dashboard"})

    return frozenset()


def _resource_last_modified() -> dict[str, int]:
    """Return each resource's newest relevant committed event timestamp."""
    modified = {uri: _RESOURCE_BOOTSTRAPPED_AT for uri in MEMORY_RESOURCE_URIS}
    try:
        with read_db() as conn:
            rows = conn.execute(
                "SELECT kind, MAX(created_at) AS created_at FROM events GROUP BY kind"
            ).fetchall()
    except Exception:
        # Resource listing should remain available during a transient database
        # startup/lock failure; its short TTL makes this conservative fallback
        # safe until the next list/read.
        return modified
    for row in rows:
        for uri in resources_for_event(row["kind"]):
            modified[uri] = max(modified[uri], int(row["created_at"] or 0))
    return modified


def _freshness_metadata(uri: str, modified_at: int | None = None) -> dict[str, Any]:
    if modified_at is None:
        modified_at = _resource_last_modified().get(uri, _RESOURCE_BOOTSTRAPPED_AT)
    return {
        "cacheScope": RESOURCE_CACHE_SCOPE,
        "ttl": RESOURCE_TTL_SECONDS,
        "lastModified": _iso_timestamp(modified_at),
    }


@dataclass
class _ResourceSubscription:
    session: Any
    uri: str
    after_event_id: int


class _MemoryResourceSubscriptions:
    """Subscription registry plus a small committed-event poller.

    Background daemons can commit through a different process from the MCP
    request server. Polling the durable event log, rather than a process-local
    callback, keeps updates transaction-aware across that boundary. One poll
    batch becomes at most one notification per subscribed URI/session.
    """

    def __init__(self) -> None:
        self._subscriptions: list[_ResourceSubscription] = []
        self._last_event_id: int | None = None
        self._poller: asyncio.Task[None] | None = None

    @staticmethod
    def _latest_event_id() -> int:
        with read_db() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS id FROM events"
            ).fetchone()
        return int(row["id"])

    @property
    def count(self) -> int:
        return len(self._subscriptions)

    async def subscribe(self, uri: str, session: Any) -> None:
        if uri not in MEMORY_RESOURCE_URIS:
            raise ValueError(f"unknown memory resource: {uri}")
        latest = self._latest_event_id()
        if not self._subscriptions:
            self._last_event_id = latest
        self._subscriptions = [
            item for item in self._subscriptions
            if not (item.session is session and item.uri == uri)
        ]
        self._subscriptions.append(
            _ResourceSubscription(session=session, uri=uri, after_event_id=latest)
        )
        if self._poller is None or self._poller.done():
            self._poller = asyncio.create_task(self._poll_loop())

    async def unsubscribe(self, uri: str, session: Any) -> None:
        self._subscriptions = [
            item for item in self._subscriptions
            if not (item.session is session and item.uri == uri)
        ]
        if not self._subscriptions:
            self._last_event_id = None

    async def _poll_loop(self) -> None:
        try:
            while self._subscriptions:
                await asyncio.sleep(_SUBSCRIPTION_POLL_SECONDS)
                await self.poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("memory resource subscription poller stopped", exc_info=True)

    async def poll_once(self) -> None:
        """Deliver one coalesced committed-event batch to current subscribers."""
        if not self._subscriptions:
            return
        if self._last_event_id is None:
            self._last_event_id = self._latest_event_id()
            return
        try:
            with read_db() as conn:
                rows = conn.execute(
                    "SELECT id, kind, created_at FROM events WHERE id>? ORDER BY id",
                    (self._last_event_id,),
                ).fetchall()
        except Exception:
            logger.debug("memory resource update poll failed", exc_info=True)
            return
        if not rows:
            return

        self._last_event_id = int(rows[-1]["id"])
        pending: dict[tuple[int, str], tuple[Any, int]] = {}
        for row in rows:
            event_id = int(row["id"])
            changed_at = int(row["created_at"])
            for uri in resources_for_event(row["kind"]):
                for subscription in self._subscriptions:
                    if subscription.uri != uri or event_id <= subscription.after_event_id:
                        continue
                    key = (id(subscription.session), uri)
                    previous = pending.get(key)
                    pending[key] = (
                        subscription.session,
                        max(changed_at, previous[1]) if previous else changed_at,
                    )

        failed_sessions: set[int] = set()
        for (session_id, uri), (session, changed_at) in pending.items():
            try:
                params = types.ResourceUpdatedNotificationParams(
                    uri=uri,
                    _meta=_freshness_metadata(uri, changed_at),
                )
                await session.send_notification(
                    types.ServerNotification(
                        types.ResourceUpdatedNotification(params=params)
                    )
                )
            except Exception:
                # A disconnected client cannot receive future updates. Remove
                # its subscriptions but never let it block healthy clients.
                failed_sessions.add(session_id)
                logger.debug("memory resource notification failed", exc_info=True)
        if failed_sessions:
            self._subscriptions = [
                item for item in self._subscriptions
                if id(item.session) not in failed_sessions
            ]


_subscriptions = _MemoryResourceSubscriptions()


@mcp.resource(
    "memory://brief",
    name="brief",
    title="thread-keeper memory brief",
    description="Session-start memory brief: core memory, open/idle/closed "
    "threads, live peers, style, verbatim, user-model. Read-only, rendered "
    "lean (no behavioral nudges, no side effects). Mirrors the brief() tool.",
    mime_type="text/plain",
    annotations=Annotations(audience=["assistant"], priority=_RESOURCE_PRIORITIES["memory://brief"]),
    meta={"cacheScope": RESOURCE_CACHE_SCOPE, "ttl": RESOURCE_TTL_SECONDS},
)
def brief_resource() -> str:
    conn = get_db()
    _ensure_session(conn)
    # lean=True keeps the pull side-effect-free: the spawn/thread/skill hint
    # blocks (which write *_hint_shown events) are all gated on `not eff_lean`.
    return render_brief(conn, scope="full", lean=True)


@mcp.resource(
    "memory://context",
    name="context",
    title="thread-keeper runtime context",
    description="Runtime context: session id, age, semantic on/off, db path, "
    "thread counts. Read-only. Mirrors the context() tool's text block.",
    mime_type="text/plain",
    annotations=Annotations(audience=["assistant"], priority=_RESOURCE_PRIORITIES["memory://context"]),
    meta={"cacheScope": RESOURCE_CACHE_SCOPE, "ttl": RESOURCE_TTL_SECONDS},
)
def context_resource() -> str:
    conn = get_db()
    _ensure_session(conn)
    text, _ = render_context(conn)
    return text


@mcp.resource(
    "memory://dashboard",
    name="dashboard",
    title="thread-keeper system dashboard",
    description="One-call rollup: store sizes, autonomous-loop fire counts, and "
    "what those loops produced. Read-only. Mirrors the mp_dashboard() tool.",
    mime_type="text/plain",
    annotations=Annotations(audience=["assistant"], priority=_RESOURCE_PRIORITIES["memory://dashboard"]),
    meta={"cacheScope": RESOURCE_CACHE_SCOPE, "ttl": RESOURCE_TTL_SECONDS},
)
def dashboard_resource() -> str:
    # mp_dashboard() is the read_tool() function; FastMCP leaves it directly
    # callable. It opens its own db handle and is defensive on partial schemas.
    return mp_dashboard()


@mcp.resource(
    "memory://agent-status",
    name="agent-status",
    title="thread-keeper autonomous-loop status",
    description="Autonomous learning loops: state, backlog, last pass, RSS. "
    "Read-only cached snapshot (refresh=False, no process re-scan). Mirrors "
    "the agent_status() tool's formatted summary.",
    mime_type="text/plain",
    annotations=Annotations(audience=["assistant"], priority=_RESOURCE_PRIORITIES["memory://agent-status"]),
    meta={"cacheScope": RESOURCE_CACHE_SCOPE, "ttl": RESOURCE_TTL_SECONDS},
)
def agent_status_resource() -> str:
    return format_agent_status(agent_status_snapshot(refresh=False))


# FastMCP 1.x exposes resource subscription hooks on the low-level server but
# does not register them for decorated resources. Keep the bridge local to the
# four memory URIs, then advertise it through normal MCP capabilities.
@mcp._mcp_server.subscribe_resource()
async def _subscribe_memory_resource(uri) -> None:
    uri_text = str(uri)
    if uri_text not in MEMORY_RESOURCE_URIS:
        raise ValueError(f"unknown memory resource: {uri_text}")
    await _subscriptions.subscribe(uri_text, mcp._mcp_server.request_context.session)


@mcp._mcp_server.unsubscribe_resource()
async def _unsubscribe_memory_resource(uri) -> None:
    await _subscriptions.unsubscribe(str(uri), mcp._mcp_server.request_context.session)


_original_get_capabilities = mcp._mcp_server.get_capabilities


def _memory_resource_capabilities(notification_options, experimental_capabilities):
    capabilities = _original_get_capabilities(
        notification_options, experimental_capabilities
    )
    if capabilities.resources is not None:
        capabilities.resources.subscribe = True
    return capabilities


mcp._mcp_server.get_capabilities = _memory_resource_capabilities


_original_list_resources = mcp.list_resources
_original_read_resource = mcp.read_resource


def _contents_size(contents) -> int:
    size = 0
    for content in contents:
        value = content.content
        size += len(value if isinstance(value, bytes) else value.encode("utf-8"))
    return size


async def _list_memory_resources_with_metadata():
    """Attach current private-cache freshness metadata to resource listings.

    Size is calculated from the same side-effect-free snapshot implementation
    a client reads, rather than guessed from an earlier render.
    """
    listed = await _original_list_resources()
    modified = _resource_last_modified()
    enriched = []
    for resource in listed:
        uri = str(resource.uri)
        if uri not in MEMORY_RESOURCE_URIS:
            enriched.append(resource)
            continue
        try:
            contents = await _original_read_resource(uri)
            size = _contents_size(contents)
        except Exception:
            # Size is optional in MCP. Preserve a usable listing if a dynamic
            # snapshot temporarily cannot render.
            size = None
        freshness = _freshness_metadata(uri, modified[uri])
        enriched.append(resource.model_copy(update={
            "annotations": Annotations(
                audience=["assistant"],
                priority=_RESOURCE_PRIORITIES[uri],
                lastModified=freshness["lastModified"],
            ),
            "size": size,
            "meta": freshness,
        }))
    return enriched


async def _read_memory_resource_with_metadata(uri):
    # Reads remain exactly the former pull-only behavior. Metadata lives in
    # resources/list and update notifications; no memory body is put in a
    # notification payload.
    return await _original_read_resource(uri)


# Patch FastMCP's public methods and rebind its already-created low-level
# handlers so direct Python callers and protocol clients see the same contract.
mcp.list_resources = _list_memory_resources_with_metadata
mcp.read_resource = _read_memory_resource_with_metadata
mcp._mcp_server.list_resources()(mcp.list_resources)
mcp._mcp_server.read_resource()(mcp.read_resource)
