"""Unit tests for `cli_render.format_compare_result`.

Pure-function tests with hand-built `CompareProvidersResult`s — no DB,
no Typer involvement. The renderer is the source of every visual
detail (column widths, bar glyph, color escape placement, footnote
text), so a regression here is what would change the user's terminal
experience even if the projection math is right.
"""

from __future__ import annotations

import re

from llm_usage.cli_render import format_compare_result
from llm_usage.core.models import CompareProvidersResult, RankedEntry

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
