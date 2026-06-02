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
import time
from enum import StrEnum

import typer

from llm_usage.capture.proxy import run_proxy
from llm_usage.cli_render import (
    format_compare_result,
    format_providers,
    format_recommend_result,
    format_spend_groups,
    format_status,
    format_usage_summary,
)
from llm_usage.config import get_settings
from llm_usage.core.compare import project_costs
from llm_usage.core.db.session import get_session
from llm_usage.core.diagnostics import collect_status
from llm_usage.core.models import GroupBy, Period, SpendFilter
from llm_usage.core.providers import collect_providers
from llm_usage.core.recommend import recommend as _recommend_core
from llm_usage.core.spend import aggregate_spend, period_window, summarize_usage

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
        "--model",
        help=(
            "Restrict the comparison to these model names. Repeatable: "
            "`--model a --model b`. Default: every priced model."
        ),
    ),
    show_all: bool = typer.Option(
        False,
        "--all",
        help=(
            "Show every catalog row, including alias/snapshot variants that "
            "share a price (e.g., `gpt-5-mini` and `gpt-5-mini-2025-08-07`). "
            "Default: collapse same-price same-family variants into one row "
            "with ×N indicating the catalog count."
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
    yet applied.

    Default view family-dedups catalog rows that share both a model-
    family root AND an identical projected cost — so `gpt-5-mini` and
    `gpt-5-mini-2025-08-07` (alias + pinned snapshot, same price)
    collapse to one row with `×2`. Pass `--all` to see every catalog
    row. When variants in the same family have *different* prices
    (rare but happens), both rows appear regardless of `--all`.

    `--json` returns the same Pydantic shape the MCP
    `compare_providers` tool produces, so existing schemas / consumers
    work verbatim.
    """
    with get_session() as session:
        result = project_costs(
            session,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            models=models if models else None,
            include_snapshots=show_all,
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


@app.command()
def recommend(
    task: str = typer.Option(
        ...,
        "--task",
        "-t",
        help="Free-form description of the work. Echoed into the reasoning; doesn't drive selection.",
    ),
    input_tokens: int | None = typer.Option(
        None,
        "--input",
        "--in",
        "-i",
        help="Expected input tokens. Defaults to a nominal 1,000 (the reasoning flags the default).",
    ),
    output_tokens: int | None = typer.Option(
        None,
        "--output",
        "--out",
        "-o",
        help="Expected output tokens. Defaults to a nominal 1,000.",
    ),
    budget_usd: float | None = typer.Option(
        None,
        "--budget",
        "-b",
        help="Max USD per call. When set, filters to affordable models; falls back to cheapest overall if nothing fits.",
    ),
    providers: list[str] | None = typer.Option(
        None,
        "--provider",
        help=(
            "Restrict to these providers. Repeatable: "
            "`--provider openai --provider deepseek`. Default: every priced provider."
        ),
    ),
    models: list[str] | None = typer.Option(
        None,
        "--model",
        help=(
            "Restrict to these model names. Repeatable: "
            "`--model gpt-5-mini --model claude-sonnet-4-6`. Default: every priced model."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the same JSON the MCP `recommend_provider` tool returns. Pipe-friendly.",
    ),
    color: ColorMode = typer.Option(
        ColorMode.auto,
        "--color",
        help="auto = color on TTY only (respects NO_COLOR); always = force on; never = force off.",
    ),
) -> None:
    """Recommend the cheapest priced model for a workload + budget.

    v1 ranks by cost only — `--task` is echoed into the reasoning so
    the chosen model is grounded in the user's prompt, but it doesn't
    drive selection (the tool isn't an LLM and can't interpret free
    text). A future release will incorporate quality benchmarks via
    the `quality_snapshot` table.

    `--in` / `--out` default to a nominal 1,000 / 1,000 each; the
    reasoning flags the defaults so the caller knows the estimate is
    rough. `--budget` filters to affordable models — if nothing fits,
    falls back to the cheapest overall and the reasoning says so.

    `--provider` and `--model` are optional whitelists, both
    repeatable, AND-combine when used together
    (`--provider openai --model gpt-5-mini --model gpt-5-nano` =
    "of these two OpenAI models, which is cheapest"). Both apply
    before `--budget`, so the over-budget fallback returns the
    cheapest within the filter set rather than the cheapest priced
    model overall.

    `--json` returns the same Pydantic shape the MCP
    `recommend_provider` tool produces, so existing schemas /
    consumers work verbatim.
    """
    with get_session() as session:
        try:
            result = _recommend_core(
                session,
                task_description=task,
                expected_input_tokens=input_tokens,
                expected_output_tokens=output_tokens,
                budget_usd=budget_usd,
                providers=providers if providers else None,
                models=models if models else None,
                # CLI-specific flag names so the reasoning's "specify
                # ___ for a precise estimate" advice points at the
                # flags the user just used, not the MCP tool's
                # Python parameter names.
                tokens_flag_names=("--in", "--out"),
            )
        except ValueError as exc:
            # Empty pricing snapshot, or whitelist matched nothing.
            # Raise as a Typer abort with a clean exit code (1) rather
            # than a stack trace — the message already tells the user
            # what to do. The param_hint nudges them at the filter
            # flags when those are the likely culprit.
            hint = "--provider/--model" if (providers or models) else "--task"
            raise typer.BadParameter(str(exc), param_hint=hint) from exc

    if json_output:
        typer.echo(json.dumps(result.model_dump(), indent=2))
        return

    color_enabled = _resolve_color(color)
    typer.echo(
        format_recommend_result(result, color_enabled=color_enabled),
        color=color_enabled,
    )


@app.command()
def spend(
    period: Period = typer.Option(
        "week",
        "--period",
        "-p",
        help="Calendar period (UTC). today / week (Mon-now) / month / year.",
    ),
    group_by: GroupBy | None = typer.Option(
        None,
        "--group-by",
        "-g",
        help=(
            "Switch to query_spend rollup mode. provider / model / project / "
            "tag / day. Without this flag, prints the usage_summary headline."
        ),
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="Filter to one provider (requires --group-by).",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Filter to one model (requires --group-by).",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Filter to one project tag (requires --group-by).",
    ),
    include_failed: bool = typer.Option(
        False,
        "--include-failed",
        help=(
            "Fold partial-stream / failure rows into totals. Off by default "
            "so headline numbers aren't polluted by maybe-billed events."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help=(
            "Emit the MCP tool's JSON shape — `UsageSummaryResult` by default, "
            "`QuerySpendResult` with --group-by."
        ),
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
    """Show recorded spend over a calendar period.

    Default view mirrors the MCP `usage_summary` tool: headline total,
    top-3 providers, top-3 models, and the single largest call.
    `--group-by` switches to the MCP `query_spend` view — one block
    of rows for the chosen axis with bar / cost / calls / %. Filters
    (`--provider`, `--model`, `--project`) AND-combine and require
    `--group-by` because the headline view summarizes across
    everything.
    """
    filters_given = any(v is not None for v in (provider, model, project))
    if filters_given and group_by is None:
        raise typer.BadParameter(
            "--provider / --model / --project require --group-by; the "
            "headline view summarizes across all rows.",
            param_hint="--group-by",
        )

    now_ms = int(time.time() * 1000)
    start_ms, end_ms = period_window(period, now_ms)
    color_enabled = _resolve_color(color)

    with get_session() as session:
        if group_by is None:
            summary = summarize_usage(
                session,
                period=period,
                include_failed=include_failed,
                now_ms=now_ms,
            )
            if json_output:
                typer.echo(json.dumps(summary.model_dump(), indent=2))
                return
            typer.echo(
                format_usage_summary(
                    summary,
                    period=period,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    include_failed=include_failed,
                    color_enabled=color_enabled,
                    bar_width=bar_width,
                ),
                color=color_enabled,
            )
            return

        spend_filter = (
            SpendFilter(provider=provider, model=model, project=project) if filters_given else None
        )
        rollup = aggregate_spend(
            session,
            start_ms=start_ms,
            end_ms=end_ms,
            group_by=group_by,
            filter=spend_filter,
            include_failed=include_failed,
            now_ms=now_ms,
        )
        if json_output:
            typer.echo(json.dumps(rollup.model_dump(), indent=2))
            return
        typer.echo(
            format_spend_groups(
                rollup,
                period=period,
                group_by=group_by,
                start_ms=start_ms,
                end_ms=end_ms,
                include_failed=include_failed,
                color_enabled=color_enabled,
                bar_width=bar_width,
            ),
            color=color_enabled,
        )


@app.command()
def status(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the `StatusReport` Pydantic shape instead of the human view.",
    ),
    color: ColorMode = typer.Option(
        ColorMode.auto,
        "--color",
        help="auto = color on TTY only (respects NO_COLOR); always = force on; never = force off.",
    ),
    no_net: bool = typer.Option(
        False,
        "--no-net",
        help=(
            "Skip the capture-proxy TCP probe. The proxy `status` field "
            "reports `unknown` instead — useful when offline or on a "
            "flaky network so `status` doesn't hang on the probe."
        ),
    ),
) -> None:
    """Snapshot of the local install: DB, proxy, providers, pricing.

    Read-only. Never creates files — running `status` on a brand-new
    install before `proxy` or `mcp` has booted reports
    "database not initialized" rather than silently materializing
    `~/.llm-usage/usage.db`. Missing keys / not-running proxy are
    informational (yellow), not errors. Exit code is always 0
    unless a hard failure (e.g., the DB file is unreadable) fires.
    """
    settings = get_settings()
    report = collect_status(settings, check_proxy=not no_net)

    if json_output:
        typer.echo(json.dumps(report.model_dump(), indent=2))
        return

    color_enabled = _resolve_color(color)
    typer.echo(
        format_status(
            report,
            color_enabled=color_enabled,
            now_ms=int(time.time() * 1000),
        ),
        color=color_enabled,
    )


@app.command()
def providers(
    show_models: bool = typer.Option(
        False,
        "--models",
        "-m",
        help="Expand each provider with its priced model list underneath.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the `ProvidersReport` Pydantic shape instead of the human view.",
    ),
    color: ColorMode = typer.Option(
        ColorMode.auto,
        "--color",
        help="auto = color on TTY only (respects NO_COLOR); always = force on; never = force off.",
    ),
) -> None:
    """List configured providers with key state, wire-format, model count.

    Deeper than the `status` Providers block: adds `openai-compat`
    flag and the optional per-provider model list (`--models`). Read-
    only. Never creates files — running before `proxy` or `mcp` has
    booted shows every `KNOWN_PROVIDERS` row with zero models priced
    rather than silently materializing the database.

    For "what can I call right now," prefer the MCP `list_providers`
    tool — it only surfaces providers with priced models. This command
    answers "what's wired up and where," including providers whose
    pricing hasn't been seeded yet.
    """
    settings = get_settings()
    report = collect_providers(settings)

    if json_output:
        typer.echo(json.dumps(report.model_dump(), indent=2))
        return

    color_enabled = _resolve_color(color)
    typer.echo(
        format_providers(
            report,
            color_enabled=color_enabled,
            show_models=show_models,
        ),
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


__all__ = ["app", "compare", "main", "providers", "proxy", "proxy_main", "recommend"]
