"""End-to-end tests for `llm-usage models` via Typer's `CliRunner`.

The core function (`query_pricing`) is tested in `test_query_pricing.py`;
the renderer (`format_pricing_catalog`) is tested in `test_cli_render.py`.
These tests cover the CLI shim: argument parsing, `--json`, `--cache`,
`--sort`, `--all`, filter combinations, color resolution.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from llm_usage.bootstrap import migrate_to_head
from llm_usage.cli import app
from llm_usage.core.db.session import get_session
from llm_usage.core.pricing import Pricing, upsert_pricing

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def priced_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """DB with a controlled catalog that exercises every CLI branch."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(
            session,
            [
                # Two anthropic rows, one carrying cache rates.
                Pricing(
                    "anthropic",
                    "claude-haiku-4-5",
                    1.0,
                    5.0,
                    cache_read_per_million_usd=0.1,
                    cache_write_per_million_usd=1.25,
                    fetched_at=1,
                ),
                Pricing("anthropic", "claude-sonnet-4-5", 3.0, 15.0, fetched_at=1),
                # OpenAI: alias + pinned snapshot at identical price
                # (family-dedup target).
                Pricing("openai", "gpt-5-nano", 0.05, 0.4, fetched_at=1),
                Pricing("openai", "gpt-5-nano-2025-08-07", 0.05, 0.4, fetched_at=1),
                # Cheapest-rate row for --sort input.
                Pricing("qwen", "qwen-turbo", 0.05, 0.2, fetched_at=1),
            ],
        )
        session.commit()
    return db


@pytest.fixture
def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    return db


# --- happy path ----------------------------------------------------------


def test_models_default_renders_catalog(priced_db: Path, runner: CliRunner) -> None:
    """Bare `llm-usage models` returns a deduped catalog with provider /
    model / input rate / output rate columns."""
    result = runner.invoke(app, ["models", "--color", "never"])
    assert result.exit_code == 0, result.stdout
    assert "Pricing catalog" in result.stdout
    assert "Anthropic" in result.stdout
    assert "OpenAI" in result.stdout
    assert "Qwen" in result.stdout
    # Default columns only.
    assert "Cache" not in result.stdout


def test_models_empty_db_returns_clean_hint(empty_db: Path, runner: CliRunner) -> None:
    """An un-seeded DB should yield a single hint line, not a stack trace."""
    result = runner.invoke(app, ["models", "--color", "never"])
    assert result.exit_code == 0, result.stdout
    assert "no priced models" in result.stdout


# --- family dedup default vs --all --------------------------------------


def test_models_default_dedups_alias_snapshot_pairs(priced_db: Path, runner: CliRunner) -> None:
    """gpt-5-nano + gpt-5-nano-2025-08-07 at the same price collapse
    to one row with ×2; the snapshot variant is hidden by default."""
    result = runner.invoke(app, ["models", "--color", "never"])
    assert "gpt-5-nano-2025-08-07" not in result.stdout
    # The kept row carries the ×2 marker.
    nano_line = next(line for line in result.stdout.split("\n") if "gpt-5-nano " in line)
    assert "×2" in nano_line


def test_models_all_flag_shows_every_catalog_row(priced_db: Path, runner: CliRunner) -> None:
    """`--all` opts out of family-dedup; every row appears, no ×N column."""
    result = runner.invoke(app, ["models", "--all", "--color", "never"])
    assert "gpt-5-nano" in result.stdout
    assert "gpt-5-nano-2025-08-07" in result.stdout
    assert "×" not in result.stdout


# --- --cache toggle -----------------------------------------------------


def test_models_default_hides_cache_columns(priced_db: Path, runner: CliRunner) -> None:
    """Cache columns are hidden by default; the footer points users
    at the `--cache` flag for discovery."""
    result = runner.invoke(app, ["models", "--color", "never"])
    assert "Cache R/M" not in result.stdout
    assert "Cache W/M" not in result.stdout
    assert "--cache to show" in result.stdout


def test_models_cache_flag_renders_cache_columns(priced_db: Path, runner: CliRunner) -> None:
    """`--cache` adds Cache R/M + Cache W/M columns. Rows without cache
    rates render `—`; rows with rates show the dollar amount."""
    result = runner.invoke(
        app, ["models", "--cache", "--provider", "anthropic", "--color", "never"]
    )
    assert "Cache R/M" in result.stdout
    assert "Cache W/M" in result.stdout
    # Haiku has cache rates → dollar value present.
    haiku_line = next(line for line in result.stdout.split("\n") if "haiku-4-5" in line)
    assert "$0.10" in haiku_line
    # Sonnet has no cache rates → em dash.
    sonnet_line = next(line for line in result.stdout.split("\n") if "sonnet-4-5" in line)
    assert "—" in sonnet_line


# --- --sort axis --------------------------------------------------------


def test_models_default_sort_is_alphabetical_by_provider(
    priced_db: Path, runner: CliRunner
) -> None:
    """`--sort provider` (default) → Anthropic, OpenAI, Qwen reading order."""
    result = runner.invoke(app, ["models", "--color", "never"])
    data_lines = [
        line
        for line in result.stdout.split("\n")
        if any(p in line for p in ("Anthropic", "OpenAI", "Qwen"))
    ]
    providers_in_order = []
    for line in data_lines:
        for p in ("Anthropic", "OpenAI", "Qwen"):
            if line.startswith(p):
                providers_in_order.append(p)
                break
    # Each provider only appears in a contiguous block.
    seen: list[str] = []
    for p in providers_in_order:
        if not seen or seen[-1] != p:
            seen.append(p)
    assert seen == ["Anthropic", "OpenAI", "Qwen"]


def test_models_sort_input_lists_cheapest_input_first(priced_db: Path, runner: CliRunner) -> None:
    """`--sort input` → qwen-turbo and gpt-5-nano (both $0.05/M) at top."""
    result = runner.invoke(app, ["models", "--sort", "input", "--color", "never"])
    assert "sorted by input rate" in result.stdout
    # First data row should be one of the $0.05/M models.
    data_lines = [
        line
        for line in result.stdout.split("\n")
        if line and (line.startswith(("Anthropic", "OpenAI", "Qwen")))
    ]
    first = data_lines[0]
    assert "qwen-turbo" in first or "gpt-5-nano" in first


def test_models_sort_output_lists_cheapest_output_first(priced_db: Path, runner: CliRunner) -> None:
    """`--sort output` → qwen-turbo ($0.20/M out) first."""
    result = runner.invoke(app, ["models", "--sort", "output", "--color", "never"])
    assert "sorted by output rate" in result.stdout
    data_lines = [
        line
        for line in result.stdout.split("\n")
        if line and line.startswith(("Anthropic", "OpenAI", "Qwen"))
    ]
    assert "qwen-turbo" in data_lines[0]


# --- filters ------------------------------------------------------------


def test_models_provider_filter_narrows_results(priced_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["models", "--provider", "openai", "--color", "never"])
    assert result.exit_code == 0
    assert "OpenAI" in result.stdout
    assert "Anthropic" not in result.stdout
    assert "Qwen" not in result.stdout


def test_models_provider_filter_is_case_insensitive(priced_db: Path, runner: CliRunner) -> None:
    """`--provider OpenAI` (branded form) hits lowercase DB rows.
    Symmetric with `recommend --provider`."""
    result = runner.invoke(app, ["models", "--provider", "OpenAI", "--color", "never"])
    assert result.exit_code == 0
    assert "OpenAI" in result.stdout
    assert "Anthropic" not in result.stdout


def test_models_provider_flag_is_repeatable(priced_db: Path, runner: CliRunner) -> None:
    """`--provider openai --provider anthropic` shows both, not Qwen."""
    result = runner.invoke(
        app,
        ["models", "--provider", "openai", "--provider", "anthropic", "--color", "never"],
    )
    assert "OpenAI" in result.stdout
    assert "Anthropic" in result.stdout
    assert "Qwen" not in result.stdout


def test_models_match_substring_filter(priced_db: Path, runner: CliRunner) -> None:
    """`--match nano` returns only models whose name contains `nano`."""
    result = runner.invoke(app, ["models", "--match", "nano", "--color", "never"])
    assert "gpt-5-nano" in result.stdout
    assert "haiku" not in result.stdout
    assert "qwen-turbo" not in result.stdout


def test_models_no_match_renders_empty_hint(priced_db: Path, runner: CliRunner) -> None:
    """A filter that matches nothing should produce the same hint
    line as a missing DB — the user gets the same call-to-action."""
    result = runner.invoke(app, ["models", "--match", "does-not-exist", "--color", "never"])
    assert result.exit_code == 0
    assert "no priced models" in result.stdout


# --- --json output -------------------------------------------------------


def test_models_json_emits_get_pricing_result_shape(priced_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["models", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "models" in payload
    # JSON returns the raw catalog (NOT family-deduped).
    model_names = [m["model"] for m in payload["models"]]
    assert "gpt-5-nano" in model_names
    assert "gpt-5-nano-2025-08-07" in model_names


def test_models_json_no_ansi_even_with_color_always(priced_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["models", "--json", "--color", "always"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None
    json.loads(result.stdout)


def test_models_json_includes_cache_rate_fields(priced_db: Path, runner: CliRunner) -> None:
    """JSON always includes cache fields (it's the raw `GetPricingResult`
    shape) — `--cache` only affects the human view."""
    result = runner.invoke(app, ["models", "--provider", "anthropic", "--json"])
    payload = json.loads(result.stdout)
    haiku = next(m for m in payload["models"] if m["model"] == "claude-haiku-4-5")
    assert haiku["cache_read_per_million_usd"] == pytest.approx(0.1)
    assert haiku["cache_write_per_million_usd"] == 1.25


# --- --color parity ------------------------------------------------------


def test_models_color_never_suppresses_ansi(priced_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["models", "--color", "never"])
    assert _ANSI_RE.search(result.stdout) is None


def test_models_color_always_emits_ansi(priced_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["models", "--color", "always"])
    assert _ANSI_RE.search(result.stdout) is not None


def test_models_color_auto_disables_on_non_tty(priced_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["models"])
    assert _ANSI_RE.search(result.stdout) is None
