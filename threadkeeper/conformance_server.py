"""Test-only Streamable HTTP transport for MCP conformance checks.

Production starts :mod:`threadkeeper.server` over stdio.  This module imports
that entry point only to assemble its normal tool, resource, prompt, extension,
and schema registry, then exposes the same ``mcp`` instance over loopback HTTP.
It is used by CI and is not installed as a console script or configurable
production transport.
"""
from __future__ import annotations

import argparse

from . import server as _server  # Registers the complete production catalog.
from ._mcp import mcp


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    mcp.run(
        "streamable-http",
        host=args.host,
        port=args.port,
        streamable_http_path="/mcp",
    )


if __name__ == "__main__":
    main()
