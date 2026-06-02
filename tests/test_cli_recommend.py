"""End-to-end tests for `llm-usage recommend` via Typer's `CliRunner`.

The core function is unit-tested in `test_recommend.py`; the renderer
is unit-tested in `test_cli_render.py`. These tests cover the CLI shim
itself: argument parsing, `--json` output, error handling for the
empty-pricing case, color resolution, and the guarantee that the CLI
passes its own flag names into the reasoning template.
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

_PRICINGS = [
    Pricing("deepseek", "cheap-1", 1.0, 2.0, fetched_at=1),
    Pricing("openai", "mid-1", 2.0, 4.0, fetched_at=1),
    Pricing("anthropic", "premium-1", 5.0, 10.0, fetched_at=1),
]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


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
def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    return db


# --- happy path ----------------------------------------------------------


def test_recommend_happy_path_picks_cheapest(priced_db: Path, runner: CliRunner) -> None:
    """1M/1M workload over the controlled fixture → cheap-1 at $3.00."""
    result = runner.invoke(
        app,
        [
            "recommend",
            "--task",
            "summarize a transcript",
            "--in",
            "1000000",
            "--out",
            "1000000",
            "--color",
            "never",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "Recommendation" in result.stdout
    assert "Reasoning" in result.stdout
    # The branded display name is what the user sees in the chosen row.
    assert "DeepSeek / cheap-1" in result.stdout
    assert "$3.0000" in result.stdout


def test_recommend_short_flags_work(priced_db: Path, runner: CliRunner) -> None:
    """`-t`, `-i`, `-o`, `-b` all parse."""
    result = runner.invoke(
        app,
        [
            "recommend",
            "-t",
            "anything",
            "-i",
            "1000000",
            "-o",
            "1000000",
            "-b",
            "5.0",
            "--color",
            "never",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "DeepSeek / cheap-1" in result.stdout


def test_recommend_requires_task_flag(priced_db: Path, runner: CliRunner) -> None:
    """`--task` is mandatory — without it, Typer should exit non-zero
    and surface the missing argument in stderr (rich error output)."""
    result = runner.invoke(app, ["recommend"])
    assert result.exit_code != 0
    plain = _ANSI_RE.sub("", result.stdout + result.output)
    # Rich may wrap the flag name; check for the unambiguous substring.
    assert "task" in plain.lower()


# --- defaults plumbed through -------------------------------------------


def test_recommend_omitting_tokens_uses_nominal_defaults(
    priced_db: Path, runner: CliRunner
) -> None:
    """Without `--in` / `--out`, the reasoning should flag the
    nominal defaults — and the advice phrase should reference the
    CLI's *flag* names, not the MCP tool's parameter names."""
    result = runner.invoke(app, ["recommend", "-t", "anything", "--color", "never"])
    assert result.exit_code == 0
    assert "nominal defaults" in result.stdout
    assert "--in" in result.stdout
    assert "--out" in result.stdout
    # Anti-regression: the MCP-flavor reasoning hint should NOT leak
    # into CLI output.
    assert "expected_input_tokens" not in result.stdout


def test_recommend_supplied_tokens_drop_the_default_note(
    priced_db: Path, runner: CliRunner
) -> None:
    result = runner.invoke(
        app,
        [
            "recommend",
            "-t",
            "anything",
            "--in",
            "8000",
            "--out",
            "2000",
            "--color",
            "never",
        ],
    )
    assert result.exit_code == 0
    assert "nominal defaults" not in result.stdout


# --- budget -------------------------------------------------------------


def test_recommend_over_budget_falls_back_with_explanation(
    priced_db: Path, runner: CliRunner
) -> None:
    """`-b 0.01` fits nothing at 1M/1M — fall back to cheap-1 with the
    over-budget message."""
    result = runner.invoke(
        app,
        [
            "recommend",
            "-t",
            "anything",
            "-i",
            "1000000",
            "-o",
            "1000000",
            "-b",
            "0.01",
            "--color",
            "never",
        ],
    )
    assert result.exit_code == 0
    assert "no priced model fits" in result.stdout
    assert "$0.0100 budget" in result.stdout
    assert "DeepSeek / cheap-1" in result.stdout


# --- empty pricing-table error path -------------------------------------


def test_recommend_empty_pricing_exits_non_zero(empty_db: Path, runner: CliRunner) -> None:
    """An un-seeded DB makes the recommendation impossible; the CLI
    should surface this as a clean error rather than a stack trace."""
    result = runner.invoke(app, ["recommend", "-t", "anything", "--color", "never"])
    assert result.exit_code != 0
    plain = _ANSI_RE.sub("", result.stdout + result.output)
    assert "no priced models" in plain


# --- --json output -------------------------------------------------------


def test_recommend_json_emits_full_result_shape(priced_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        [
            "recommend",
            "-t",
            "anything",
            "-i",
            "1000000",
            "-o",
            "1000000",
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {
        "provider",
        "model",
        "estimated_cost_usd",
        "alternatives",
        "reasoning",
    }
    assert payload["provider"] == "deepseek"
    assert payload["model"] == "cheap-1"
    assert payload["estimated_cost_usd"] == 3.0
    # The two runner-ups in the controlled fixture, cost-ascending.
    assert [(a["provider"], a["model"]) for a in payload["alternatives"]] == [
        ("openai", "mid-1"),
        ("anthropic", "premium-1"),
    ]


def test_recommend_json_no_ansi_even_with_color_always(priced_db: Path, runner: CliRunner) -> None:
    """`--json` should never emit ANSI escapes, regardless of `--color`."""
    result = runner.invoke(app, ["recommend", "-t", "anything", "--json", "--color", "always"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None
    json.loads(result.stdout)  # parses


# --- --color parity ------------------------------------------------------


def test_recommend_color_never_suppresses_ansi(priced_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["recommend", "-t", "anything", "--color", "never"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None


def test_recommend_color_always_emits_ansi(priced_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["recommend", "-t", "anything", "--color", "always"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is not None


def test_recommend_color_auto_disables_on_non_tty(priced_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["recommend", "-t", "anything"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None


# --- --provider / --model filters ---------------------------------------


def test_recommend_provider_filter_changes_winner(priced_db: Path, runner: CliRunner) -> None:
    """Without filters cheap-1 (deepseek) wins. `--provider openai`
    should pivot to mid-1 — exercises the filter path from CLI to core."""
    result = runner.invoke(
        app,
        [
            "recommend",
            "-t",
            "anything",
            "-i",
            "1000000",
            "-o",
            "1000000",
            "--provider",
            "openai",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["provider"] == "openai"
    assert payload["model"] == "mid-1"


def test_recommend_provider_flag_is_repeatable(priced_db: Path, runner: CliRunner) -> None:
    """`--provider openai --provider deepseek` should keep both pools'
    candidates; cheap-1 (deepseek) is still cheapest of the two."""
    result = runner.invoke(
        app,
        [
            "recommend",
            "-t",
            "anything",
            "-i",
            "1000000",
            "-o",
            "1000000",
            "--provider",
            "openai",
            "--provider",
            "deepseek",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert (payload["provider"], payload["model"]) == ("deepseek", "cheap-1")


def test_recommend_model_filter_changes_winner(priced_db: Path, runner: CliRunner) -> None:
    """`--model mid-1 --model premium-1` excludes cheap-1; mid-1 wins."""
    result = runner.invoke(
        app,
        [
            "recommend",
            "-t",
            "anything",
            "-i",
            "1000000",
            "-o",
            "1000000",
            "--model",
            "mid-1",
            "--model",
            "premium-1",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert (payload["provider"], payload["model"]) == ("openai", "mid-1")


def test_recommend_provider_and_model_filters_and_combine(
    priced_db: Path, runner: CliRunner
) -> None:
    """Both filters AND-combine. `--provider openai --model mid-1
    --model cheap-1` should pick openai/mid-1 (cheap-1 is deepseek,
    filtered out by --provider)."""
    result = runner.invoke(
        app,
        [
            "recommend",
            "-t",
            "anything",
            "-i",
            "1000000",
            "-o",
            "1000000",
            "--provider",
            "openai",
            "--model",
            "mid-1",
            "--model",
            "cheap-1",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert (payload["provider"], payload["model"]) == ("openai", "mid-1")


def _normalize(text: str) -> str:
    """Strip ANSI escapes + box borders and collapse whitespace.

    Rich word-wraps long messages inside its `╭─ Error ─╮` box, so a
    multi-word phrase like "no priced models match the recommend
    filter" can be split across two visible lines, each prefixed and
    suffixed with `│`. After stripping ANSI, we also drop the box
    border glyphs so the substring "recommend filter" doesn't get
    interrupted by `│ │` when the wrap happens to land between the
    two words.
    """
    plain = _ANSI_RE.sub("", text)
    # Box-drawing glyphs Rich uses for the error frame: ╭ ╮ ╯ ╰ ─ │
    for glyph in "╭╮╯╰─│":
        plain = plain.replace(glyph, " ")
    return " ".join(plain.split())


def test_recommend_filter_with_no_match_exits_non_zero_with_filter_hint(
    priced_db: Path, runner: CliRunner
) -> None:
    """A whitelist that matches nothing should fail cleanly, nudging
    the user at `--provider/--model` rather than `--task` so a typo
    is obvious."""
    result = runner.invoke(
        app,
        [
            "recommend",
            "-t",
            "anything",
            "--provider",
            "no-such-provider",
            "--color",
            "never",
        ],
    )
    assert result.exit_code != 0
    plain = _normalize(result.stdout + result.output)
    assert "no priced models match the recommend filter" in plain
    # The param hint should point at the filter flags, not --task.
    assert "--provider" in plain or "--model" in plain


def test_recommend_filter_error_echoes_user_filter_values(
    priced_db: Path, runner: CliRunner
) -> None:
    """The error should print the failing filter contents so the user
    can spot a typo without re-reading their command."""
    result = runner.invoke(
        app,
        [
            "recommend",
            "-t",
            "anything",
            "--model",
            "typo-model-name",
            "--color",
            "never",
        ],
    )
    assert result.exit_code != 0
    plain = _normalize(result.stdout + result.output)
    assert "typo-model-name" in plain


# --- alternatives in the human view -------------------------------------


def test_recommend_human_output_shows_alternatives_block(
    priced_db: Path, runner: CliRunner
) -> None:
    """Default CLI run should show the chosen row plus the two
    runner-ups as a block — the new user-facing behavior."""
    result = runner.invoke(
        app,
        [
            "recommend",
            "-t",
            "anything",
            "-i",
            "1000000",
            "-o",
            "1000000",
            "--color",
            "never",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "Alternatives" in result.stdout
    # The two non-chosen models in the controlled fixture appear in
    # the Alternatives block.
    assert "OpenAI / mid-1" in result.stdout
    assert "Anthropic / premium-1" in result.stdout


def test_recommend_human_output_omits_alternatives_when_pool_is_one(
    priced_db: Path, runner: CliRunner
) -> None:
    """When `--model` narrows to a single match, the alternatives
    section is skipped — no empty `Alternatives` header."""
    result = runner.invoke(
        app,
        [
            "recommend",
            "-t",
            "anything",
            "-i",
            "1000000",
            "-o",
            "1000000",
            "--model",
            "cheap-1",
            "--color",
            "never",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "Alternatives" not in result.stdout
    assert "Reasoning" in result.stdout  # other sections still render
