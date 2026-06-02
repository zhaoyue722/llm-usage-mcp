"""End-to-end tests for `llm-usage compare` via Typer's `CliRunner`.

The renderer is unit-tested in `test_cli_render.py`; the projection
math is covered in `test_compare_providers.py` via the shared
`core.compare.project_costs`. These tests assert the CLI shim itself:
flag parsing, JSON output, color-mode resolution (auto / always /
never + NO_COLOR + TTY).
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


# Three controlled models with round per-million rates so the CLI's
# rendered cost numbers are deterministic.
#   cheap   : $1/M in, $2/M out → 1*1 + 1*2 = 3e-6 USD for 1in/1out
#   mid     : $2/M in, $4/M out → 6e-6 USD (2x cheap)
#   premium : $5/M in, $10/M out → 15e-6 USD (5x cheap)
_PRICINGS = [
    Pricing(
        provider="deepseek",
        model="cheap-1",
        input_per_million_usd=1.0,
        output_per_million_usd=2.0,
        fetched_at=1_700_000_000_000,
    ),
    Pricing(
        provider="openai",
        model="mid-1",
        input_per_million_usd=2.0,
        output_per_million_usd=4.0,
        fetched_at=1_700_000_000_000,
    ),
    Pricing(
        provider="anthropic",
        model="premium-1",
        input_per_million_usd=5.0,
        output_per_million_usd=10.0,
        fetched_at=1_700_000_000_000,
    ),
]


@pytest.fixture
def priced_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(session, _PRICINGS)
        session.commit()
    return db


@pytest.fixture
def runner() -> CliRunner:
    # click 8.2+ always separates stdout / stderr — no `mix_stderr` kwarg.
    return CliRunner()


# --- happy path ------------------------------------------------------------


def test_compare_default_ranks_cheapest_first_in_text_output(
    priced_db: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["compare", "--in", "100", "--out", "100", "--color", "never"])
    assert result.exit_code == 0, result.stdout

    # Expected order: cheap-1 < mid-1 < premium-1.
    cheap_idx = result.stdout.index("cheap-1")
    mid_idx = result.stdout.index("mid-1")
    premium_idx = result.stdout.index("premium-1")
    assert cheap_idx < mid_idx < premium_idx


def test_compare_text_output_contains_header_divider_and_footnote(
    priced_db: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["compare", "--in", "100", "--out", "100", "--color", "never"])
    assert result.exit_code == 0
    assert "projecting cost for 100 in" in result.stdout
    assert "─" in result.stdout
    assert "note: cache pricing not applied" in result.stdout


# --- JSON output -----------------------------------------------------------


def test_compare_json_emits_compare_providers_result_shape(
    priced_db: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["compare", "--in", "100", "--out", "100", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    # Same Pydantic shape the MCP tool returns: `ranked` array of entries
    # with provider/model/cost_usd/relative_cost_pct/notes.
    assert "ranked" in payload
    assert len(payload["ranked"]) == 3
    first = payload["ranked"][0]
    assert set(first.keys()) == {
        "provider",
        "model",
        "cost_usd",
        "relative_cost_pct",
        "notes",
    }
    # Cheapest first.
    assert first["model"] == "cheap-1"
    assert first["relative_cost_pct"] == 100.0


def test_compare_json_output_has_no_ansi_escapes(priced_db: Path, runner: CliRunner) -> None:
    """`--json` is always machine-readable — `--color always` does not
    style it."""
    result = runner.invoke(
        app,
        ["compare", "--in", "100", "--out", "100", "--json", "--color", "always"],
    )
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None
    # And it parses.
    json.loads(result.stdout)


# --- --model filter -------------------------------------------------------


def test_compare_models_filter_restricts_to_named_subset(
    priced_db: Path, runner: CliRunner
) -> None:
    result = runner.invoke(
        app,
        [
            "compare",
            "--in",
            "100",
            "--out",
            "100",
            "--model",
            "cheap-1",
            "--model",
            "premium-1",
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    models = [row["model"] for row in payload["ranked"]]
    assert models == ["cheap-1", "premium-1"]


def test_compare_models_filter_with_no_match_returns_empty_ranked(
    priced_db: Path, runner: CliRunner
) -> None:
    result = runner.invoke(
        app,
        [
            "compare",
            "--in",
            "100",
            "--out",
            "100",
            "--model",
            "does-not-exist",
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ranked"] == []


# --- --color resolution ----------------------------------------------------


def test_compare_color_never_suppresses_ansi(priced_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["compare", "--in", "100", "--out", "100", "--color", "never"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None


def test_compare_color_always_emits_ansi_even_when_not_a_tty(
    priced_db: Path, runner: CliRunner
) -> None:
    """CliRunner's stdout is a StringIO, not a TTY. `--color always`
    must override the auto-detect."""
    result = runner.invoke(app, ["compare", "--in", "100", "--out", "100", "--color", "always"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is not None


def test_compare_color_auto_disables_when_stdout_is_not_a_tty(
    priced_db: Path, runner: CliRunner
) -> None:
    """The default `--color auto`: CliRunner's StringIO isn't a TTY,
    so no ANSI bytes appear."""
    result = runner.invoke(app, ["compare", "--in", "100", "--out", "100"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None


def test_compare_color_auto_disables_when_no_color_env_is_set(
    priced_db: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`NO_COLOR=1` overrides `--color auto` even if stdout looks like a
    TTY. Cross-tool standard (https://no-color.org/) so users can
    monochrome every CLI at once."""
    monkeypatch.setenv("NO_COLOR", "1")
    result = runner.invoke(app, ["compare", "--in", "100", "--out", "100", "--color", "auto"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None


# --- bar-width override ----------------------------------------------------


def test_compare_bar_width_override_changes_max_bar_cells(
    priced_db: Path, runner: CliRunner
) -> None:
    result = runner.invoke(
        app,
        [
            "compare",
            "--in",
            "100",
            "--out",
            "100",
            "--bar-width",
            "20",
            "--color",
            "never",
        ],
    )
    assert result.exit_code == 0
    # The premium row (max ratio) fills the entire bar — 20 cells.
    premium_line = next(line for line in result.stdout.split("\n") if "premium-1" in line)
    assert premium_line.count("▅") == 20


# --- input validation -----------------------------------------------------


def test_compare_missing_required_input_flag_errors_out(runner: CliRunner) -> None:
    result = runner.invoke(app, ["compare", "--out", "100"])
    assert result.exit_code != 0
    # Typer prints the error envelope to stderr.
    assert "--input" in result.stderr or "Missing option" in result.stderr


def test_compare_alias_in_works_as_input(priced_db: Path, runner: CliRunner) -> None:
    """`--in` is an alias for `--input` — matches the README's terse form."""
    result = runner.invoke(app, ["compare", "--in", "100", "--out", "100", "--color", "never"])
    assert result.exit_code == 0
    assert "cheap-1" in result.stdout
