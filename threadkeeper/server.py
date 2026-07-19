"""threadkeeper.server — package entry point.

Importing each tools module triggers its @mcp.tool() decorators against the
shared FastMCP singleton in threadkeeper._mcp. After all imports, the
runtime is fully assembled; mcp.run() starts the stdio MCP loop.

To launch:
    python -m threadkeeper.server
"""
from __future__ import annotations

# Singleton must be importable before tool modules.
from ._mcp import mcp

# Core modules — registered for completeness; no @mcp.tool() decorators here.
# Import them so any import-time side effects happen in a predictable order.
from . import config  # noqa: F401
from . import db  # noqa: F401
from . import helpers  # noqa: F401
from . import identity  # noqa: F401
from . import embeddings  # noqa: F401
from . import ingest  # noqa: F401
from . import brief  # noqa: F401

# Tool modules — each import registers a group of @mcp.tool() entries on
# the shared mcp instance. Order is deliberate: peers/spawn first because
# pickup imports spawn for auto_spawn; brief is already imported above so
# tools.threads can pull render_brief.
from .tools import threads  # noqa: F401
from .tools import style  # noqa: F401
from .tools import peers  # noqa: F401
from .tools import spawn  # noqa: F401
from .tools import probes  # noqa: F401
from .tools import concepts  # noqa: F401
from .tools import distill  # noqa: F401
from .tools import core_memory  # noqa: F401
from .tools import graph  # noqa: F401
from .tools import correlation  # noqa: F401
from .tools import extract  # noqa: F401
from .tools import consolidate  # noqa: F401
from .tools import validate  # noqa: F401
from .tools import invariants  # noqa: F401
from .tools import missed_spawns  # noqa: F401
from .tools import dialog  # noqa: F401
from .tools import pickup  # noqa: F401
from .tools import session  # noqa: F401
from .tools import skills  # noqa: F401
from .tools import dialectic  # noqa: F401
from .tools import panel  # noqa: F401
from .tools import process_health  # noqa: F401
from .tools import memory_guard  # noqa: F401
from .tools import shadow_review  # noqa: F401
from .tools import lessons  # noqa: F401
from .tools import curator  # noqa: F401
from .tools import candidate_reviewer  # noqa: F401
from .tools import evolve_applier  # noqa: F401
from .tools import dialectic_feed  # noqa: F401
from .tools import dashboard  # noqa: F401
from .tools import agent_status  # noqa: F401
from .tools import config_watch  # noqa: F401
from .tools import db_maintenance  # noqa: F401
from .tools import forget  # noqa: F401

# MCP Resources & Prompts (roadmap #78) — the other two MCP server primitives.
# Importing these registers @mcp.resource / @mcp.prompt entries (NOT tools) on
# the shared instance: read-only memory snapshots as resources, curation/audit
# flows as prompts. Additive — every existing tool stays unchanged, and hosts
# without the resources/prompts capability fall back to the tool-only surface.
from .tools import resources  # noqa: F401
from .tools import prompts  # noqa: F401
from .tools import sync  # noqa: F401


if __name__ == "__main__":
    from .menubar_app import ensure_menubar_app

    ensure_menubar_app()
    mcp.run()
