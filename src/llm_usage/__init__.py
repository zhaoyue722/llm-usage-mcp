"""Package entrypoint for `llm-usage-mcp`.

`main()` is the target of the console script declared in
`pyproject.toml` (`llm-usage-mcp = "llm_usage:main"`). It is also the
target of `python -m llm_usage` via `__main__.py`. It runs schema
migrations, materializes the vendored pricing on a fresh database,
then hands control to the FastMCP server over stdio — the transport
`claude mcp add llm-usage uv run llm-usage-mcp` expects.

`Settings.require_keys()` is intentionally **not** called here: the
MCP server is usable read-only against an existing database without
any provider API keys. The future capture proxy is the natural
consumer of the refuse-to-start gate.
"""

from __future__ import annotations

import logging

from llm_usage.bootstrap import bootstrap
from llm_usage.config import get_settings
from llm_usage.mcp.server import server


def main() -> None:
    """Boot the MCP server: configure logging, bootstrap, run stdio."""
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    bootstrap()
    server.run()  # blocks; default transport is stdio
