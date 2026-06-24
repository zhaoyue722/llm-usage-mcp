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
    _print_startup_banner()
    server.run()  # blocks; default transport is stdio


def _print_startup_banner() -> None:
    """Print the watch-pom banner to stderr — only when stderr is a TTY.

    Stdout carries the JSON-RPC stream, so the banner must never go there.
    When an MCP client launches the server its stderr is piped (not a
    TTY), so we stay silent — no log noise. The banner only appears when
    someone runs `llm-usage-mcp` directly in a terminal to poke at it.
    Best-effort: a banner failure must never stop the server booting.
    """
    import os
    import sys

    if not sys.stderr.isatty():
        return
    try:
        from llm_usage.capture.proxy import _banner_db_path, _banner_version
        from llm_usage.cli_render import format_mcp_banner

        banner = format_mcp_banner(
            version=_banner_version(),
            db_path=_banner_db_path(get_settings().db_url),
            color_enabled=not bool(os.environ.get("NO_COLOR")),
        )
        print(banner, file=sys.stderr)
    except Exception:  # cosmetics must not break boot
        logging.getLogger(__name__).debug("startup banner skipped", exc_info=True)
