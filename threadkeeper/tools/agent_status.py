"""MCP wrapper for structured autonomous loop status."""
from __future__ import annotations

import json

from .._mcp import read_tool, write_tool, structured_result
from ..agent_status import (
    agent_status_snapshot,
    format_agent_status,
    format_memory_cleanup,
    memory_cleanup,
)
from ..tool_schemas import AgentStatusSnapshot


@read_tool()
def agent_status(json_output: bool = False, refresh: bool = True) -> AgentStatusSnapshot:
    """Show autonomous learning loops with state, backlog, last pass, and RSS.

    Set json_output=True for the same stable shape used by the menu-bar app.
    Always returns structuredContent (AgentStatusSnapshot); the text block is
    the JSON dump when json_output else the formatted summary.
    """
    snapshot = agent_status_snapshot(refresh=refresh)
    if json_output:
        text = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    else:
        text = format_agent_status(snapshot)
    return structured_result(text, AgentStatusSnapshot(**snapshot))


@write_tool(destructive=True)
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
