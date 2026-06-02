"""Unit tests for `cli_render.format_compare_result`.

Pure-function tests with hand-built `CompareProvidersResult`s — no DB,
no Typer involvement. The renderer is the source of every visual
detail (column widths, bar glyph, color escape placement, footnote
text), so a regression here is what would change the user's terminal
experience even if the projection math is right.
"""

from __future__ import annotations

import re

from llm_usage.cli_render import (
    format_compare_result,
    format_pricing_catalog,
    format_providers,
    format_recommend_result,
    format_spend_groups,
    format_status,
    format_usage_summary,
)
from llm_usage.core.models import (
    Alternative,
    CompareProvidersResult,
    LargestCall,
    PricingEntry,
    ProviderRow,
    ProvidersReport,
    QuerySpendResult,
    RankedEntry,
    RecommendProviderResult,
    SpendGroup,
    StatusDatabase,
    StatusPricing,
    StatusProvider,
    StatusProxy,
    StatusReport,
    TopModel,
    TopProvider,
    UsageSummaryResult,
)

# Fixed timestamps so the period-header tests are deterministic.
#   start = 2026-05-26 00:00 UTC (Mon)
#   end   = 2026-06-01 12:00 UTC (the rendered "now")
_WEEK_START_MS = 1779753600000  # 2026-05-26T00:00:00Z
_WEEK_END_MS = 1780315200000  # 2026-06-01T12:00:00Z

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _ranked(*entries: tuple[str, str, float, float]) -> CompareProvidersResult:
    """Helper: build a `CompareProvidersResult` from (provider, model, cost, pct) tuples."""
    return CompareProvidersResult(
        ranked=[
            RankedEntry(provider=p, model=m, cost_usd=c, relative_cost_pct=pct, notes=None)
            for p, m, c, pct in entries
        ]
    )


# --- empty / edge cases ----------------------------------------------------


def test_empty_result_renders_an_empty_state_message() -> None:
    result = CompareProvidersResult(ranked=[])
    out = format_compare_result(result, input_tokens=100, output_tokens=100, color_enabled=False)
    assert "no priced models" in out
    # No divider, no footnote — empty state is a single line.
    assert "\n" not in out
    assert "─" not in out


# --- non-empty structural assertions ---------------------------------------


def test_non_empty_result_has_header_divider_rows_footnote() -> None:
    result = _ranked(
        ("deepseek", "cheap-1", 0.001, 100.0),
        ("openai", "mid-1", 0.002, 200.0),
    )
    out = format_compare_result(result, input_tokens=8000, output_tokens=2000, color_enabled=False)
    lines = out.split("\n")
    # header + divider + 2 data rows + divider + footnote
    assert len(lines) == 6
    assert "projecting cost for" in lines[0]
    assert lines[1].startswith("─")
    assert "cheap-1" in lines[2]
    assert "mid-1" in lines[3]
    assert lines[4].startswith("─")
    assert "cache pricing not applied" in lines[5]


def test_header_includes_terse_token_counts() -> None:
    result = _ranked(("openai", "x", 0.01, 100.0))
    # 8K, 2K should appear in the header.
    out = format_compare_result(result, input_tokens=8000, output_tokens=2000, color_enabled=False)
    assert "8K in" in out
    assert "2K out" in out


def test_header_renders_sub_1k_as_raw_integer() -> None:
    result = _ranked(("openai", "x", 0.01, 100.0))
    out = format_compare_result(result, input_tokens=500, output_tokens=100, color_enabled=False)
    assert "500 in" in out
    assert "100 out" in out


def test_header_renders_decimal_thousands() -> None:
    result = _ranked(("openai", "x", 0.01, 100.0))
    out = format_compare_result(
        result, input_tokens=1500, output_tokens=128_000, color_enabled=False
    )
    assert "1.5K in" in out
    assert "128K out" in out


def test_header_renders_millions() -> None:
    result = _ranked(("openai", "x", 0.01, 100.0))
    out = format_compare_result(
        result, input_tokens=1_000_000, output_tokens=2_500_000, color_enabled=False
    )
    assert "1M in" in out
    assert "2.5M out" in out


# --- provider display name mapping -----------------------------------------


def test_known_providers_render_with_branded_capitalization() -> None:
    result = _ranked(
        ("anthropic", "a", 0.001, 100.0),
        ("openai", "b", 0.002, 200.0),
        ("deepseek", "c", 0.003, 300.0),
        ("qwen", "d", 0.004, 400.0),
    )
    out = format_compare_result(result, input_tokens=1, output_tokens=1, color_enabled=False)
    # Brand-correct casing, not `.title()` collapse.
    assert "Anthropic" in out
    assert "OpenAI" in out
    assert "DeepSeek" in out
    assert "Qwen" in out
    # Never the lowercase DB form.
    assert "openai " not in out  # space-suffixed to avoid matching "openai" inside something
    assert "deepseek " not in out


def test_unknown_provider_falls_back_to_title_case() -> None:
    result = _ranked(("moonshot", "kimi-k2", 0.001, 100.0))
    out = format_compare_result(result, input_tokens=1, output_tokens=1, color_enabled=False)
    assert "Moonshot" in out


# --- bar scaling -----------------------------------------------------------


def test_cheapest_row_has_one_cell_bar() -> None:
    result = _ranked(
        ("deepseek", "cheap-1", 0.001, 100.0),
        ("openai", "expensive-1", 0.100, 10_000.0),
    )
    out = format_compare_result(
        result, input_tokens=1, output_tokens=1, color_enabled=False, bar_width=10
    )
    cheap_line = next(line for line in out.split("\n") if "cheap-1" in line)
    # Exactly one bar glyph for the cheapest, surrounded by spaces.
    assert cheap_line.count("▅") == 1


def test_most_expensive_row_fills_the_bar_track() -> None:
    result = _ranked(
        ("deepseek", "cheap-1", 0.001, 100.0),
        ("openai", "expensive-1", 0.100, 10_000.0),
    )
    out = format_compare_result(
        result, input_tokens=1, output_tokens=1, color_enabled=False, bar_width=10
    )
    expensive_line = next(line for line in out.split("\n") if "expensive-1" in line)
    assert expensive_line.count("▅") == 10


def test_bar_scaling_is_log_not_linear() -> None:
    # Three rows at 100% / 1000% / 10000% — log-scaled, the midpoint
    # gets ~half the bar width (5 of 10 cells). Linear would put it at
    # 1 cell (1/100 of the way) which would collapse against the cheapest.
    result = _ranked(
        ("deepseek", "cheap", 0.001, 100.0),
        ("openai", "mid", 0.010, 1000.0),
        ("anthropic", "expensive", 0.100, 10_000.0),
    )
    out = format_compare_result(
        result, input_tokens=1, output_tokens=1, color_enabled=False, bar_width=10
    )
    mid_line = next(line for line in out.split("\n") if "mid" in line)
    mid_bars = mid_line.count("▅")
    # Log scaling: 1 + 9 * log(10)/log(100) = 1 + 4.5 = 5.5 → 6 cells (banker's rounding).
    assert 4 <= mid_bars <= 6  # tolerate ±1 around the rounding boundary


# --- color: off / on -------------------------------------------------------


def test_color_disabled_emits_no_ansi_escapes() -> None:
    result = _ranked(
        ("deepseek", "cheap-1", 0.001, 100.0),
        ("openai", "mid-1", 0.002, 200.0),
        ("anthropic", "expensive-1", 0.010, 1000.0),
    )
    out = format_compare_result(result, input_tokens=1, output_tokens=1, color_enabled=False)
    assert _ANSI_RE.search(out) is None


def test_color_enabled_styles_winner_row_bold_green() -> None:
    result = _ranked(
        ("deepseek", "cheap-1", 0.001, 100.0),
        ("openai", "mid-1", 0.002, 200.0),
    )
    out = format_compare_result(result, input_tokens=1, output_tokens=1, color_enabled=True)
    winner_line = next(line for line in out.split("\n") if "cheap-1" in line)
    # Click emits `\x1b[1m\x1b[32m` for bold + green (order may vary).
    assert "\x1b[" in winner_line  # ANSI present
    assert "1" in _extract_ansi_codes(winner_line)  # bold
    assert "32" in _extract_ansi_codes(winner_line)  # green fg


def test_color_enabled_heat_maps_pct_column_on_non_winner_rows() -> None:
    # 100% (winner) / 150% (green ≤200%) / 400% (yellow ≤500%) / 800% (red).
    result = _ranked(
        ("deepseek", "cheap-1", 0.001, 100.0),
        ("openai", "low-mult", 0.0015, 150.0),
        ("openai", "mid-mult", 0.004, 400.0),
        ("anthropic", "high-mult", 0.008, 800.0),
    )
    out = format_compare_result(result, input_tokens=1, output_tokens=1, color_enabled=True)
    by_model = {row.split()[1]: row for row in out.split("\n") if "▅" in row}
    # Winner: whole-line green+bold (covers the pct too) — codes 1 and 32.
    winner_codes = _extract_ansi_codes(by_model["cheap-1"])
    assert "32" in winner_codes and "1" in winner_codes
    # 150% → green heat on pct only (no bold).
    low_codes = _extract_ansi_codes(by_model["low-mult"])
    assert "32" in low_codes and "1" not in low_codes
    # 400% → yellow heat.
    mid_codes = _extract_ansi_codes(by_model["mid-mult"])
    assert "33" in mid_codes  # yellow
    # 800% → red heat.
    high_codes = _extract_ansi_codes(by_model["high-mult"])
    assert "31" in high_codes  # red


def test_color_enabled_dims_header_divider_and_footnote() -> None:
    result = _ranked(("deepseek", "cheap-1", 0.001, 100.0))
    out = format_compare_result(result, input_tokens=1, output_tokens=1, color_enabled=True)
    lines = out.split("\n")
    # `dim` is SGR code 2.
    assert "2" in _extract_ansi_codes(lines[0])  # header
    assert "2" in _extract_ansi_codes(lines[1])  # top divider
    # data row (index 2) — not necessarily dim
    assert "2" in _extract_ansi_codes(lines[3])  # bottom divider
    assert "2" in _extract_ansi_codes(lines[4])  # footnote


# --- ×N variant column ---------------------------------------------------


def _ranked_with_variants(
    *entries: tuple[str, str, float, float, int],
) -> CompareProvidersResult:
    """Like `_ranked` but each entry carries an explicit `variant_count`.

    Tuple shape: (provider, model, cost_usd, relative_cost_pct, variant_count).
    """
    return CompareProvidersResult(
        ranked=[
            RankedEntry(
                provider=p,
                model=m,
                cost_usd=c,
                relative_cost_pct=pct,
                notes=None,
                variant_count=vc,
            )
            for p, m, c, pct, vc in entries
        ]
    )


def test_variant_column_hidden_when_no_row_has_collapsed_variants() -> None:
    """All rows at variant_count=1 → no `×N` column, no extra footer.
    Default-on dedup with nothing actually collapsed must look identical
    to the pre-dedup output for catalogs without alias-family clusters."""
    result = _ranked_with_variants(
        ("openai", "a", 0.001, 100.0, 1),
        ("openai", "b", 0.002, 200.0, 1),
    )
    out = format_compare_result(result, input_tokens=100, output_tokens=100, color_enabled=False)
    assert "×" not in out
    assert "collapsed catalog variants" not in out


def test_variant_column_renders_when_any_row_has_collapsed_variants() -> None:
    """A single row at variant_count>1 should trigger the `×N` column
    and the explanatory footer line."""
    result = _ranked_with_variants(
        ("qwen", "qwen-turbo", 0.0003, 100.0, 4),  # 4 catalog variants
        ("deepseek", "deepseek-coder", 0.0004, 168.0, 1),  # solo
    )
    out = format_compare_result(result, input_tokens=100, output_tokens=100, color_enabled=False)
    qwen_line = next(line for line in out.split("\n") if "qwen-turbo" in line)
    deepseek_line = next(line for line in out.split("\n") if "deepseek-coder" in line)
    assert "×4" in qwen_line
    # Solo rows must NOT carry a ×1 marker — only the column padding.
    assert "×" not in deepseek_line
    # Footer note explaining the convention.
    assert "×N indicates N collapsed catalog variants" in out


def test_variant_column_aligns_x_marker_across_rows() -> None:
    """When the count widths differ (×4 vs ×12), the right edge of the
    column should align so the `×` characters stack vertically."""
    result = _ranked_with_variants(
        ("openai", "a", 0.001, 100.0, 4),
        ("openai", "b", 0.002, 200.0, 12),
    )
    out = format_compare_result(result, input_tokens=100, output_tokens=100, color_enabled=False)
    # Data rows are the ones with both a bar glyph AND the variant marker;
    # the footer line carries `×N indicates...` but no bar.
    data_lines = [line for line in out.split("\n") if "▅" in line and "×" in line]
    # Both should end with the marker; compare from the right edge so
    # the column is right-aligned.
    assert len(data_lines) == 2
    assert data_lines[0].rstrip().endswith("×4")
    assert data_lines[1].rstrip().endswith("×12")
    # Both lines must have the same total length once stripped.
    plain_lines = [_ANSI_RE.sub("", line) for line in data_lines]
    assert len(plain_lines[0]) == len(plain_lines[1])


def test_variant_column_dim_when_color_enabled() -> None:
    """`×N` text should carry the dim attribute (SGR 2), so it's
    visually subordinate to the bar/cost/pct columns."""
    result = _ranked_with_variants(
        ("openai", "winner", 0.001, 100.0, 1),  # winner row (no dim needed)
        ("openai", "other", 0.002, 200.0, 3),  # non-winner with variants
    )
    out = format_compare_result(result, input_tokens=100, output_tokens=100, color_enabled=True)
    other_line = next(line for line in out.split("\n") if "other" in line)
    assert "2" in _extract_ansi_codes(other_line)  # dim somewhere on the line


# --- alignment: styled vs unstyled rows -----------------------------------


def test_pct_column_aligns_between_styled_and_unstyled_rows() -> None:
    """Regression: pre-padding the pct value before styling keeps the
    Pct column visually aligned. If we padded after styling, the ANSI
    escape bytes would inflate the string length and Python's `>7`
    format spec would add no padding — making styled rows visually
    shift left."""
    result = _ranked(
        ("deepseek", "cheap-1", 0.001, 100.0),  # winner, fully styled
        ("openai", "mid-1", 0.002, 150.0),  # styled pct only
    )
    out = format_compare_result(result, input_tokens=1, output_tokens=1, color_enabled=True)
    # Strip ANSI escapes; the resulting visual widths must match.
    plain_lines = [_ANSI_RE.sub("", line) for line in out.split("\n") if "▅" in line]
    # Both data rows must have the same length once styles are stripped.
    assert len(plain_lines) == 2
    assert len(plain_lines[0]) == len(plain_lines[1])


# --- helpers ---------------------------------------------------------------


def _extract_ansi_codes(text: str) -> set[str]:
    """Pull all SGR parameter codes out of a styled string.

    `\\x1b[1;32m` → {"1", "32"}; multiple escape sequences in one line
    flatten into one set. Useful for asserting "bold and green appear
    somewhere in this row" without depending on click's exact escape
    ordering.
    """
    codes: set[str] = set()
    for match in _ANSI_RE.finditer(text):
        params = match.group(0)[2:-1]  # strip `\x1b[` and `m`
        codes.update(p for p in params.split(";") if p)
    return codes


# === format_usage_summary ===============================================


def _summary(
    *,
    total_cost: float = 0.0,
    call_count: int = 0,
    top_providers: list[tuple[str, float, float]] | None = None,
    top_models: list[tuple[str, float, float]] | None = None,
    largest_call: tuple[str, str, float, int] | None = None,
) -> UsageSummaryResult:
    """Build a `UsageSummaryResult` from tuples for terse fixtures."""
    return UsageSummaryResult(
        period="week",
        total_cost_usd=total_cost,
        call_count=call_count,
        top_providers=[
            TopProvider(provider=p, cost_usd=c, pct=pct) for p, c, pct in (top_providers or [])
        ],
        top_models=[TopModel(model=m, cost_usd=c, pct=pct) for m, c, pct in (top_models or [])],
        largest_call=(
            LargestCall(
                id=largest_call[0],
                model=largest_call[1],
                cost_usd=largest_call[2],
                timestamp=largest_call[3],
            )
            if largest_call is not None
            else None
        ),
    )


def test_summary_empty_window_renders_only_header_and_empty_state() -> None:
    out = format_usage_summary(
        _summary(),
        period="week",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=False,
    )
    lines = out.split("\n")
    assert len(lines) == 2
    assert "spend this week" in lines[0]
    assert "no calls recorded" in lines[1]


def test_summary_empty_window_today_suggests_widening_to_week() -> None:
    out = format_usage_summary(
        _summary(),
        period="today",
        start_ms=_WEEK_END_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=False,
    )
    assert "try --period week" in out


def test_summary_empty_window_week_suggests_widening_to_month() -> None:
    out = format_usage_summary(
        _summary(),
        period="week",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=False,
    )
    assert "try --period month" in out


def test_summary_today_header_shows_single_date() -> None:
    out = format_usage_summary(
        _summary(),
        period="today",
        start_ms=_WEEK_END_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=False,
    )
    assert "spend today (2026-06-01, UTC)" in out


def test_summary_week_header_shows_start_to_end_range() -> None:
    out = format_usage_summary(
        _summary(),
        period="week",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=False,
    )
    assert "spend this week (2026-05-26 → 2026-06-01, UTC)" in out


def test_summary_populated_renders_total_top_providers_top_models_and_largest() -> None:
    result = _summary(
        total_cost=0.1234,
        call_count=47,
        top_providers=[
            ("anthropic", 0.0892, 72.3),
            ("openai", 0.0234, 18.9),
            ("deepseek", 0.0108, 8.8),
        ],
        top_models=[
            ("claude-sonnet-4-6", 0.0712, 57.7),
            ("gpt-5-mini", 0.0156, 12.6),
        ],
        largest_call=("evt-1", "claude-sonnet-4-6", 0.0089, _WEEK_END_MS),
    )
    out = format_usage_summary(
        result,
        period="week",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=False,
    )
    assert "total: $0.1234  across 47 calls" in out
    assert "top providers:" in out
    assert "Anthropic" in out  # branded
    assert "top models:" in out
    assert "claude-sonnet-4-6" in out
    assert "largest call:" in out
    # ISO-style timestamp on the largest_call line.
    assert "2026-06-01 12:00 UTC" in out


def test_summary_singular_call_count_is_grammatical() -> None:
    out = format_usage_summary(
        _summary(total_cost=0.01, call_count=1, top_providers=[("openai", 0.01, 100.0)]),
        period="week",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=False,
    )
    assert "across 1 call" in out  # not "1 calls"
    assert "across 1 calls" not in out


def test_summary_top_providers_and_top_models_align_vertically() -> None:
    """Provider names ("Anthropic" = 9) are shorter than model names
    ("claude-sonnet-4-6" = 17). The shared key-column width forces
    both blocks to pad to the longer key, so bars / costs / pcts
    line up when the eye sweeps from the providers block to the
    models block."""
    result = _summary(
        total_cost=0.10,
        call_count=2,
        top_providers=[("anthropic", 0.08, 80.0), ("openai", 0.02, 20.0)],
        top_models=[
            ("claude-sonnet-4-6", 0.08, 80.0),
            ("gpt-5-nano", 0.02, 20.0),
        ],
    )
    out = format_usage_summary(
        result,
        period="week",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        color_enabled=False,
        include_failed=False,
    )
    lines = out.split("\n")
    # The bar's first glyph marks the start of the bar column. Pull it
    # off one row in each block and assert they match.
    p_line = next(line for line in lines if "Anthropic" in line)
    m_line = next(line for line in lines if "claude-sonnet-4-6" in line)
    assert p_line.index("▅") == m_line.index("▅")
    # `$` marks the start of the cost column. Same invariant.
    assert p_line.index("$") == m_line.index("$")


def test_summary_omits_largest_call_section_when_none() -> None:
    out = format_usage_summary(
        _summary(
            total_cost=0.01,
            call_count=1,
            top_providers=[("openai", 0.01, 100.0)],
            top_models=[("gpt-5", 0.01, 100.0)],
            largest_call=None,
        ),
        period="week",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=False,
    )
    assert "largest call:" not in out


def test_summary_color_enabled_marks_leader_row_bold_green_in_each_block() -> None:
    out = format_usage_summary(
        _summary(
            total_cost=0.10,
            call_count=2,
            top_providers=[("anthropic", 0.08, 80.0), ("openai", 0.02, 20.0)],
            top_models=[("claude-sonnet-4-6", 0.08, 80.0), ("gpt-5", 0.02, 20.0)],
        ),
        period="week",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=True,
    )
    anthropic_line = next(line for line in out.split("\n") if "Anthropic" in line)
    sonnet_line = next(
        line for line in out.split("\n") if "claude-sonnet-4-6" in line and "▅" in line
    )
    # Leader rows are bold + green; deliberately *not* heat-colored.
    assert "1" in _extract_ansi_codes(anthropic_line)
    assert "32" in _extract_ansi_codes(anthropic_line)
    assert "1" in _extract_ansi_codes(sonnet_line)
    assert "32" in _extract_ansi_codes(sonnet_line)


def test_summary_color_enabled_marks_section_labels_cyan() -> None:
    out = format_usage_summary(
        _summary(
            total_cost=0.10,
            call_count=2,
            top_providers=[("anthropic", 0.10, 100.0)],
            top_models=[("claude-sonnet-4-6", 0.10, 100.0)],
            largest_call=("e", "claude-sonnet-4-6", 0.10, _WEEK_END_MS),
        ),
        period="week",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=True,
    )
    for label in ("top providers:", "top models:", "largest call:"):
        label_line = next(line for line in out.split("\n") if label in line)
        # cyan = 36.
        assert "36" in _extract_ansi_codes(label_line)


def test_summary_color_enabled_non_leader_rows_use_per_column_palette() -> None:
    """Non-leader rows: bar white (37), cost yellow (33), pct magenta (35).
    No green (32) anywhere on these rows — that's the leader's exclusive cue."""
    out = format_usage_summary(
        _summary(
            total_cost=0.10,
            call_count=2,
            top_providers=[("anthropic", 0.08, 80.0), ("openai", 0.02, 20.0)],
        ),
        period="week",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=True,
    )
    openai_line = next(line for line in out.split("\n") if "OpenAI" in line)
    codes = _extract_ansi_codes(openai_line)
    assert "37" in codes  # white (bar)
    assert "33" in codes  # yellow (cost)
    assert "35" in codes  # magenta (pct)
    assert "32" not in codes  # never green on non-leader


def test_summary_color_disabled_emits_no_ansi() -> None:
    out = format_usage_summary(
        _summary(total_cost=0.05, call_count=3, top_providers=[("openai", 0.05, 100.0)]),
        period="week",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=False,
    )
    assert _ANSI_RE.search(out) is None


def test_summary_failure_footnote_reflects_include_failed_flag() -> None:
    result = _summary(total_cost=0.05, call_count=3, top_providers=[("openai", 0.05, 100.0)])
    excluded = format_usage_summary(
        result,
        period="week",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        color_enabled=False,
        include_failed=False,
    )
    included = format_usage_summary(
        result,
        period="week",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        color_enabled=False,
        include_failed=True,
    )
    assert "excluded" in excluded and "included" not in excluded
    assert "included" in included and "excluded" not in included


# === format_spend_groups ================================================


def _groups(
    *entries: tuple[str, float, int],
    total_cost: float | None = None,
    total_calls: int = 0,
) -> QuerySpendResult:
    """Build a `QuerySpendResult` from (key, cost, calls) tuples."""
    spend_groups = [
        SpendGroup(key=k, cost_usd=c, calls=n, input_tokens=0, output_tokens=0)
        for k, c, n in entries
    ]
    return QuerySpendResult(
        total_cost_usd=total_cost if total_cost is not None else sum(c for _, c, _ in entries),
        total_calls=total_calls or sum(n for _, _, n in entries),
        total_input_tokens=0,
        total_output_tokens=0,
        groups=spend_groups,
    )


def test_groups_empty_renders_only_header_and_empty_state() -> None:
    out = format_spend_groups(
        _groups(),
        period="week",
        group_by="provider",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=False,
    )
    lines = out.split("\n")
    assert len(lines) == 2
    assert "by provider" in lines[0]
    assert "no calls recorded" in lines[1]


def test_groups_populated_renders_one_row_per_group_with_bars_costs_calls_pct() -> None:
    out = format_spend_groups(
        _groups(("anthropic", 0.0892, 12), ("openai", 0.0234, 8), ("deepseek", 0.0108, 5)),
        period="week",
        group_by="provider",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=False,
    )
    assert "Anthropic" in out  # branded display name
    assert "$0.0892" in out
    assert "12 calls" in out
    # Pct is share-of-total; Anthropic is 72.3% of (0.0892+0.0234+0.0108)=0.1234.
    assert "72.3%" in out


def test_groups_singular_call_count_is_grammatical() -> None:
    out = format_spend_groups(
        _groups(("anthropic", 0.05, 1), ("openai", 0.05, 1)),
        period="week",
        group_by="provider",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=False,
    )
    assert "1 call " in out  # space-suffixed so it doesn't match "1 calls"
    assert "1 calls" not in out


def test_groups_color_enabled_marks_top_row_bold_green() -> None:
    out = format_spend_groups(
        _groups(("anthropic", 0.08, 1), ("openai", 0.02, 1)),
        period="week",
        group_by="provider",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=True,
    )
    leader = next(line for line in out.split("\n") if "Anthropic" in line)
    assert "1" in _extract_ansi_codes(leader)
    assert "32" in _extract_ansi_codes(leader)


def test_groups_color_enabled_non_leader_rows_use_per_column_palette() -> None:
    """Per-column colors on non-leader rows: bar white (37), cost yellow (33),
    calls cyan (36), pct magenta (35). The extra cyan vs the summary view
    comes from the calls column that summary rows don't have."""
    out = format_spend_groups(
        _groups(("anthropic", 0.08, 1), ("openai", 0.02, 3)),
        period="week",
        group_by="provider",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=True,
    )
    openai_line = next(line for line in out.split("\n") if "OpenAI" in line)
    codes = _extract_ansi_codes(openai_line)
    assert "37" in codes  # white (bar)
    assert "33" in codes  # yellow (cost)
    assert "36" in codes  # cyan (calls)
    assert "35" in codes  # magenta (pct)
    assert "32" not in codes  # never green on non-leader


def test_groups_bars_are_linear_proportional_to_total() -> None:
    """50% row should fill ~half the bar; 25% row ~quarter; 10% row ~tenth."""
    out = format_spend_groups(
        _groups(("anthropic", 0.50, 1), ("openai", 0.25, 1), ("deepseek", 0.10, 1)),
        period="week",
        group_by="provider",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=False,
        bar_width=10,
    )
    by_provider = {row.split()[0]: row for row in out.split("\n") if "▅" in row}
    # Pct numerator: each cost / 0.85 total.
    # anthropic 50/85 ≈ 58.8% → 6 cells. openai 25/85 ≈ 29.4% → 3 cells.
    # deepseek 10/85 ≈ 11.8% → 1 cell.
    assert by_provider["Anthropic"].count("▅") == 6
    assert by_provider["OpenAI"].count("▅") == 3
    assert by_provider["DeepSeek"].count("▅") == 1


def test_groups_day_axis_renders_key_as_date_unchanged() -> None:
    """Day rollups have date keys (YYYY-MM-DD); no provider-display
    transformation should be applied."""
    out = format_spend_groups(
        _groups(("2026-06-01", 0.05, 2), ("2026-05-31", 0.03, 1)),
        period="week",
        group_by="day",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=False,
    )
    assert "2026-06-01" in out
    assert "2026-05-31" in out
    assert "by day" in out


def test_groups_color_disabled_emits_no_ansi() -> None:
    out = format_spend_groups(
        _groups(("anthropic", 0.05, 1)),
        period="week",
        group_by="provider",
        start_ms=_WEEK_START_MS,
        end_ms=_WEEK_END_MS,
        include_failed=False,
        color_enabled=False,
    )
    assert _ANSI_RE.search(out) is None


# === format_status =======================================================


def _providers(
    *entries: tuple[str, str, bool, str, int],
) -> list[StatusProvider]:
    """Build `StatusProvider` rows from (name, display, key_set, url, count) tuples."""
    return [
        StatusProvider(
            name=name,
            display_name=display,
            key_set=key_set,
            base_url=url,
            model_count=count,
        )
        for name, display, key_set, url, count in entries
    ]


def _default_providers() -> list[StatusProvider]:
    return _providers(
        ("anthropic", "Anthropic", True, "https://api.anthropic.com", 8),
        ("openai", "OpenAI", False, "https://api.openai.com/v1", 24),
        ("deepseek", "DeepSeek", True, "https://api.deepseek.com", 2),
        ("qwen", "Qwen", False, "https://dashscope.aliyuncs.com/compatible-mode/v1", 4),
    )


def _status(
    *,
    database: StatusDatabase | None = None,
    proxy: StatusProxy | None = None,
    providers: list[StatusProvider] | None = None,
    pricing: StatusPricing | None = None,
    version: str = "0.1.0",
) -> StatusReport:
    return StatusReport(
        version=version,
        database=database,
        proxy=proxy or StatusProxy(host="127.0.0.1", port=5525, reachable=False),
        providers=providers or _default_providers(),
        pricing=pricing,
    )


def test_status_no_db_renders_not_initialized_hint() -> None:
    out = format_status(_status(), color_enabled=False, now_ms=_WEEK_END_MS)
    assert "Database" in out
    assert "not initialized" in out
    # No Pricing section appears when there's no DB.
    assert "Pricing" not in out


def test_status_renders_version_banner_first() -> None:
    out = format_status(_status(version="9.9.9"), color_enabled=False, now_ms=_WEEK_END_MS)
    assert out.splitlines()[0] == "llm-usage 9.9.9"


def test_status_database_block_shows_path_size_schema_events() -> None:
    db = StatusDatabase(
        path="/tmp/test.db",
        size_bytes=1_250_000,
        schema_revision="abc123",
        schema_at_head=True,
        event_count=42,
        oldest_event_ms=_WEEK_START_MS,
        newest_event_ms=_WEEK_END_MS,
    )
    out = format_status(
        _status(
            database=db,
            pricing=StatusPricing(
                model_count=10, provider_count=4, newest_fetched_at_ms=_WEEK_END_MS
            ),
        ),
        color_enabled=False,
        now_ms=_WEEK_END_MS,
    )
    assert "/tmp/test.db" in out
    assert "1.2 MB" in out
    assert "head (rev abc123)" in out
    assert "42 (oldest 2026-05-26, newest 2026-06-01" in out


def test_status_database_block_shows_empty_state_when_no_events() -> None:
    db = StatusDatabase(
        path="/tmp/test.db",
        size_bytes=4096,
        schema_revision="abc123",
        schema_at_head=True,
        event_count=0,
        oldest_event_ms=None,
        newest_event_ms=None,
    )
    out = format_status(_status(database=db), color_enabled=False, now_ms=_WEEK_END_MS)
    assert "none recorded yet" in out


def test_status_schema_behind_head_flags_attention() -> None:
    db = StatusDatabase(
        path="/tmp/test.db",
        size_bytes=4096,
        schema_revision="oldrev",
        schema_at_head=False,
        event_count=0,
        oldest_event_ms=None,
        newest_event_ms=None,
    )
    out = format_status(_status(database=db), color_enabled=False, now_ms=_WEEK_END_MS)
    assert "behind" in out
    assert "next boot will migrate" in out


def test_status_proxy_running_renders_green_without_start_hint() -> None:
    out = format_status(
        _status(
            proxy=StatusProxy(host="127.0.0.1", port=5525, reachable=True),
        ),
        color_enabled=False,
        now_ms=_WEEK_END_MS,
    )
    assert "running" in out
    assert "start" not in out  # hint suppressed when already running
    assert "127.0.0.1:5525" in out


def test_status_proxy_not_running_renders_yellow_and_start_hint() -> None:
    out = format_status(
        _status(
            proxy=StatusProxy(host="127.0.0.1", port=5525, reachable=False),
        ),
        color_enabled=False,
        now_ms=_WEEK_END_MS,
    )
    assert "not running" in out
    assert "uv run llm-usage proxy" in out


def test_status_proxy_unknown_renders_no_net_marker() -> None:
    out = format_status(
        _status(
            proxy=StatusProxy(host="127.0.0.1", port=5525, reachable=None),
        ),
        color_enabled=False,
        now_ms=_WEEK_END_MS,
    )
    assert "unknown (--no-net)" in out


def test_status_providers_block_lists_every_provider_with_key_state() -> None:
    out = format_status(_status(), color_enabled=False, now_ms=_WEEK_END_MS)
    for name in ("Anthropic", "OpenAI", "DeepSeek", "Qwen"):
        assert name in out
    # Anthropic + DeepSeek have keys; OpenAI + Qwen don't.
    assert "key set" in out
    assert "key missing" in out


def test_status_providers_block_shows_each_base_url() -> None:
    out = format_status(_status(), color_enabled=False, now_ms=_WEEK_END_MS)
    assert "https://api.anthropic.com" in out
    assert "https://api.openai.com/v1" in out


def test_status_providers_block_handles_singular_model_count() -> None:
    out = format_status(
        _status(
            providers=_providers(
                ("openai", "OpenAI", True, "https://api.openai.com/v1", 1),
            ),
        ),
        color_enabled=False,
        now_ms=_WEEK_END_MS,
    )
    assert "1 model priced" in out
    assert "1 models priced" not in out


def test_status_pricing_block_shows_catalog_and_refreshed_age() -> None:
    # Fetched two days before _WEEK_END_MS.
    two_days_ms = 2 * 24 * 3600 * 1000
    out = format_status(
        _status(
            database=_full_db(),
            pricing=StatusPricing(
                model_count=38,
                provider_count=4,
                newest_fetched_at_ms=_WEEK_END_MS - two_days_ms,
            ),
        ),
        color_enabled=False,
        now_ms=_WEEK_END_MS,
    )
    assert "38 models across 4 providers" in out
    assert "2 days ago" in out


def test_status_pricing_stale_pricing_flags_attention_color() -> None:
    """Fetched > 14 days ago → yellow."""
    twenty_days_ms = 20 * 24 * 3600 * 1000
    out = format_status(
        _status(
            database=_full_db(),
            pricing=StatusPricing(
                model_count=38,
                provider_count=4,
                newest_fetched_at_ms=_WEEK_END_MS - twenty_days_ms,
            ),
        ),
        color_enabled=True,
        now_ms=_WEEK_END_MS,
    )
    # The "20 days ago" line lives on the refreshed row; yellow = SGR 33.
    refreshed_line = next(line for line in out.split("\n") if "20 days ago" in line)
    assert "33" in _extract_ansi_codes(refreshed_line)


def test_status_color_disabled_emits_no_ansi() -> None:
    out = format_status(
        _status(
            database=_full_db(),
            pricing=StatusPricing(
                model_count=4, provider_count=4, newest_fetched_at_ms=_WEEK_END_MS
            ),
        ),
        color_enabled=False,
        now_ms=_WEEK_END_MS,
    )
    assert _ANSI_RE.search(out) is None


def test_status_color_enabled_marks_section_labels_cyan() -> None:
    out = format_status(
        _status(
            database=_full_db(),
            pricing=StatusPricing(
                model_count=4, provider_count=4, newest_fetched_at_ms=_WEEK_END_MS
            ),
        ),
        color_enabled=True,
        now_ms=_WEEK_END_MS,
    )
    for label in ("Database", "Capture proxy", "Providers", "Pricing"):
        label_line = next(line for line in out.split("\n") if label in line)
        assert "36" in _extract_ansi_codes(label_line)


def test_status_color_enabled_marks_key_set_green_and_missing_yellow() -> None:
    out = format_status(_status(), color_enabled=True, now_ms=_WEEK_END_MS)
    set_line = next(line for line in out.split("\n") if "Anthropic" in line)
    missing_line = next(line for line in out.split("\n") if "OpenAI" in line)
    assert "32" in _extract_ansi_codes(set_line)  # green
    assert "33" in _extract_ansi_codes(missing_line)  # yellow


def _full_db() -> StatusDatabase:
    return StatusDatabase(
        path="/tmp/test.db",
        size_bytes=2048,
        schema_revision="abc123",
        schema_at_head=True,
        event_count=10,
        oldest_event_ms=_WEEK_START_MS,
        newest_event_ms=_WEEK_END_MS,
    )


# --- providers renderer ---------------------------------------------------


def _provider_row(
    name: str,
    *,
    display_name: str | None = None,
    openai_compatible: bool = True,
    key_set: bool = False,
    base_url: str = "https://example.com",
    models: list[str] | None = None,
) -> ProviderRow:
    """Small constructor; defaults keep individual tests focused on one axis."""
    return ProviderRow(
        name=name,
        display_name=display_name or name.title(),
        openai_compatible=openai_compatible,
        key_set=key_set,
        base_url=base_url,
        models=models or [],
    )


def _providers_report(*rows: ProviderRow) -> ProvidersReport:
    return ProvidersReport(providers=list(rows))


def test_providers_renders_a_header_naming_the_count() -> None:
    """The section label spells out how many providers are listed —
    helps the reader sanity-check that nothing is filtered out."""
    out = format_providers(
        _providers_report(
            _provider_row("anthropic", display_name="Anthropic", openai_compatible=False),
            _provider_row("openai", display_name="OpenAI"),
        ),
        color_enabled=False,
    )
    first_line = out.split("\n")[0]
    assert "Providers" in first_line
    assert "2 known" in first_line


def test_providers_emits_one_data_row_per_provider() -> None:
    report = _providers_report(
        _provider_row("anthropic", display_name="Anthropic"),
        _provider_row("openai", display_name="OpenAI"),
        _provider_row("deepseek", display_name="DeepSeek"),
        _provider_row("qwen", display_name="Qwen"),
    )
    out = format_providers(report, color_enabled=False)
    # Header + 4 rows = 5 lines total.
    assert len(out.split("\n")) == 5


def test_providers_renders_branded_display_names() -> None:
    """The CamelCase display name is what the user sees, not the
    lowercase DB name."""
    out = format_providers(
        _providers_report(
            _provider_row("openai", display_name="OpenAI"),
            _provider_row("deepseek", display_name="DeepSeek"),
        ),
        color_enabled=False,
    )
    assert "OpenAI" in out
    assert "DeepSeek" in out
    # The raw lowercase form shouldn't appear as a row label.
    assert "openai " not in out  # trailing space catches the cell, not the URL


def test_providers_renders_key_state_strings_distinctly() -> None:
    """`key set` vs `key missing` is the headline signal — the renderer
    must spell both states out, not collapse to a checkbox."""
    out = format_providers(
        _providers_report(
            _provider_row("anthropic", display_name="Anthropic", key_set=True),
            _provider_row("openai", display_name="OpenAI", key_set=False),
        ),
        color_enabled=False,
    )
    set_line = next(line for line in out.split("\n") if "Anthropic" in line)
    missing_line = next(line for line in out.split("\n") if "OpenAI" in line)
    assert "key set" in set_line
    assert "key missing" in missing_line


def test_providers_renders_openai_compat_flag() -> None:
    """The `openai-compat: yes|no` column is the second informational
    axis — it tells the user whether they can swap an OpenAI client
    against the provider's base URL."""
    out = format_providers(
        _providers_report(
            _provider_row("anthropic", display_name="Anthropic", openai_compatible=False),
            _provider_row("openai", display_name="OpenAI", openai_compatible=True),
        ),
        color_enabled=False,
    )
    anthro_line = next(line for line in out.split("\n") if "Anthropic" in line)
    openai_line = next(line for line in out.split("\n") if "OpenAI" in line)
    assert "openai-compat: no" in anthro_line
    assert "openai-compat: yes" in openai_line


def test_providers_pluralizes_model_count() -> None:
    """`1 model priced` vs `N models priced` — the pluralization bug
    we hit on `spend` should not reappear here."""
    out = format_providers(
        _providers_report(
            _provider_row("openai", display_name="OpenAI", models=["only-one"]),
            _provider_row("deepseek", display_name="DeepSeek", models=["a", "b"]),
            _provider_row("qwen", display_name="Qwen", models=[]),
        ),
        color_enabled=False,
    )
    single_line = next(line for line in out.split("\n") if "OpenAI" in line)
    plural_line = next(line for line in out.split("\n") if "DeepSeek" in line)
    zero_line = next(line for line in out.split("\n") if "Qwen" in line)
    assert "1 model priced" in single_line
    assert "2 models priced" in plural_line
    assert "0 models priced" in zero_line


def test_providers_renders_base_url_verbatim() -> None:
    """Whatever URL `Settings.base_url_for` returns lands in the
    row — no normalization, so a `LLM_USAGE_*_BASE_URL` override
    shows up exactly as the user set it."""
    out = format_providers(
        _providers_report(
            _provider_row(
                "anthropic",
                display_name="Anthropic",
                base_url="https://proxy.example.com/foo",
            ),
        ),
        color_enabled=False,
    )
    assert "https://proxy.example.com/foo" in out


def test_providers_show_models_expands_one_line_per_model() -> None:
    """`--models` flag: each priced model on its own indented line
    underneath the provider row."""
    out = format_providers(
        _providers_report(
            _provider_row(
                "anthropic",
                display_name="Anthropic",
                models=["claude-opus-4-7", "claude-sonnet-4-6"],
            ),
        ),
        color_enabled=False,
        show_models=True,
    )
    # Header + 1 provider row + 2 model lines = 4.
    assert len(out.split("\n")) == 4
    assert "    claude-opus-4-7" in out
    assert "    claude-sonnet-4-6" in out


def test_providers_show_models_hint_for_unseeded_provider() -> None:
    """A provider with no priced models should print an explicit hint
    rather than a silent empty block — otherwise `--models` would
    look broken for providers whose pricing hasn't been seeded."""
    out = format_providers(
        _providers_report(_provider_row("anthropic", display_name="Anthropic", models=[])),
        color_enabled=False,
        show_models=True,
    )
    assert "no models priced yet" in out


def test_providers_show_models_off_by_default() -> None:
    """Without `--show_models`, model names do not appear in the
    output even if the provider has priced models."""
    out = format_providers(
        _providers_report(
            _provider_row("anthropic", display_name="Anthropic", models=["claude-opus-4-7"]),
        ),
        color_enabled=False,
    )
    assert "claude-opus-4-7" not in out


def test_providers_color_disabled_emits_no_ansi() -> None:
    out = format_providers(
        _providers_report(
            _provider_row("anthropic", display_name="Anthropic", key_set=True),
            _provider_row("openai", display_name="OpenAI", key_set=False),
        ),
        color_enabled=False,
    )
    assert _ANSI_RE.search(out) is None


def test_providers_color_enabled_marks_section_label_cyan() -> None:
    out = format_providers(
        _providers_report(_provider_row("anthropic", display_name="Anthropic")),
        color_enabled=True,
    )
    header = out.split("\n")[0]
    assert "36" in _extract_ansi_codes(header)  # cyan fg
    assert "1" in _extract_ansi_codes(header)  # bold


def test_providers_color_enabled_marks_key_set_green_and_missing_yellow() -> None:
    out = format_providers(
        _providers_report(
            _provider_row("anthropic", display_name="Anthropic", key_set=True),
            _provider_row("openai", display_name="OpenAI", key_set=False),
        ),
        color_enabled=True,
    )
    set_line = next(line for line in out.split("\n") if "Anthropic" in line)
    missing_line = next(line for line in out.split("\n") if "OpenAI" in line)
    assert "32" in _extract_ansi_codes(set_line)  # green
    assert "33" in _extract_ansi_codes(missing_line)  # yellow


def test_providers_rows_align_when_model_counts_have_different_widths() -> None:
    """Regression: the model-count column was variable width (8 vs 120
    models) on the first prototype, so the base URL didn't line up
    vertically. Padding the suffix to its widest entry fixes this; the
    test pins it by checking that the base URL appears at the same
    character offset in every row."""
    report = _providers_report(
        _provider_row("a", display_name="AAA", models=["x"]),  # 1 model = "1 model priced"
        _provider_row("b", display_name="BBB", models=["y"] * 120),  # widest
    )
    out = format_providers(report, color_enabled=False)
    data_lines = out.split("\n")[1:]  # skip header
    assert len(data_lines) == 2
    offsets = [line.index("https://example.com") for line in data_lines]
    assert offsets[0] == offsets[1]


def test_providers_empty_report_renders_a_one_line_hint() -> None:
    """Defensive: `collect_providers` always returns rows, but if a
    caller hands in an empty report the renderer shouldn't crash."""
    out = format_providers(_providers_report(), color_enabled=False)
    assert "no known providers" in out
    assert "\n" not in out


# --- format_recommend_result ---------------------------------------------


def _recommend_result(
    *,
    provider: str = "qwen",
    model: str = "qwen-flash",
    cost: float = 0.0042,
    alternatives: list[Alternative] | None = None,
    reasoning: str = "For task 'anything': recommending qwen/qwen-flash.",
) -> RecommendProviderResult:
    return RecommendProviderResult(
        provider=provider,
        model=model,
        estimated_cost_usd=cost,
        alternatives=alternatives if alternatives is not None else [],
        reasoning=reasoning,
    )


def _alt(provider: str, model: str, cost: float) -> Alternative:
    return Alternative(provider=provider, model=model, estimated_cost_usd=cost)


def test_recommend_renders_two_section_layout() -> None:
    """The renderer should produce a `Recommendation` block, a blank
    line, and a `Reasoning` block — readers should see the chosen
    model before they encounter the paragraph."""
    out = format_recommend_result(_recommend_result(), color_enabled=False)
    lines = out.split("\n")
    assert "Recommendation" in lines[0]
    # The blank line separates the two sections; find the Reasoning
    # label and check there's at least one empty line above it.
    reasoning_idx = next(i for i, line in enumerate(lines) if "Reasoning" in line)
    assert "" in lines[:reasoning_idx]


def test_recommend_renders_branded_provider_name() -> None:
    """`deepseek` → `DeepSeek` in the chosen-model row (same convention
    as compare's leader row)."""
    out = format_recommend_result(
        _recommend_result(provider="deepseek", model="cheap-1"),
        color_enabled=False,
    )
    chosen_row = next(line for line in out.split("\n") if "cheap-1" in line)
    assert "DeepSeek" in chosen_row
    # The lowercase form shouldn't appear as a row label.
    assert "deepseek / cheap-1" not in chosen_row


def test_recommend_renders_4dp_cost() -> None:
    """`$0.0042` — 4 decimal places, matching `compare` and `spend`."""
    out = format_recommend_result(
        _recommend_result(cost=0.0042),
        color_enabled=False,
    )
    assert "$0.0042" in out


def test_recommend_word_wraps_long_reasoning_paragraph() -> None:
    """A reasoning paragraph longer than the wrap width should be
    split across multiple lines — the user shouldn't have to scroll
    horizontally to read it."""
    long_reasoning = (
        "For task 'a very long description here that should wrap': "
        "recommending qwen/qwen-flash — the cheapest projected cost "
        "among 159 priced model(s). Estimated $0.0042 for the workload. "
        "v1 ranks by cost only; task_description is echoed for context "
        "but does not drive selection."
    )
    out = format_recommend_result(
        _recommend_result(reasoning=long_reasoning),
        color_enabled=False,
    )
    reasoning_lines = [line for line in out.split("\n") if line.startswith("  ")]
    # At least two lines under either section (chosen row + reasoning
    # paragraph wrapped onto multiple lines). The wrapped paragraph
    # specifically must span >1 line.
    paragraph_lines = [line for line in reasoning_lines if "qwen" in line.lower() or "v1" in line]
    assert len(paragraph_lines) >= 2


def test_recommend_color_disabled_emits_no_ansi() -> None:
    out = format_recommend_result(_recommend_result(), color_enabled=False)
    assert _ANSI_RE.search(out) is None


def test_recommend_color_enabled_marks_section_labels_cyan() -> None:
    out = format_recommend_result(_recommend_result(), color_enabled=True)
    for label in ("Recommendation", "Reasoning"):
        label_line = next(line for line in out.split("\n") if label in line)
        assert "36" in _extract_ansi_codes(label_line)  # cyan
        assert "1" in _extract_ansi_codes(label_line)  # bold


def test_recommend_color_enabled_marks_chosen_row_green_and_bold() -> None:
    """The chosen-model row gets the leader-row stripe (bold green) so
    the user's eye lands there first."""
    out = format_recommend_result(
        _recommend_result(provider="qwen", model="qwen-flash"),
        color_enabled=True,
    )
    chosen_row = next(line for line in out.split("\n") if "qwen-flash" in line)
    assert "32" in _extract_ansi_codes(chosen_row)  # green
    assert "1" in _extract_ansi_codes(chosen_row)  # bold


def test_recommend_color_enabled_dims_the_reasoning_paragraph() -> None:
    """Reasoning is informational and dimmed so it doesn't compete
    with the chosen-row green."""
    out = format_recommend_result(_recommend_result(), color_enabled=True)
    # Find a reasoning line (it starts with two spaces, contains text from
    # the paragraph, and is NOT the chosen row).
    reasoning_idx = next(i for i, line in enumerate(out.split("\n")) if "Reasoning" in line)
    paragraph_line = out.split("\n")[reasoning_idx + 1]
    assert "2" in _extract_ansi_codes(paragraph_line)  # dim


# --- recommend: alternatives block ---------------------------------------


def test_recommend_renders_alternatives_block_when_non_empty() -> None:
    """When the result has alternatives, the renderer must produce an
    `Alternatives` section between Recommendation and Reasoning."""
    out = format_recommend_result(
        _recommend_result(
            alternatives=[
                _alt("deepseek", "deepseek-coder", 0.0004),
                _alt("openai", "gpt-5-nano", 0.0004),
            ],
        ),
        color_enabled=False,
    )
    lines = out.split("\n")
    rec_idx = next(i for i, line in enumerate(lines) if "Recommendation" in line)
    alt_idx = next(i for i, line in enumerate(lines) if "Alternatives" in line)
    rea_idx = next(i for i, line in enumerate(lines) if "Reasoning" in line)
    # Order: Recommendation < Alternatives < Reasoning.
    assert rec_idx < alt_idx < rea_idx


def test_recommend_renders_one_indented_row_per_alternative() -> None:
    out = format_recommend_result(
        _recommend_result(
            alternatives=[
                _alt("deepseek", "deepseek-coder", 0.0004),
                _alt("openai", "gpt-5-nano", 0.0004),
            ],
        ),
        color_enabled=False,
    )
    # Branded names + cost should appear in indented rows.
    assert "  DeepSeek / deepseek-coder" in out
    assert "  OpenAI / gpt-5-nano" in out
    # Cost column shows 4dp like the chosen row.
    assert out.count("$0.0004") == 2


def test_recommend_suppresses_alternatives_block_when_empty() -> None:
    """The constraint the user asked for: a single-option result must
    not show an empty `Alternatives` header."""
    out = format_recommend_result(_recommend_result(alternatives=[]), color_enabled=False)
    assert "Alternatives" not in out
    # Reasoning is still present.
    assert "Reasoning" in out


def test_recommend_aligns_alternative_cost_column() -> None:
    """The `$X.XXXX` column should line up vertically across
    alternative rows, regardless of provider/model name length."""
    out = format_recommend_result(
        _recommend_result(
            alternatives=[
                _alt("openai", "x", 0.0004),
                _alt("anthropic", "this-is-a-much-longer-model-name", 0.0010),
            ],
        ),
        color_enabled=False,
    )
    cost_lines = [line for line in out.split("\n") if line.startswith("  ") and "$" in line]
    # All cost columns should start at the same offset.
    offsets = [line.index("$") for line in cost_lines]
    assert len(set(offsets)) == 1, f"costs misaligned: {offsets}"


def test_recommend_color_enabled_keeps_chosen_row_green_and_alternatives_default() -> None:
    """The chosen row should stay green/bold (leader-row convention);
    alternatives stay in default color so the chosen row pops without
    needing color contrast on the runner-ups."""
    out = format_recommend_result(
        _recommend_result(
            provider="qwen",
            model="qwen-flash",
            alternatives=[_alt("deepseek", "deepseek-coder", 0.0004)],
        ),
        color_enabled=True,
    )
    chosen_line = next(line for line in out.split("\n") if "qwen-flash" in line)
    alt_line = next(line for line in out.split("\n") if "deepseek-coder" in line)
    assert "32" in _extract_ansi_codes(chosen_line)  # green
    # Alternative row should not carry green foreground.
    assert "32" not in _extract_ansi_codes(alt_line)


# --- format_pricing_catalog ---------------------------------------------


def _pricing(
    provider: str,
    model: str,
    *,
    input_rate: float,
    output_rate: float,
    cache_read: float | None = None,
    cache_write: float | None = None,
) -> PricingEntry:
    return PricingEntry(
        provider=provider,
        model=model,
        input_per_million_usd=input_rate,
        output_per_million_usd=output_rate,
        cache_read_per_million_usd=cache_read,
        cache_write_per_million_usd=cache_write,
        fetched_at=1,
    )


def test_catalog_empty_returns_one_line_hint() -> None:
    """No entries → single hint line, no table scaffolding."""
    out = format_pricing_catalog([], color_enabled=False)
    assert "no priced models" in out
    assert "\n" not in out


def test_catalog_renders_section_header_and_column_row() -> None:
    out = format_pricing_catalog(
        [
            _pricing("openai", "gpt-x", input_rate=1.0, output_rate=2.0),
        ],
        color_enabled=False,
    )
    lines = out.split("\n")
    assert "Pricing catalog" in lines[0]
    # Blank line between section header and column row.
    assert lines[1] == ""
    assert "Provider" in lines[2]
    assert "Model" in lines[2]
    assert "Input/M" in lines[2]
    assert "Output/M" in lines[2]


def test_catalog_renders_branded_provider_names() -> None:
    out = format_pricing_catalog(
        [
            _pricing("openai", "gpt-x", input_rate=1.0, output_rate=2.0),
            _pricing("deepseek", "ds-x", input_rate=1.0, output_rate=2.0),
        ],
        color_enabled=False,
    )
    # CamelCase brand names, not the lowercase DB form.
    assert "OpenAI" in out
    assert "DeepSeek" in out


def test_catalog_renders_2dp_rates() -> None:
    out = format_pricing_catalog(
        [_pricing("openai", "gpt-x", input_rate=1.234, output_rate=10.0)],
        color_enabled=False,
    )
    assert "$1.23" in out
    assert "$10.00" in out


def test_catalog_dedups_same_family_same_price_entries() -> None:
    """alias + dated snapshot at identical price → 1 row with ×2."""
    out = format_pricing_catalog(
        [
            _pricing("openai", "gpt-5-mini", input_rate=0.25, output_rate=1.0),
            _pricing("openai", "gpt-5-mini-2025-08-07", input_rate=0.25, output_rate=1.0),
        ],
        color_enabled=False,
    )
    # Snapshot variant must NOT appear; the alias represents both.
    assert "gpt-5-mini-2025-08-07" not in out
    nano_line = next(line for line in out.split("\n") if "gpt-5-mini" in line)
    assert "×2" in nano_line


def test_catalog_keeps_same_family_different_price_entries() -> None:
    """Price divergence within a family → both rows survive."""
    out = format_pricing_catalog(
        [
            _pricing("openai", "gpt-5-mini", input_rate=0.25, output_rate=1.0),
            _pricing("openai", "gpt-5-mini-2025-08-07", input_rate=0.20, output_rate=0.80),
        ],
        color_enabled=False,
    )
    assert "gpt-5-mini " in out  # alias row
    assert "gpt-5-mini-2025-08-07" in out  # snapshot row
    # Neither row carries a ×N marker (each is solo).
    assert "×" not in out


def test_catalog_show_all_disables_dedup() -> None:
    out = format_pricing_catalog(
        [
            _pricing("openai", "gpt-5-mini", input_rate=0.25, output_rate=1.0),
            _pricing("openai", "gpt-5-mini-2025-08-07", input_rate=0.25, output_rate=1.0),
        ],
        show_all=True,
        color_enabled=False,
    )
    assert "gpt-5-mini-2025-08-07" in out
    assert "×" not in out


def test_catalog_cache_columns_hidden_by_default() -> None:
    out = format_pricing_catalog(
        [
            _pricing(
                "anthropic",
                "claude-x",
                input_rate=1.0,
                output_rate=5.0,
                cache_read=0.1,
                cache_write=1.25,
            )
        ],
        color_enabled=False,
    )
    assert "Cache R/M" not in out
    assert "Cache W/M" not in out
    assert "$0.10" not in out  # cache_read rate not rendered
    # Footer points at the flag for discovery.
    assert "--cache to show" in out


def test_catalog_cache_columns_visible_with_show_cache() -> None:
    out = format_pricing_catalog(
        [
            _pricing(
                "anthropic",
                "claude-x",
                input_rate=1.0,
                output_rate=5.0,
                cache_read=0.1,
                cache_write=1.25,
            )
        ],
        show_cache=True,
        color_enabled=False,
    )
    assert "Cache R/M" in out
    assert "Cache W/M" in out
    assert "$0.10" in out
    assert "$1.25" in out


def test_catalog_cache_columns_render_dash_for_missing_rates() -> None:
    """A model with no cache rates should show `—` in the cache cells
    rather than `$0.00` or a blank — `—` makes "absent" obvious."""
    out = format_pricing_catalog(
        [
            _pricing(
                "qwen",
                "qwen-x",
                input_rate=0.05,
                output_rate=0.2,
                cache_read=None,
                cache_write=None,
            )
        ],
        show_cache=True,
        color_enabled=False,
    )
    qwen_line = next(line for line in out.split("\n") if "qwen-x" in line)
    assert "—" in qwen_line


def test_catalog_sort_provider_keeps_input_order() -> None:
    """Default `sort=provider` preserves the alphabetical (provider,
    model) order that `query_pricing` provides."""
    entries = [
        _pricing("anthropic", "a", input_rate=10.0, output_rate=20.0),
        _pricing("openai", "b", input_rate=1.0, output_rate=2.0),
        _pricing("qwen", "c", input_rate=5.0, output_rate=10.0),
    ]
    out = format_pricing_catalog(entries, color_enabled=False)
    a_idx = out.index("Anthropic")
    o_idx = out.index("OpenAI")
    q_idx = out.index("Qwen")
    assert a_idx < o_idx < q_idx


def test_catalog_sort_input_orders_by_input_rate_ascending() -> None:
    entries = [
        _pricing("anthropic", "expensive", input_rate=10.0, output_rate=20.0),
        _pricing("openai", "cheap", input_rate=1.0, output_rate=2.0),
        _pricing("qwen", "mid", input_rate=5.0, output_rate=10.0),
    ]
    out = format_pricing_catalog(entries, sort="input", color_enabled=False)
    assert "sorted by input rate" in out
    cheap_idx = out.index("cheap")
    mid_idx = out.index("mid")
    expensive_idx = out.index("expensive")
    assert cheap_idx < mid_idx < expensive_idx


def test_catalog_sort_output_orders_by_output_rate_ascending() -> None:
    entries = [
        _pricing("anthropic", "high-out", input_rate=1.0, output_rate=100.0),
        _pricing("openai", "low-out", input_rate=1.0, output_rate=2.0),
    ]
    out = format_pricing_catalog(entries, sort="output", color_enabled=False)
    assert "sorted by output rate" in out
    assert out.index("low-out") < out.index("high-out")


def test_catalog_unknown_sort_raises() -> None:
    """Defensive — unknown sort axis must surface as an error rather
    than silently fall through to provider-order."""
    import pytest

    entries = [_pricing("openai", "x", input_rate=1.0, output_rate=2.0)]
    with pytest.raises(ValueError, match="unknown sort axis"):
        format_pricing_catalog(entries, sort="cost", color_enabled=False)


def test_catalog_color_disabled_emits_no_ansi() -> None:
    out = format_pricing_catalog(
        [_pricing("openai", "gpt-x", input_rate=1.0, output_rate=2.0)],
        color_enabled=False,
    )
    assert _ANSI_RE.search(out) is None


def test_catalog_color_enabled_marks_section_label_cyan() -> None:
    out = format_pricing_catalog(
        [_pricing("openai", "gpt-x", input_rate=1.0, output_rate=2.0)],
        color_enabled=True,
    )
    header = out.split("\n")[0]
    assert "36" in _extract_ansi_codes(header)  # cyan
    assert "1" in _extract_ansi_codes(header)  # bold


def test_catalog_variant_marker_is_dim_when_color_enabled() -> None:
    """`×N` cells get the dim attribute so they don't compete with
    the rate columns — same convention as `compare`'s variant column."""
    out = format_pricing_catalog(
        [
            _pricing("openai", "gpt-5-mini", input_rate=1.0, output_rate=2.0),
            _pricing("openai", "gpt-5-mini-2025-08-07", input_rate=1.0, output_rate=2.0),
        ],
        color_enabled=True,
    )
    mini_line = next(line for line in out.split("\n") if "gpt-5-mini" in line)
    assert "2" in _extract_ansi_codes(mini_line)  # dim somewhere on the line
