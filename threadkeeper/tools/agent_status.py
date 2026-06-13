"""MCP wrapper for structured autonomous loop status."""
from __future__ import annotations

import json

from .._mcp import mcp
from ..agent_status import (
    agent_status_snapshot,
    format_agent_status,
    format_memory_cleanup,
    memory_cleanup,
)


@mcp.tool()
def agent_status(json_output: bool = False, refresh: bool = True) -> str:
    """Show autonomous learning loops with state, backlog, last pass, and RSS.

    Set json_output=True for the same stable shape used by the menu-bar app.
    """
    snapshot = agent_status_snapshot(refresh=refresh)
    if json_output:
        return json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    return format_agent_status(snapshot)


@mcp.tool()
def agent_memory_cleanup(
    json_output: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> str:
    """Trim ThreadKeeper memory and clean orphan/over-limit server processes.

    By default this applies the safe cleanup path. Set dry_run=True to inspect
    the plan first. It does not kill active spawned child agents.
    """
    result = memory_cleanup(dry_run=dry_run, force=force)
    if json_output:
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    return format_memory_cleanup(result)
