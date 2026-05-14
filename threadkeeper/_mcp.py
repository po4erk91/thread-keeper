"""Singleton FastMCP instance shared by every tool module. All
@mcp.tool() definitions across the package register on this same instance,
so server.py can simply import every tool module and call mcp.run()."""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("thread-keeper")
