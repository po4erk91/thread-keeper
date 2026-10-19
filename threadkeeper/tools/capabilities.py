"""Deferred capability discovery for MCP hosts without native tool search."""
from __future__ import annotations

from .._mcp import active_tool_names, read_tool
from ..tool_profiles import deferred_capabilities


@read_tool()
def capability_search(query: str = "") -> dict[str, object]:
    """Find hidden capability families without exposing their concrete tools.

    Select the returned profile when starting the server to advertise that
    family. A search result is descriptive only; it cannot enable any tool.
    """
    return deferred_capabilities(query, active_tool_names())
