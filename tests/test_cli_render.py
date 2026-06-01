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
    format_spend_groups,
    format_usage_summary,
)
from llm_usage.core.models import (
    CompareProvidersResult,
    LargestCall,
    QuerySpendResult,
    RankedEntry,
    SpendGroup,
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
