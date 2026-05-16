"""Typer CLI for `llm-usage-proxy` (and future subcommands).

`pyproject.toml` declares `llm-usage-proxy = "llm_usage.cli:main"`,
making `main()` the entry the installed `llm-usage-proxy` script calls.
Today it runs the capture proxy; future commands (pricing refresh,
quality importer, etc.) land here as additional `typer.run`-style
wrappers or as subcommands when there's more than one.

The MCP server stays on its own dedicated script (`llm-usage-mcp`,
target `llm_usage:main`) — split entry points are simpler than a
single dispatcher and preserve the boot sequence each transport
expects (stdio for MCP, uvicorn for the proxy).
"""

from __future__ import annotations

import typer

from llm_usage.capture.proxy import run_proxy


def proxy(
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help="TCP port to bind. Defaults to LLM_USAGE_PROXY_PORT (5525).",
    ),
    log_level: str | None = typer.Option(
        None,
        "--log-level",
        help="Log verbosity. Defaults to LLM_USAGE_LOG_LEVEL (INFO).",
    ),
) -> None:
    """Run the local LLM capture proxy on 127.0.0.1.

    Forwards Anthropic `/v1/messages` calls to api.anthropic.com (or
    the configured base URL) and records token usage to the local
    SQLite. ANTHROPIC_API_KEY must be set. Streaming requests are
    rejected with 400 in this release. The proxy is loopback-only
    by design and never reachable from the network.
    """
    run_proxy(port=port, log_level=log_level)


def main() -> None:
    """`llm-usage-proxy` console-script entry point."""
    typer.run(proxy)


__all__ = ["main", "proxy"]
