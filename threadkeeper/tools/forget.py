"""Privacy erasure MCP tool."""
from __future__ import annotations

from .._mcp import write_tool
from ..forget import forget_selector


@write_tool(destructive=True, idempotent=True)
def forget(
    selector: str,
    selector_type: str = "auto",
    dry_run: bool = True,
) -> str:
    """Forget one session/cid/thread/dialog UUID.

    Defaults to dry-run and reports affected rows per store. Set
    ``dry_run=False`` to delete dialog rows, FTS/vector sidecars, directly
    sourced dialectic/verbatim/extract/task records, and matching task spool
    files. Lessons and skills that cite the selector are listed for manual
    re-review instead of silently retained.
    """
    return forget_selector(
        selector,
        selector_type=selector_type,
        dry_run=dry_run,
    )
