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
    ProviderRow,
    ProvidersReport,
    QuerySpendResult,
    RankedEntry,
    RecommendProviderResult,
    SpendGroup,
    StatusProvider,
    StatusReport,
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


# --- status renderer ------------------------------------------------------

# Stale threshold for the "refreshed N days ago" line on the Pricing
# section. Beyond 14 days the cron should have refreshed several times;
# something is off if we're past it (refresh action disabled, fork out
# of sync, etc.). Pre-spec'd decision: yellow at > 14 days, no red.
_PRICING_STALE_DAYS: Final[int] = 14

# Indent for the inner key/value lines under each `status` section
# label. Two spaces — matches the indent used by `top providers:` etc.
# under `spend`, so the visual register is the same across commands.
_STATUS_INDENT: Final[str] = "  "


def format_status(report: StatusReport, *, color_enabled: bool, now_ms: int) -> str:
    """Render `llm-usage status` for terminal display.

    Lines are flush-left section labels (bold cyan) with two-space-
    indented key/value rows underneath. Most values are plain; the
    "OK" states (key set, running, head) are green and the
    "attention" states (key missing, not running, schema behind
    head, stale pricing) are yellow. Deliberately no red — many
    "attention" states are intentional (one provider configured,
    proxy not yet started) and red would over-alarm.

    `now_ms` is the rendered "now," passed by the caller so the
    "N days ago" stamps in the Pricing section are deterministic in
    tests. Production callers pass `int(time.time() * 1000)`.
    """
    blocks: list[list[str]] = [
        [_style(f"llm-usage {report.version}", color_enabled, bold=True), ""],
        _status_database_block(report, color_enabled),
        _status_proxy_block(report, color_enabled),
        _status_providers_block(report, color_enabled),
        _status_pricing_block(report, color_enabled, now_ms),
    ]
    # Filter out empty blocks (DB-missing case skips the Database +
    # Pricing sections entirely) then join with one blank line between.
    nonempty = [b for b in blocks if b]
    return "\n".join("\n".join(b) for b in nonempty).rstrip()


def _status_database_block(report: StatusReport, color_enabled: bool) -> list[str]:
    """Database section. When `report.database is None`, render a single
    hint line instead — the DB file doesn't exist yet (no boot has
    happened) and `status` shouldn't pretend it does."""
    if report.database is None:
        return [
            _section_label("Database", color_enabled),
            _STATUS_INDENT
            + _style(
                "not initialized (run llm-usage proxy or llm-usage-mcp once to migrate)",
                color_enabled,
                fg="yellow",
            ),
            "",
        ]

    db = report.database
    rev_text = db.schema_revision or "missing"
    schema_value = (
        _style(f"head (rev {rev_text})", color_enabled, fg="green")
        if db.schema_at_head
        else _style(
            f"behind (current rev {rev_text}; next boot will migrate)",
            color_enabled,
            fg="yellow",
        )
    )

    if db.event_count == 0:
        events_value = _style("none recorded yet", color_enabled, fg="yellow")
    else:
        oldest = _ms_to_date(db.oldest_event_ms) if db.oldest_event_ms else "—"
        newest = _ms_to_date(db.newest_event_ms) if db.newest_event_ms else "—"
        events_value = f"{db.event_count:,} (oldest {oldest}, newest {newest}, UTC)"

    return [
        _section_label("Database", color_enabled),
        _kv_row("path", _shorten_home(db.path), color_enabled),
        _kv_row("size", _format_bytes(db.size_bytes), color_enabled),
        _kv_row("schema", schema_value, color_enabled, value_already_styled=True),
        _kv_row("events", events_value, color_enabled, value_already_styled=True),
        "",
    ]


def _status_proxy_block(report: StatusReport, color_enabled: bool) -> list[str]:
    """Capture proxy section. Adds a `start` hint when not running."""
    proxy = report.proxy
    bind_value = f"{proxy.host}:{proxy.port}"
    lines = [
        _section_label("Capture proxy", color_enabled),
        _kv_row("bind", bind_value, color_enabled),
    ]
    if proxy.reachable is None:
        status_value = _style("unknown (--no-net)", color_enabled, dim=True)
        lines.append(_kv_row("status", status_value, color_enabled, value_already_styled=True))
    elif proxy.reachable:
        status_value = _style("running", color_enabled, fg="green")
        lines.append(_kv_row("status", status_value, color_enabled, value_already_styled=True))
    else:
        status_value = _style("not running", color_enabled, fg="yellow")
        lines.append(_kv_row("status", status_value, color_enabled, value_already_styled=True))
        start_hint = _style("uv run llm-usage proxy", color_enabled, dim=True)
        lines.append(_kv_row("start", start_hint, color_enabled, value_already_styled=True))
    lines.append("")
    return lines


def _status_providers_block(report: StatusReport, color_enabled: bool) -> list[str]:
    """One row per provider — key status + base URL + model count.

    Column widths are computed across the full provider list so the
    base URLs line up regardless of whose key is set.
    """
    name_w = max(len(p.display_name) for p in report.providers)
    state_w = max(len("key missing"), len("key set"))  # the two possible state words
    lines: list[str] = [_section_label("Providers", color_enabled)]
    for p in report.providers:
        lines.append(
            _format_provider_row(p, name_w=name_w, state_w=state_w, color_enabled=color_enabled)
        )
    lines.append("")
    return lines


def _format_provider_row(
    provider: StatusProvider, *, name_w: int, state_w: int, color_enabled: bool
) -> str:
    """`  Name        state          base-url      N models priced` (one row)."""
    name = provider.display_name.ljust(name_w)
    if provider.key_set:
        state = _style("key set".ljust(state_w), color_enabled, fg="green")
    else:
        state = _style("key missing".ljust(state_w), color_enabled, fg="yellow")
    models_suffix = (
        f"{provider.model_count} models priced" if provider.model_count != 1 else "1 model priced"
    )
    models_styled = _style(models_suffix, color_enabled, dim=True)
    return f"{_STATUS_INDENT}{name}  {state}  {provider.base_url}  {models_styled}"


def _status_pricing_block(report: StatusReport, color_enabled: bool, now_ms: int) -> list[str]:
    """Pricing section. Skipped when the DB doesn't exist (mirrors Database)."""
    if report.pricing is None:
        return []  # already covered by the Database "not initialized" hint.

    p = report.pricing
    if p.model_count == 0:
        catalog_value = _style("empty (run bootstrap to materialize)", color_enabled, fg="yellow")
        return [
            _section_label("Pricing", color_enabled),
            _kv_row("catalog", catalog_value, color_enabled, value_already_styled=True),
            "",
        ]

    catalog_value = f"{p.model_count} models across {p.provider_count} providers"
    refreshed_value = _format_refreshed(p.newest_fetched_at_ms, now_ms, color_enabled)
    return [
        _section_label("Pricing", color_enabled),
        _kv_row("catalog", catalog_value, color_enabled),
        _kv_row("refreshed", refreshed_value, color_enabled, value_already_styled=True),
        "",
    ]


def _format_refreshed(fetched_at_ms: int | None, now_ms: int, color_enabled: bool) -> str:
    """`2026-05-31 (1 day ago)` — yellow when older than `_PRICING_STALE_DAYS`."""
    if fetched_at_ms is None:
        return _style("never", color_enabled, fg="yellow")

    date_str = _ms_to_date(fetched_at_ms)
    age_ms = max(0, now_ms - fetched_at_ms)
    age_days = age_ms // (24 * 3600 * 1000)
    if age_days == 0:
        age_phrase = "today"
    elif age_days == 1:
        age_phrase = "1 day ago"
    else:
        age_phrase = f"{age_days} days ago"

    rendered = f"{date_str} ({age_phrase})"
    if age_days > _PRICING_STALE_DAYS:
        return _style(rendered, color_enabled, fg="yellow")
    return rendered


# --- status: small helpers ----------------------------------------------


def _kv_row(
    label: str,
    value: str,
    color_enabled: bool,
    *,
    value_already_styled: bool = False,
) -> str:
    """`  label    value` — label dimmed, value already styled by caller (or plain).

    Label width is fixed at 10 because every status label in this
    file is ≤9 chars (`refreshed`, `events`, `schema`, …). Hard-coded
    so the renderer doesn't have to know about every label up front.
    """
    label_styled = _style(label.ljust(10), color_enabled, dim=True)
    return f"{_STATUS_INDENT}{label_styled}  {value}"


def _format_bytes(n: int) -> str:
    """`1.2 MB` / `512 KB` / `48 bytes` — single-decimal human size."""
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def _shorten_home(path: str) -> str:
    """Replace the user's home dir prefix with `~` for readability."""
    from pathlib import Path

    try:
        home = str(Path.home())
    except RuntimeError:
        return path
    if path.startswith(home + "/"):
        return "~" + path[len(home) :]
    if path == home:
        return "~"
    return path


# --- providers renderer ---------------------------------------------------
#
# Layout: section header → one row per provider (`Name  key-state
# openai-compat: yes/no  N models  base-url`). With `show_models=True`,
# each provider row is followed by the priced model list, two-space-
# further indented underneath the row. The same two-space convention
# the spend and status renderers use, so the visual register matches.
#
# Color treatment (intentionally light — this is a configuration view,
# not a ranking):
#   - `key set`     → green
#   - `key missing` → yellow
#   - `openai-compat: yes/no` → dim (informational, not actionable)
#   - model-count suffix → dim
#   - base URL → default (the value the user typed / inherited)
#   - section label → bold cyan, matching the other commands


def format_providers(
    report: ProvidersReport,
    *,
    color_enabled: bool,
    show_models: bool = False,
) -> str:
    """Render `llm-usage providers` for terminal display. Returns one string.

    `show_models=True` (the `--models` CLI flag) expands each row with
    the provider's priced model list underneath. A provider with no
    seeded pricing prints a single dim "no models priced yet" hint
    rather than a silent empty block, so the user can tell apart
    "not configured" from "configured but the catalog hasn't been
    materialized."
    """
    if not report.providers:
        # Defensive: `collect_providers` always returns one row per
        # `KNOWN_PROVIDERS`, so this branch is only reached if someone
        # constructs an empty `ProvidersReport` by hand. Keep the
        # message terse rather than the multi-line "is the database
        # bootstrapped?" hint — the situation is different.
        return _style("no known providers", color_enabled, dim=True)

    name_w = max(len(p.display_name) for p in report.providers)
    state_w = max(len("key missing"), len("key set"))
    compat_w = max(len("openai-compat: yes"), len("openai-compat: no"))
    # Width of the "N models priced" / "1 model priced" suffix. Padded
    # to the widest entry so the base-URL column lines up vertically
    # across rows regardless of whether one provider has 8 priced
    # models and another has 120.
    models_w = max(len(_format_models_suffix(len(p.models))) for p in report.providers)

    count = len(report.providers)
    header = f"Providers · {count} known"

    lines: list[str] = [_section_label(header, color_enabled)]

    for provider in report.providers:
        lines.append(
            _format_providers_row(
                provider,
                name_w=name_w,
                state_w=state_w,
                compat_w=compat_w,
                models_w=models_w,
                color_enabled=color_enabled,
            )
        )
        if show_models:
            lines.extend(_format_provider_model_lines(provider, color_enabled))

    return "\n".join(lines)


def _format_models_suffix(count: int) -> str:
    """`N models priced` / `1 model priced` — pluralization in one place.

    Shared between width calculation (where the unstyled length is
    what matters for padding) and the row renderer.
    """
    return f"{count} models priced" if count != 1 else "1 model priced"


def _format_providers_row(
    provider: ProviderRow,
    *,
    name_w: int,
    state_w: int,
    compat_w: int,
    models_w: int,
    color_enabled: bool,
) -> str:
    """`  Name  state  openai-compat: yes  N models  https://…`.

    Per-field padding happens *before* styling — same lesson as
    `compare`/`spend` — so ANSI escapes don't inflate `len()` and
    knock subsequent columns out of alignment.
    """
    name = provider.display_name.ljust(name_w)

    if provider.key_set:
        state = _style("key set".ljust(state_w), color_enabled, fg="green")
    else:
        state = _style("key missing".ljust(state_w), color_enabled, fg="yellow")

    compat_text = f"openai-compat: {'yes' if provider.openai_compatible else 'no'}"
    compat = _style(compat_text.ljust(compat_w), color_enabled, dim=True)

    models_suffix = _format_models_suffix(len(provider.models))
    models_styled = _style(models_suffix.ljust(models_w), color_enabled, dim=True)

    return f"{_STATUS_INDENT}{name}  {state}  {compat}  {models_styled}  {provider.base_url}"


def _format_provider_model_lines(provider: ProviderRow, color_enabled: bool) -> list[str]:
    """The expanded model list under one provider row (when `--models` is on).

    Returns a single hint line for providers with no priced models,
    otherwise one indented model name per line. The four-space indent
    visually "hangs" the model list off the provider row's two-space
    indent.
    """
    if not provider.models:
        return [
            "    " + _style("no models priced yet", color_enabled, dim=True),
        ]
    return ["    " + model for model in provider.models]


# --- recommend renderer ---------------------------------------------------
#
# Two-block layout:
#
#     Recommendation
#       Qwen / qwen-flash       $0.0042
#
#     Reasoning
#       For task 'summarize a transcript': recommending qwen/qwen-flash —
#       the cheapest projected cost among 159 priced model(s). Estimated
#       $0.0042 for 1,000 input / 1,000 output tokens. v1 ranks by cost
#       only; task_description is echoed for context but does not drive
#       selection.
#
# Color treatment:
#   - Section labels: bold cyan (same as `status` / `spend`).
#   - The chosen row: bold green — the leader-row convention.
#   - Reasoning paragraph: dim — informational, structured by template
#     not requiring the user's full attention.
#
# Reasoning is word-wrapped at `_RECOMMEND_REASONING_WIDTH`. The MCP
# tool returns it as one long string; the CLI wraps for readability.


# Width the reasoning paragraph is wrapped at. 78 leaves a 2-col
# margin under an 80-col terminal — wide enough to keep most sentences
# on one line, narrow enough that the dim paragraph doesn't run edge-
# to-edge with the rest of the output.
_RECOMMEND_REASONING_WIDTH: Final[int] = 78


def format_recommend_result(
    result: RecommendProviderResult,
    *,
    color_enabled: bool,
) -> str:
    """Render `recommend`'s result for terminal display. Returns one string.

    Layout: `Recommendation` block (chosen provider / model + cost) →
    blank line → `Reasoning` block (word-wrapped paragraph). The
    chosen row is green to match the leader-row convention from
    `compare` and `spend`; the reasoning is dim because it's
    informational and the user's eye should land on the chosen row
    first.
    """
    provider = _provider_display(result.provider)
    chosen_line = f"  {provider} / {result.model}  {_format_cost(result.estimated_cost_usd)}"
    styled_chosen = (
        click.style(chosen_line, fg="green", bold=True) if color_enabled else chosen_line
    )

    reasoning_lines = [
        "  " + line for line in _wrap_paragraph(result.reasoning, _RECOMMEND_REASONING_WIDTH - 2)
    ]
    styled_reasoning = [_style(line, color_enabled, dim=True) for line in reasoning_lines]

    blocks: list[str] = [
        _section_label("Recommendation", color_enabled),
        styled_chosen,
        "",
        _section_label("Reasoning", color_enabled),
        *styled_reasoning,
    ]
    return "\n".join(blocks)


def _wrap_paragraph(text: str, width: int) -> list[str]:
    """Greedy word-wrap a paragraph at `width` columns.

    Uses `textwrap.wrap` rather than rolling our own — handles the
    long-token edge case (a URL or model name longer than `width`)
    by leaving the long token alone on its own line rather than
    breaking it mid-character. `replace_whitespace=False` would let
    the reasoning's em-dash and apostrophes survive intact, but the
    default behavior is fine for our single-line input.
    """
    import textwrap

    return textwrap.wrap(
        text,
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [""]


__all__ = [
    "format_compare_result",
    "format_providers",
    "format_recommend_result",
    "format_spend_groups",
    "format_status",
    "format_usage_summary",
]
