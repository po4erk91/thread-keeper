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
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import AnyUrl
from pydantic import BaseModel

from .mcp_skills import (
    MAX_SKILL_RESOURCE_BYTES,
    SKILL_URI_ORIGIN,
    SkillsExtension,
    read_skill_resource,
    resource_catalog,
)


class ThreadKeeperMCPServer(MCPServer):
    """MCPServer with a live, read-only view of canonical skill files.

    The SDK resource manager owns the static memory resources.  Skills change
    while this server is running, so their resource metadata is rebuilt for
    each list/read request instead of registering stale copies at startup.
    """

    async def list_resources(self):
        static_resources = await super().list_resources()
        return [*static_resources, *resource_catalog()]

    async def read_resource(self, uri: AnyUrl | str, context=None):
        value = str(uri)
        if value.startswith(f"skill://{SKILL_URI_ORIGIN}/"):
            item = read_skill_resource(value)
            try:
                data = item.path.read_bytes()
            except OSError as exc:
                raise ResourceNotFoundError(f"Unknown resource: {value}") from exc
            if len(data) > MAX_SKILL_RESOURCE_BYTES:
                raise ResourceNotFoundError(f"Unknown resource: {value}")
            # The client checks the catalog digest.  This read is delivery only;
            # it does not activate the skill or record any usage telemetry.
            return [ReadResourceContents(content=data, mime_type=item.mime_type)]
        return await super().read_resource(uri, context)

mcp = ThreadKeeperMCPServer("thread-keeper", extensions=[SkillsExtension()])


def read_tool(**kwargs):
    """Register a read-only MCP tool (``readOnlyHint=True``).

    Use for pure queries that do not modify thread-keeper state — briefs,
    searches, status snapshots, listings. Extra kwargs pass through to
    ``mcp.tool`` (e.g. ``name=``)."""
    return mcp.tool(
        annotations=ToolAnnotations(read_only_hint=True),
        **kwargs,
    )


def write_tool(*, destructive: bool = False, idempotent: bool = False, **kwargs):
    """Register a state-mutating MCP tool (``readOnlyHint=False``).

    ``destructive=True`` sets ``destructiveHint=True`` for tools that delete,
    overwrite, archive, or kill (``compost`` excluded — it only reads).
    ``idempotent=True`` sets ``idempotentHint=True`` where repeating the call
    is a no-op (closing an already-closed thread, deleting a missing key)."""
    return mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=destructive,
            idempotent_hint=idempotent,
        ),
        **kwargs,
    )


def structured_result(text: str, model: BaseModel) -> CallToolResult:
    """Build a CallToolResult that carries BOTH the legacy human-readable
    ``text`` block AND ``model`` as machine-readable ``structuredContent``.

    The tool's return annotation (a pydantic model) supplies the advertised
    ``outputSchema`` in ``tools/list``; this helper keeps the serialized text
    block for backward compatibility, as the MCP 2025-06-18 spec recommends
    for tools that emit structured content."""
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=model.model_dump(mode="json", by_alias=True),
    )
