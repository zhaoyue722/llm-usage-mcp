"""Terminal-rendering helpers for the `llm-usage` CLI.

Pure functions: given a `CompareProvidersResult` (and a few formatting
options), produce a string. No I/O, no color-detection side effects —
the caller decides whether color is enabled and the renderer trusts
that flag. This keeps the rendering covered by simple snapshot tests
that don't have to mock TTY detection.

Color is applied via `click.style` (vendored into typer, already a
dep). The `color_enabled` flag short-circuits styling to a no-op when
the caller wants plain output — for `--json`, for `--color never`,
for `NO_COLOR`, and for non-TTY stdout.

Layout of the human-readable `compare` output:

    Provider  Model              bar              Cost     Pct%

- Provider is the proper-noun display name (`OpenAI`, not `openai`).
- Model is the snapshot's lowercase model name verbatim.
- bar is `_BAR_GLYPH` repeated, log-scaled so cheapest = 1 cell,
  most expensive = `bar_width` cells (default 14). The bar character
  intentionally has whitespace above it (`▅`, LOWER FIVE EIGHTHS BLOCK)
  so adjacent rows don't visually merge into a single block.
- Cost is right-aligned `$0.0123` (4 dp). Always shows 4 dp so the
  decimal points line up vertically across rows.
- Pct is right-aligned with comma thousands separator (`9,384%`).

Color treatment (the "winner + ratio heat" scheme):
- Cheapest row: bold green across the whole line.
- All other rows: `Pct%` column is heat-mapped — green ≤200%, yellow
  ≤500%, red >500%.
- Header line and divider rules: dim.
- `note:` footnote lines: dim.
"""

from __future__ import annotations

import math
from typing import Final

import click

from llm_usage.core.models import CompareProvidersResult, RankedEntry

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


__all__ = ["format_compare_result"]
