"""Observable confirmation boundary for memory-driven consequential actions."""
from __future__ import annotations

from .._mcp import read_tool
from ..authority import authorize_action
from ..db import get_db


@read_tool()
def memory_authorize_action(
    artifact_kind: str,
    artifact_id: str,
    confirmed: bool = False,
) -> str:
    """Check the authority gate before a consequential memory-driven action.

    Low-authority memory is denied unless it has an independent trusted root
    or the caller explicitly records confirmation with ``confirmed=True``.
    """
    decision = authorize_action(get_db(), artifact_kind.strip(), artifact_id.strip(), confirmed=confirmed)
    return ("allow" if decision.allowed else "deny") + f" reason={decision.reason}"
