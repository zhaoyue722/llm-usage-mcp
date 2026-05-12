"""MCP server package — see `llm_usage.mcp.server` for the FastMCP instance and tool stubs.

`__init__.py` deliberately does NOT re-export the FastMCP instance, because
that would shadow the `server` submodule (Python resolves attribute lookups
on the package namespace, and a re-exported instance named `server` would
take precedence over the submodule of the same name). Import the instance
explicitly: `from llm_usage.mcp.server import server`.
"""
