"""Singleton MCPServer instance shared by every tool module. All
@mcp.tool() definitions across the package register on this same instance,
so server.py can simply import every tool module and call mcp.run().

Tools are registered through two thin wrappers around ``mcp.tool`` that
attach MCP 2025-06-18 ``ToolAnnotations`` so clients can tell reads from
writes without calling them:

  * ``@read_tool()``  — pure query, no state mutation (``readOnlyHint=True``).
  * ``@write_tool()`` — mutates state (``readOnlyHint=False``); pass
    ``destructive=True`` for delete/overwrite/kill tools and
    ``idempotent=True`` where a repeat call is a no-op.

This static metadata layer is what a confirmation/elicitation client reads
to decide which calls warrant a prompt (roadmap #67; substrate for #26).
"""
import secrets

from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.request_state import RequestStateSecurity
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

from .protocol import ReplaySafeWrites, caller_principal


# Discovery is static for one server process: decorators register the complete
# catalog before ``mcp.run()`` begins. These public hints let 2026 clients cache
# it without promising that user-owned resource *contents* are static.
_CATALOG_CACHE_HINTS = {
    "tools/list": CacheHint(ttl_ms=3_600_000, scope="public"),
    "resources/list": CacheHint(ttl_ms=3_600_000, scope="public"),
    "resources/templates/list": CacheHint(ttl_ms=3_600_000, scope="public"),
    "prompts/list": CacheHint(ttl_ms=3_600_000, scope="public"),
    "server/discover": CacheHint(ttl_ms=3_600_000, scope="public"),
}

# The set is populated by ``write_tool`` as modules register their tools. The
# replay guard retains this object, so it sees the finished catalog once
# stdio serving starts without duplicating business-tool metadata.
WRITE_TOOL_NAMES: set[str] = set()

mcp = MCPServer(
    "thread-keeper",
    version="0.17.0",
    cache_hints=_CATALOG_CACHE_HINTS,
    request_state_security=RequestStateSecurity(
        keys=[secrets.token_bytes(32)],
        ttl=300,
        bind_principal=caller_principal,
    ),
    middleware=[ReplaySafeWrites(WRITE_TOOL_NAMES)],
)

def read_tool(**kwargs):
    """Register a read-only MCP tool (``readOnlyHint=True``).

    Use for pure queries that do not modify thread-keeper state — briefs,
    searches, status snapshots, listings. Extra kwargs pass through to
    ``mcp.tool`` (e.g. ``name=``)."""
    return mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True),
        **kwargs,
    )


def write_tool(*, destructive: bool = False, idempotent: bool = False, **kwargs):
    """Register a state-mutating MCP tool (``readOnlyHint=False``).

    ``destructive=True`` sets ``destructiveHint=True`` for tools that delete,
    overwrite, archive, or kill (``compost`` excluded — it only reads).
    ``idempotent=True`` sets ``idempotentHint=True`` where repeating the call
    is a no-op (closing an already-closed thread, deleting a missing key)."""
    register = mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=destructive,
            idempotentHint=idempotent,
        ),
        **kwargs,
    )

    def decorate(fn):
        WRITE_TOOL_NAMES.add(kwargs.get("name") or fn.__name__)
        return register(fn)

    return decorate


def structured_result(text: str, model: BaseModel) -> CallToolResult:
    """Build a CallToolResult that carries BOTH the legacy human-readable
    ``text`` block AND ``model`` as machine-readable ``structuredContent``.

    The tool's return annotation (a pydantic model) supplies the advertised
    ``outputSchema`` in ``tools/list``; this helper keeps the serialized text
    block for backward compatibility, as the MCP 2025-06-18 spec recommends
    for tools that emit structured content."""
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=model.model_dump(mode="json", by_alias=True),
    )
