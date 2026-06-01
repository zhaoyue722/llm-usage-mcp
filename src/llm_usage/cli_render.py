"""Terminal-rendering helpers for the `llm-usage` CLI.

Pure functions: given an MCP-tool result (and a few formatting
options), produce a string. No I/O, no color-detection side effects —
the caller decides whether color is enabled and the renderer trusts
that flag. This keeps the rendering covered by simple snapshot tests
that don't have to mock TTY detection.

Color is applied via `click.style` (vendored into typer, already a
dep). The `color_enabled` flag short-circuits styling to a no-op when
the caller wants plain output — for `--json`, for `--color never`,
for `NO_COLOR`, and for non-TTY stdout.

## `compare` output

    Provider  Model              bar              Cost     Pct%

- Provider is the proper-noun display name (`OpenAI`, not `openai`).
- Model is the snapshot's lowercase model name verbatim.
- bar is `_BAR_GLYPH` repeated, **log-scaled** so cheapest = 1 cell,
  most expensive = `bar_width` cells (default 14). The bar character
  intentionally has whitespace above it (`▅`, LOWER FIVE EIGHTHS BLOCK)
  so adjacent rows don't visually merge into a single block.
- Cost is right-aligned `$0.0123` (4 dp). Always shows 4 dp so the
  decimal points line up vertically across rows.
- Pct is right-aligned with comma thousands separator (`9,384%`).

Color treatment ("winner + ratio heat"):
- Cheapest row: bold green across the whole line.
- All other rows: `Pct%` column is heat-mapped — green ≤200%, yellow
  ≤500%, red >500%.
- Header line and divider rules: dim.

## `spend` output

Two shapes, depending on whether `--group-by` was passed.

**Default (`format_usage_summary`)**: a `usage_summary` view —
headline total + top-3 providers + top-3 models, each as bar/cost/%
rows, plus a "largest call" footer.

**Grouped (`format_spend_groups`)**: a `query_spend` view — one
block of rows for the chosen group axis with bar/cost/calls/%
columns.

Bars in both `spend` shapes are **linear**, not log: each row's bar
length is `pct / 100 * bar_width`, so the lengths read as
share-of-spend at face value. Linear is honest here because spend
ratios are typically 2-10x; log would compress a real 4:1 difference
between providers into something visually close.

Color treatment for `spend` (per-column identity, not heat):
- Top spender / top model: bold green across the whole row — the
  leader stripe is uniform so it pops against the multi-color
  non-leader rows below. Same convention as `compare`.
- Non-leader rows: name in default; bar in white; cost in yellow
  (money); calls in cyan (count); pct in magenta (share). Each
  column gets its own color so a reader can scan vertically by
  "tell me the cost column" without re-reading row labels.
- Section labels ("top providers:", "top models:", "largest call:"):
  bold cyan — structural navigation, not data.
- The headline total: bold (no color change).
- Header / divider / `note:` lines: dim.
- Deliberately *no* heat-mapping on `%` — a high share is what you
  *expect* from the leader, so painting it red would invert the
  natural reading from `compare`.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Final

import click

from llm_usage.core.models import (
    CompareProvidersResult,
    GroupBy,
    LargestCall,
    Period,
    QuerySpendResult,
    RankedEntry,
    SpendGroup,
    TopModel,
    TopProvider,
    UsageSummaryResult,
)

# Lower 5/8 block — chosen because it fills the bottom 5/8 of the cell,
# leaving the top 3/8 empty. Vertically-adjacent rows of this glyph
# don't merge into a single column the way `█` does, which keeps each
# row visually independent.
_BAR_GLYPH: Final[str] = "▅"

# Default bar track width. 14 cells fits comfortably in an 80-col
# terminal alongside the longest provider/model names + cost + pct.
_DEFAULT_BAR_WIDTH: Final[int] = 14

# Divider rule character + width. 60 chars matches the visual weight
# of the data rows without overrunning the typical terminal.
_DIVIDER: Final[str] = "─" * 60

# Heat thresholds for the `Pct%` column on non-winner rows. Green up
# to 2x cheapest, yellow up to 5x, red beyond. The 2x / 5x bands match
# the rough mental brackets a user would use ("similar price" /
# "noticeably more" / "way more").
_HEAT_GREEN_MAX: Final[float] = 2.0
_HEAT_YELLOW_MAX: Final[float] = 5.0

# Lowercase DB names → properly-capitalized brand names. Falls back to
# `.title()` for any provider not in this map — fine for adding new
# providers without touching the renderer, but loses CamelCase brands
# like OpenAI / DeepSeek until they're added explicitly.
_PROVIDER_DISPLAY: Final[dict[str, str]] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "qwen": "Qwen",
    "deepseek": "DeepSeek",
}


def format_compare_result(
    result: CompareProvidersResult,
    *,
    input_tokens: int,
    output_tokens: int,
    color_enabled: bool,
    bar_width: int = _DEFAULT_BAR_WIDTH,
) -> str:
    """Render `compare`'s result for terminal display. Returns one string.

    Empty `result.ranked` (no priced models in the DB) returns a single
    line explaining the empty state; callers don't need to special-case.

    `input_tokens` / `output_tokens` are passed through only for the
    header line ("projecting cost for 8K in · 2K out") — the actual
    cost numbers are already baked into `result.ranked` by
    `core/compare.py`.
    """
    if not result.ranked:
        return _style(
            "no priced models in pricing_snapshot — is the database bootstrapped?",
            color_enabled,
            dim=True,
        )

    provider_w = max(len(_provider_display(e.provider)) for e in result.ranked)
    model_w = max(len(e.model) for e in result.ranked)

    # Most expensive ratio bounds the bar's log scale. `result.ranked`
    # is cheapest-first, so the last row is the max.
    max_ratio = result.ranked[-1].relative_cost_pct / 100.0

    lines: list[str] = [
        _style(
            f"projecting cost for {_format_tokens(input_tokens)} in"
            f" · {_format_tokens(output_tokens)} out",
            color_enabled,
            dim=True,
        ),
        _style(_DIVIDER, color_enabled, dim=True),
    ]

    for i, entry in enumerate(result.ranked):
        lines.append(
            _format_row(
                entry,
                is_winner=(i == 0),
                provider_w=provider_w,
                model_w=model_w,
                bar_width=bar_width,
                max_ratio=max_ratio,
                color_enabled=color_enabled,
            )
        )

    lines.append(_style(_DIVIDER, color_enabled, dim=True))
    lines.append(_style("note: cache pricing not applied (use --cache)", color_enabled, dim=True))
    return "\n".join(lines)


def _format_row(
    entry: RankedEntry,
    *,
    is_winner: bool,
    provider_w: int,
    model_w: int,
    bar_width: int,
    max_ratio: float,
    color_enabled: bool,
) -> str:
    """One data row. The winner row is bold-green; others heat the Pct% column."""
    provider = _provider_display(entry.provider).ljust(provider_w)
    model = entry.model.ljust(model_w)

    ratio = entry.relative_cost_pct / 100.0
    bar_cells = _bar_cells(ratio, max_ratio, bar_width)
    bar = (_BAR_GLYPH * bar_cells).ljust(bar_width)

    cost = _format_cost(entry.cost_usd)
    # Pre-pad the pct field to its target column width *before* styling.
    # `click.style` wraps the text in ANSI escape bytes, which Python's
    # `f"{x:>7}"` would count as part of the string length and skip
    # padding entirely — leaving styled rows visually misaligned against
    # unstyled ones. Pre-pad → style preserves the column.
    pct = f"{_format_pct(entry.relative_cost_pct):>7}"

    # Compose the line as plain text first so column widths are stable
    # regardless of color escapes.
    raw = f"{provider}  {model}  {bar}  {cost:>8}  {pct}"

    if not color_enabled:
        return raw

    if is_winner:
        return click.style(raw, fg="green", bold=True)

    # Non-winner rows: heat the (already-padded) Pct% column only.
    styled_pct = _style_pct(pct, ratio)
    return f"{provider}  {model}  {bar}  {cost:>8}  {styled_pct}"


def _bar_cells(ratio: float, max_ratio: float, width: int) -> int:
    """Log-scaled bar length. Cheapest (ratio=1) → 1 cell, max → `width` cells.

    Linear scaling collapses the bottom of the range when the
    expensive end is two orders of magnitude above the cheapest
    (a real case — claude-opus is ~90x qwen-flash). Log makes the
    bar a function of *order-of-magnitude* spend, which is more
    legible at a glance: each doubling of ratio adds roughly the
    same number of cells.

    Edge cases:
    - `max_ratio <= 1` → every row is the cheapest, every bar is 1 cell.
    - `ratio <= 1` (floating-point rounding) → clamped to 1 cell.
    """
    if max_ratio <= 1.0 or ratio <= 1.0:
        return 1
    cells = 1 + (width - 1) * math.log(ratio) / math.log(max_ratio)
    return max(1, min(width, round(cells)))


def _format_cost(cost_usd: float) -> str:
    """`$0.0123` — 4 decimal places, always. Aligns decimal points across rows."""
    return f"${cost_usd:.4f}"


def _format_pct(pct: float) -> str:
    """`9,384%` — comma thousands separator, no decimals (the math already
    rounded to 2dp but trailing `.00` would just add noise)."""
    return f"{round(pct):,}%"


def _format_tokens(n: int) -> str:
    """`8K` / `1.5K` / `750` — terse rendering for the header line.

    Sub-1000 stays in raw integer form. 1K-1M uses `K` with one
    decimal only when needed. The header is decorative; absolute
    precision belongs in the cost column.
    """
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        thousands = n / 1000
        if thousands == int(thousands):
            return f"{int(thousands)}K"
        return f"{thousands:.1f}K"
    millions = n / 1_000_000
    if millions == int(millions):
        return f"{int(millions)}M"
    return f"{millions:.1f}M"


def _provider_display(name: str) -> str:
    """Lowercase DB name → branded display. Falls back to `.title()`."""
    return _PROVIDER_DISPLAY.get(name, name.title())


def _style_pct(pct_text: str, ratio: float) -> str:
    """Color the Pct% column on a green/yellow/red heat scale."""
    if ratio <= _HEAT_GREEN_MAX:
        return click.style(pct_text, fg="green")
    if ratio <= _HEAT_YELLOW_MAX:
        return click.style(pct_text, fg="yellow")
    return click.style(pct_text, fg="red")


def _style(text: str, color_enabled: bool, *, dim: bool = False, **kwargs: object) -> str:
    """`click.style` if color is on, else verbatim text. Single chokepoint."""
    if not color_enabled:
        return text
    return click.style(text, dim=dim, **kwargs)  # type: ignore[arg-type]


# --- spend renderers -------------------------------------------------------

# How many top-N rows usage_summary returns per axis (providers, models). The
# MCP tool caps at 3 (see `core.spend._TOP_N`); the renderer uses the same
# constant for column-width sizing.
_SUMMARY_TOP_N: Final[int] = 3


def format_usage_summary(
    result: UsageSummaryResult,
    *,
    period: Period,
    start_ms: int,
    end_ms: int,
    include_failed: bool,
    color_enabled: bool,
    bar_width: int = _DEFAULT_BAR_WIDTH,
) -> str:
    """Render `usage_summary` for the `spend` CLI (no `--group-by`).

    Layout: period header → headline total → top providers block →
    top models block → largest call → footnote. Sections with empty
    data (top_providers=[], top_models=[], largest_call=None) are
    skipped so a fresh DB with one call doesn't print awkward empty
    headers.

    `start_ms`/`end_ms` are computed by the CLI via
    `core.spend.period_window` and passed in — the renderer stays
    pure (no time/zone calls of its own).
    """
    header = _format_period_header("spend", period, start_ms, end_ms)
    if result.call_count == 0:
        return "\n".join(
            [
                _style(header, color_enabled, dim=True),
                _empty_window_message(period, color_enabled),
            ]
        )

    lines: list[str] = [
        _style(header, color_enabled, dim=True),
        _style(_DIVIDER, color_enabled, dim=True),
        _format_summary_total(result, color_enabled),
        _style(_DIVIDER, color_enabled, dim=True),
    ]

    # Compute one key-column width across both blocks so the bars,
    # costs, and pct columns line up vertically when the eye sweeps
    # from `top providers:` down to `top models:`. Provider names
    # ("Anthropic" = 9) are usually shorter than model names
    # ("claude-sonnet-4-6" = 17); without a shared width the bars
    # would start at different columns.
    key_w = _shared_key_width(result.top_providers, result.top_models)

    if result.top_providers:
        lines.append(_section_label("top providers:", color_enabled))
        lines.extend(_render_top_providers(result.top_providers, key_w, bar_width, color_enabled))
        lines.append("")

    if result.top_models:
        lines.append(_section_label("top models:", color_enabled))
        lines.extend(_render_top_models(result.top_models, key_w, bar_width, color_enabled))
        lines.append("")

    if result.largest_call is not None:
        lines.append(_section_label("largest call:", color_enabled))
        lines.append(_render_largest_call(result.largest_call))

    # Drop the trailing blank line a top-block left behind so the
    # bottom divider butts up against the last content row.
    while lines and lines[-1] == "":
        lines.pop()

    lines.append(_style(_DIVIDER, color_enabled, dim=True))
    lines.append(_failure_footnote(include_failed, color_enabled))
    return "\n".join(lines)


def format_spend_groups(
    result: QuerySpendResult,
    *,
    period: Period,
    group_by: GroupBy,
    start_ms: int,
    end_ms: int,
    include_failed: bool,
    color_enabled: bool,
    bar_width: int = _DEFAULT_BAR_WIDTH,
) -> str:
    """Render a `query_spend` rollup for the `spend --group-by …` CLI.

    Layout: period header (with `, by <axis>` suffix) → rows
    (key, bar, cost, calls, %) → footnote. Empty windows print the
    same empty-state line as the summary view.

    Bars are linear (`cells = pct / 100 * bar_width`). The top row is
    bold green; non-leader rows have bars in dim cyan and the rest in
    default style.
    """
    header = _format_period_header("spend", period, start_ms, end_ms) + f", by {group_by}"
    if not result.groups:
        return "\n".join(
            [
                _style(header, color_enabled, dim=True),
                _empty_window_message(period, color_enabled),
            ]
        )

    total = result.total_cost_usd
    key_w = max(len(_display_group_key(g.key, group_by)) for g in result.groups)
    calls_w = max(len(_format_calls(g.calls)) for g in result.groups)

    lines: list[str] = [
        _style(header, color_enabled, dim=True),
        _style(_DIVIDER, color_enabled, dim=True),
    ]

    for i, group in enumerate(result.groups):
        lines.append(
            _format_spend_row(
                group,
                group_by=group_by,
                is_leader=(i == 0),
                total_cost=total,
                key_w=key_w,
                calls_w=calls_w,
                bar_width=bar_width,
                color_enabled=color_enabled,
            )
        )

    lines.append(_style(_DIVIDER, color_enabled, dim=True))
    lines.append(_failure_footnote(include_failed, color_enabled))
    return "\n".join(lines)


# --- spend: shared helpers ------------------------------------------------


def _format_period_header(prefix: str, period: Period, start_ms: int, end_ms: int) -> str:
    """`spend this week (2026-05-26 → 2026-06-01, UTC)` and siblings.

    `today` collapses the date range to a single date since
    `start_ms == end_ms` is one calendar day. Other periods show
    `start → end`. Always anchored UTC so the line is unambiguous.
    """
    label = _PERIOD_LABELS[period]
    start_d = _ms_to_date(start_ms)
    end_d = _ms_to_date(end_ms)
    if period == "today":
        return f"{prefix} {label} ({start_d}, UTC)"
    return f"{prefix} {label} ({start_d} → {end_d}, UTC)"


_PERIOD_LABELS: Final[dict[Period, str]] = {
    "today": "today",
    "week": "this week",
    "month": "this month",
    "year": "this year",
}


def _ms_to_date(ms: int) -> str:
    """`YYYY-MM-DD` from a UTC ms-epoch timestamp."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def _ms_to_datetime(ms: int) -> str:
    """`YYYY-MM-DD HH:MM UTC` — used for `largest_call`'s timestamp."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _section_label(label: str, color_enabled: bool) -> str:
    return _style(label, color_enabled, fg="cyan", bold=True)


def _empty_window_message(period: Period, color_enabled: bool) -> str:
    """Returned when the result has zero events / groups.

    Suggests widening the lookback when the user is on `today` or
    `week` — most likely the cause when a brand-new install shows zero.
    """
    suggestion = ""
    if period == "today":
        suggestion = " (try --period week for a wider window)"
    elif period == "week":
        suggestion = " (try --period month for a wider window)"
    return _style(
        f"no calls recorded in this period{suggestion}",
        color_enabled,
        dim=True,
    )


def _failure_footnote(include_failed: bool, color_enabled: bool) -> str:
    """Tell the reader which rows are counted.

    Two-state message because the user's most likely follow-up
    question on an "off by a bit" total is whether failed calls are
    in or out. Spell it out, even when the answer is "yes they're
    folded in."
    """
    if include_failed:
        text = "note: failed/partial-stream rows included (--include-failed)"
    else:
        text = "note: failed/partial-stream rows excluded (--include-failed to fold in)"
    return _style(text, color_enabled, dim=True)


# --- spend: summary helpers ----------------------------------------------


def _format_summary_total(result: UsageSummaryResult, color_enabled: bool) -> str:
    """`total: $0.1234  across 47 calls` — the headline line.

    Rendered bold (no fg color change) so it pops without competing
    with the leader-row green further down.
    """
    cost = _format_cost(result.total_cost_usd)
    return _style(
        f"total: {cost}  across {_format_calls(result.call_count)}",
        color_enabled,
        bold=True,
    )


def _format_calls(n: int) -> str:
    """`1 call` / `2 calls` / `1,234 calls` — pluralization + thousands sep.

    Single chokepoint so the `--group-by` rollup and the summary
    headline agree on the wording — saw `1 calls` slip into the
    first live run because the rollup hand-rolled the format string.
    """
    return f"{n:,} call" if n == 1 else f"{n:,} calls"


def _shared_key_width(providers: list[TopProvider], models: list[TopModel]) -> int:
    """Largest key length across both top-N blocks.

    Returning 0 for an empty pair would underpad anything, but in
    practice the caller only invokes `_render_top_*` when the
    corresponding list is non-empty, and the unused block doesn't
    contribute. Width 1 floor keeps the math safe if both lists
    happen to be empty (the format_usage_summary caller drops the
    blocks entirely in that case, so this is belt-and-braces).
    """
    candidates: list[int] = []
    if providers:
        candidates.append(max(len(_provider_display(r.provider)) for r in providers))
    if models:
        candidates.append(max(len(r.model) for r in models))
    return max(candidates) if candidates else 1


def _render_top_providers(
    rows: list[TopProvider], key_w: int, bar_width: int, color_enabled: bool
) -> list[str]:
    out: list[str] = []
    for i, row in enumerate(rows):
        out.append(
            _format_top_row(
                key=_provider_display(row.provider),
                key_w=key_w,
                cost_usd=row.cost_usd,
                pct=row.pct,
                is_leader=(i == 0),
                bar_width=bar_width,
                color_enabled=color_enabled,
            )
        )
    return out


def _render_top_models(
    rows: list[TopModel], key_w: int, bar_width: int, color_enabled: bool
) -> list[str]:
    out: list[str] = []
    for i, row in enumerate(rows):
        out.append(
            _format_top_row(
                key=row.model,
                key_w=key_w,
                cost_usd=row.cost_usd,
                pct=row.pct,
                is_leader=(i == 0),
                bar_width=bar_width,
                color_enabled=color_enabled,
            )
        )
    return out


def _format_top_row(
    *,
    key: str,
    key_w: int,
    cost_usd: float,
    pct: float,
    is_leader: bool,
    bar_width: int,
    color_enabled: bool,
) -> str:
    """One row inside a top-N block — `  Key  bar  $cost  pct%`.

    Two-space indent matches the section-label convention (the label
    is flush-left; rows hang off it). Leader = whole row bold green;
    non-leader = name default, bar white, cost yellow, pct magenta.
    Per-field padding happens *before* styling so the ANSI escape
    bytes don't inflate `len()` and break alignment (the bug we hit
    on `compare`'s first live run).
    """
    key_str = key.ljust(key_w)
    bar = _linear_bar(pct, bar_width)
    cost = f"{_format_cost(cost_usd):>8}"
    pct_str = f"{pct:>6.1f}%"

    if is_leader and color_enabled:
        return click.style(
            f"  {key_str}  {bar}  {cost}  {pct_str}",
            fg="green",
            bold=True,
        )

    bar_styled = _style(bar, color_enabled, fg="white")
    cost_styled = _style(cost, color_enabled, fg="yellow")
    pct_styled = _style(pct_str, color_enabled, fg="magenta")
    return f"  {key_str}  {bar_styled}  {cost_styled}  {pct_styled}"


def _render_largest_call(row: LargestCall) -> str:
    """`  model  $cost  (2026-05-30 14:23 UTC)` — two-space indent like top rows."""
    return f"  {row.model}  {_format_cost(row.cost_usd)}  ({_ms_to_datetime(row.timestamp)})"


# --- spend: --group-by helpers --------------------------------------------


def _display_group_key(key: str, group_by: GroupBy) -> str:
    """Provider keys get brand capitalization; everything else is raw."""
    if group_by == "provider":
        return _provider_display(key)
    return key


def _format_spend_row(
    group: SpendGroup,
    *,
    group_by: GroupBy,
    is_leader: bool,
    total_cost: float,
    key_w: int,
    calls_w: int,
    bar_width: int,
    color_enabled: bool,
) -> str:
    """One row of the grouped `spend` view — `Key  bar  $cost  N calls  pct%`.

    Bars scale linearly against the window total (not the max row),
    so the `%` column and the bar carry the same signal — a row at
    50% of total spend gets a bar half-filled. Bar lengths can sum
    to more or less than `bar_width` because non-tag axes partition
    the total, but tag groups don't (multi-tag rows double-count).
    """
    key_str = _display_group_key(group.key, group_by).ljust(key_w)
    pct = (group.cost_usd / total_cost * 100) if total_cost > 0 else 0.0
    bar = _linear_bar(pct, bar_width)
    cost = f"{_format_cost(group.cost_usd):>8}"
    calls = _format_calls(group.calls).ljust(calls_w)
    pct_str = f"{pct:>6.1f}%"

    if is_leader and color_enabled:
        return click.style(
            f"{key_str}  {bar}  {cost}  {calls}  {pct_str}",
            fg="green",
            bold=True,
        )

    bar_styled = _style(bar, color_enabled, fg="white")
    cost_styled = _style(cost, color_enabled, fg="yellow")
    calls_styled = _style(calls, color_enabled, fg="cyan")
    pct_styled = _style(pct_str, color_enabled, fg="magenta")
    return f"{key_str}  {bar_styled}  {cost_styled}  {calls_styled}  {pct_styled}"


def _linear_bar(pct: float, width: int) -> str:
    """Linear bar: `pct / 100 * width` cells, padded right to `width`.

    Linear is the right call for spend because the share-of-total
    is what the user is judging — a 50% row should look 5x bigger
    than a 10% row, not "two cells more" the way log would render
    it. Rows at <0.5% still get *some* visible bar (clamped to 1
    cell) so they don't disappear from the column.
    """
    cells = max(1 if pct > 0 else 0, round(width * pct / 100))
    cells = min(width, cells)
    return (_BAR_GLYPH * cells).ljust(width)


__all__ = [
    "format_compare_result",
    "format_spend_groups",
    "format_usage_summary",
]
