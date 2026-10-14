"""Internal contract for interpreting the public ``spawn()`` text result.

``spawn()`` keeps returning text for MCP compatibility.  Python callers must
use this parser instead of treating a non-exceptional return as a launch.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


_TASK_ID_RE = re.compile(r"\btask(?:_id)?=([^\s]+)")


@dataclass(frozen=True)
class SpawnResult:
    """A parsed spawn response with the original public text preserved."""

    text: str
    ok: bool
    task_id: str | None = None
    reason: str = ""


def parse_spawn_result(value: object) -> SpawnResult:
    """Return a launch result only when spawn reported a task identifier.

    Admission and launch failures are returned as ``ERR ...`` text, not raised
    exceptions.  Missing task identifiers are failures too: callers cannot
    safely treat an unrecognized response as a launched child.
    """
    text = str(value).strip()
    if text.startswith("ERR"):
        return SpawnResult(
            text=text,
            ok=False,
            reason=text[3:].strip() or "spawn_failed",
        )
    match = _TASK_ID_RE.search(text)
    if match:
        return SpawnResult(text=text, ok=True, task_id=match.group(1))
    return SpawnResult(
        text=text,
        ok=False,
        reason="invalid_spawn_result: missing task id",
    )
