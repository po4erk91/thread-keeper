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
from .tools import invariants  # noqa: F401
from .tools import missed_spawns  # noqa: F401
from .tools import dialog  # noqa: F401
from .tools import pickup  # noqa: F401
from .tools import session  # noqa: F401
from .tools import skills  # noqa: F401
from .tools import dialectic  # noqa: F401
from .tools import process_health  # noqa: F401
from .tools import shadow_review  # noqa: F401
from .tools import lessons  # noqa: F401


if __name__ == "__main__":
    mcp.run()
