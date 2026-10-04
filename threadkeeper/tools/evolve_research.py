"""Narrow persistence surface for Evolve web-research digests.

The research child intentionally has no generic filesystem write capability.
This tool accepts content for a parent-registered pass and delegates all
ownership, destination, size, and final-hash checks to ``evolve_daemon``.
"""
from __future__ import annotations

from .._mcp import write_tool
from ..evolve_daemon import submit_research_handoff


@write_tool(idempotent=True)
def evolve_research_handoff(pass_id: str, content: str) -> str:
    """Submit one bounded digest for an assigned Evolve research pass.

    This tool deliberately has no destination argument. Only the spawned
    ``evolve_researcher`` that owns the parent-authorized ``pass_id`` can make
    one atomic submission; malformed, oversized, stale, replayed, or
    unowned submissions are refused and recorded as handoff telemetry.
    """
    return submit_research_handoff(pass_id, content)
