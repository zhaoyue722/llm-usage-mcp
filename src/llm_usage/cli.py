"""Typer multi-command CLI for `llm-usage`.

`pyproject.toml` declares three scripts that all land here:

- `llm-usage` → `main()` — the primary entry. Dispatches to subcommands
  (`llm-usage proxy`, `llm-usage compare`, …).
- `llm-usage-proxy` → `proxy_main()` — backward-compat alias documented
  in the README; runs the `proxy` command directly without going
  through the subcommand layer.
- `llm-usage-mcp` → `llm_usage:main` — lives in the package root, not
  here, because the stdio MCP server has a different boot sequence
  and no argv parsing.

The CLI talks to the same `core/` layer the MCP server does — `compare`
calls into `core.compare.project_costs`, so the two surfaces stay in
lockstep on ranking semantics. Rendering for human-readable output is
delegated to `cli_render.py`; this module owns argument parsing,
color-mode resolution, JSON-vs-text mode selection, and writing to
stdout.
"""

from __future__ import annotations

import json
import os
import sys
from enum import StrEnum

import typer

from llm_usage.capture.proxy import run_proxy
from llm_usage.cli_render import format_compare_result
from llm_usage.core.compare import project_costs
from llm_usage.core.db.session import get_session

app = typer.Typer(
    name="llm-usage",
    help="Local-first LLM spend capture + query, exposed over MCP.",
    no_args_is_help=True,
    add_completion=False,
)


class ColorMode(StrEnum):
    """`--color` resolution for human-readable output."""

    auto = "auto"
    always = "always"
    never = "never"


@app.command()
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

    Forwards Anthropic `/v1/messages` and OpenAI-compatible
    `/{provider}/v1/chat/completions` to the upstream provider, parses
    the usage block off each response, and records to the local
    SQLite. The proxy is loopback-only by design and never reachable
    from the network. Per-route requests return 503 if their API key
    isn't configured, so a user dogfooding one provider doesn't have
    to set four keys upfront.
    """
    run_proxy(port=port, log_level=log_level)


@app.command()
def compare(
    input_tokens: int = typer.Option(
        ...,
        "--input",
        "--in",
        "-i",
        help="Expected input tokens for the projected workload.",
    ),
    output_tokens: int = typer.Option(
        ...,
        "--output",
        "--out",
        "-o",
        help="Expected output tokens for the projected workload.",
    ),
    models: list[str] | None = typer.Option(
        None,
        "--models",
        help=(
            "Restrict the comparison to these model names. Repeatable: "
            "`--models a --models b`. Default: every priced model."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the same JSON the MCP `compare_providers` tool returns. Pipe-friendly.",
    ),
    color: ColorMode = typer.Option(
        ColorMode.auto,
        "--color",
        help="auto = color on TTY only (respects NO_COLOR); always = force on; never = force off.",
    ),
    bar_width: int = typer.Option(
        14,
        "--bar-width",
        min=4,
        max=80,
        help="Width of the inline bar track in cells.",
    ),
) -> None:
    """Project the cost of a hypothetical workload across every priced model.

    Cheapest first, with `%` measured against the cheapest entry
    (cheapest = 100%). Cost is computed from input/output tokens only
    in v1; cache pricing is shown as a footnote so users know it's not
    yet applied. `--json` returns the same Pydantic shape the MCP
    `compare_providers` tool produces, so existing schemas / consumers
    work verbatim.
    """
    with get_session() as session:
        result = project_costs(
            session,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            models=models if models else None,
        )

    if json_output:
        typer.echo(json.dumps(result.model_dump(), indent=2))
        return

    color_enabled = _resolve_color(color)
    typer.echo(
        format_compare_result(
            result,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            color_enabled=color_enabled,
            bar_width=bar_width,
        ),
        # Override click's default "strip ANSI on non-TTY" — the renderer
        # only emits escapes when *we* decided color is appropriate, so
        # this flag follows the same decision. Without it, `--color always`
        # piped to a file (or captured by CliRunner) silently produces
        # plain text.
        color=color_enabled,
    )


def _resolve_color(mode: ColorMode) -> bool:
    """Decide whether to emit ANSI escapes for human-readable output.

    Honors three signals in priority order:
      1. `--color` flag, when not `auto`.
      2. `NO_COLOR` env var — the cross-tool standard
         (https://no-color.org/) for users who want monochrome
         regardless of TTY state.
      3. TTY detection: emit color only when stdout is a real terminal,
         so piping to a file or another tool doesn't pollute the
         downstream consumer with escape bytes.
    """
    if mode is ColorMode.always:
        return True
    if mode is ColorMode.never:
        return False
    # auto:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def main() -> None:
    """`llm-usage` console-script entry point — runs the multi-command app."""
    app()


def proxy_main() -> None:
    """`llm-usage-proxy` console-script entry point (backward-compat alias).

    Wraps the `proxy` command in a single-command Typer app so existing
    `uv run llm-usage-proxy --port 5555` invocations keep working. The
    underlying function is the same one `llm-usage proxy` dispatches
    to, so there's only one implementation of the proxy CLI.
    """
    standalone = typer.Typer(name="llm-usage-proxy", add_completion=False)
    standalone.command()(proxy)
    standalone()


__all__ = ["app", "compare", "main", "proxy", "proxy_main"]
