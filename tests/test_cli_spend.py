"""End-to-end tests for `llm-usage spend` via Typer's `CliRunner`.

The renderer is unit-tested in `test_cli_render.py`; the aggregate
SQL is covered in `test_query_spend.py` and `test_usage_summary.py`.
These tests assert the CLI shim itself: flag parsing, mode switching
between `usage_summary` and `query_spend` shapes, JSON output, the
filters-require-group-by guard, and the `--color` resolution paths
shared with `compare`.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
from sqlalchemy import update
from typer.testing import CliRunner

from llm_usage.bootstrap import migrate_to_head
from llm_usage.cli import app
from llm_usage.core.db.models import UsageEvent
from llm_usage.core.db.session import get_session
from llm_usage.core.pricing import Pricing, upsert_pricing
from llm_usage.core.recording import record_event

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# Controlled pricing so the rendered cost numbers are deterministic.
#   anthropic/sonnet:  $3/M in,  $15/M out  (the "expensive" one)
#   openai/gpt-mini:   $1/M in,  $2/M out
#   deepseek/v3:       $0.5/M in, $1/M out  (the "cheap" one)
_PRICINGS = [
    Pricing(
        provider="anthropic",
        model="sonnet",
        input_per_million_usd=3.0,
        output_per_million_usd=15.0,
        fetched_at=1_700_000_000_000,
    ),
    Pricing(
        provider="openai",
        model="gpt-mini",
        input_per_million_usd=1.0,
        output_per_million_usd=2.0,
        fetched_at=1_700_000_000_000,
    ),
    Pricing(
        provider="deepseek",
        model="v3",
        input_per_million_usd=0.5,
        output_per_million_usd=1.0,
        fetched_at=1_700_000_000_000,
    ),
]


@pytest.fixture
def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """DB with schema + pricing rows but no usage events."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(session, _PRICINGS)
        session.commit()
    return db


@pytest.fixture
def seeded_db(empty_db: Path) -> Path:
    """Seed 5 events spanning the last 2 days so window queries have data.

    Backdating happens in a second pass because `record_event` stamps
    timestamps from `time.time()` on insert; we let it run, then UPDATE
    the timestamp by `request_id` to land each row in the right window.
    """
    now_ms = int(time.time() * 1000)
    hour_ms = 3600 * 1000

    # (provider, model, in, out, hours_ago, project)
    rows = [
        # Anthropic dominates spend — 10k+2k at $3/$15 = $0.060 each.
        ("anthropic", "sonnet", 10_000, 2_000, 1, "demo"),
        ("anthropic", "sonnet", 8_000, 1_500, 6, "demo"),
        # OpenAI: small share.
        ("openai", "gpt-mini", 3_000, 500, 2, "demo"),
        # DeepSeek: tiny share.
        ("deepseek", "v3", 4_000, 600, 3, "demo"),
        # A failed event from yesterday — should be excluded by default.
        ("deepseek", "v3", 5_000, 0, 25, "demo"),
    ]

    with get_session() as session:
        for i, (prov, model, in_t, out_t, _hrs, project) in enumerate(rows):
            record_event(
                session,
                provider=prov,
                model=model,
                input_tokens=in_t,
                output_tokens=out_t,
                duration_ms=100,
                success=(i != 4),  # last row is the failure
                error_type=None if i != 4 else "stream_interrupted",
                request_id=f"seed-{i}",
                project=project,
            )
        session.commit()
        # Backdate each row by its hours_ago value.
        for i, (_, _, _, _, hrs, _) in enumerate(rows):
            session.execute(
                update(UsageEvent)
                .where(UsageEvent.request_id == f"seed-{i}")
                .values(timestamp=now_ms - hrs * hour_ms)
            )
        session.commit()
    return empty_db


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# --- empty window ---------------------------------------------------------


def test_spend_empty_db_renders_empty_state(empty_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["spend", "--period", "week", "--color", "never"])
    assert result.exit_code == 0, result.stdout
    assert "no calls recorded" in result.stdout
    # The empty state still shows the period header.
    assert "spend this week" in result.stdout


def test_spend_empty_db_group_by_renders_empty_state(empty_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        app, ["spend", "--period", "week", "--group-by", "provider", "--color", "never"]
    )
    assert result.exit_code == 0
    assert "no calls recorded" in result.stdout
    assert "by provider" in result.stdout


# --- default summary view -------------------------------------------------


def test_spend_summary_shows_total_top_providers_top_models_and_largest(
    seeded_db: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["spend", "--period", "year", "--color", "never"])
    assert result.exit_code == 0, result.stdout
    # 4 success rows: 2 anthropic + 1 openai + 1 deepseek.
    assert "across 4 calls" in result.stdout
    assert "top providers:" in result.stdout
    assert "Anthropic" in result.stdout  # branded display name
    assert "top models:" in result.stdout
    assert "sonnet" in result.stdout
    assert "largest call:" in result.stdout


def test_spend_summary_excludes_failed_rows_by_default(seeded_db: Path, runner: CliRunner) -> None:
    """The seeded data has one failure row from yesterday (deepseek)."""
    result = runner.invoke(app, ["spend", "--period", "year", "--color", "never"])
    assert result.exit_code == 0
    # 4, not 5 — the failure row is gone.
    assert "across 4 calls" in result.stdout
    assert "across 5 calls" not in result.stdout


def test_spend_summary_include_failed_folds_failure_back_in(
    seeded_db: Path, runner: CliRunner
) -> None:
    result = runner.invoke(
        app, ["spend", "--period", "year", "--include-failed", "--color", "never"]
    )
    assert result.exit_code == 0
    assert "across 5 calls" in result.stdout


# --- group-by view --------------------------------------------------------


def test_spend_group_by_provider_orders_cost_descending(seeded_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        ["spend", "--period", "year", "--group-by", "provider", "--color", "never"],
    )
    assert result.exit_code == 0
    # Anthropic is the biggest spender; should appear before OpenAI/DeepSeek.
    a_idx = result.stdout.index("Anthropic")
    o_idx = result.stdout.index("OpenAI")
    d_idx = result.stdout.index("DeepSeek")
    assert a_idx < o_idx < d_idx


def test_spend_group_by_model_uses_model_keys(seeded_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        app, ["spend", "--period", "year", "--group-by", "model", "--color", "never"]
    )
    assert result.exit_code == 0
    assert "by model" in result.stdout
    assert "sonnet" in result.stdout
    assert "gpt-mini" in result.stdout


def test_spend_group_by_day_keys_are_iso_dates(seeded_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        app, ["spend", "--period", "year", "--group-by", "day", "--color", "never"]
    )
    assert result.exit_code == 0
    assert "by day" in result.stdout
    # Some YYYY-MM-DD key appears on a data row.
    assert re.search(r"\d{4}-\d{2}-\d{2}", result.stdout) is not None


# --- filters --------------------------------------------------------------


def test_spend_filters_without_group_by_errors_out(seeded_db: Path, runner: CliRunner) -> None:
    """`--provider X` makes no sense in the headline summary view."""
    result = runner.invoke(app, ["spend", "--provider", "anthropic", "--color", "never"])
    assert result.exit_code != 0
    assert "--group-by" in result.stderr or "--group-by" in result.stdout


def test_spend_filters_with_group_by_apply(seeded_db: Path, runner: CliRunner) -> None:
    """Filtering to one provider should leave only that provider's rows."""
    result = runner.invoke(
        app,
        [
            "spend",
            "--period",
            "year",
            "--group-by",
            "model",
            "--provider",
            "anthropic",
            "--color",
            "never",
        ],
    )
    assert result.exit_code == 0
    assert "sonnet" in result.stdout
    assert "gpt-mini" not in result.stdout


# --- JSON output ---------------------------------------------------------


def test_spend_json_default_returns_usage_summary_shape(seeded_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["spend", "--period", "year", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    # `UsageSummaryResult` fields.
    assert set(payload.keys()) == {
        "period",
        "total_cost_usd",
        "call_count",
        "top_providers",
        "top_models",
        "largest_call",
    }
    assert payload["call_count"] == 4


def test_spend_json_group_by_returns_query_spend_shape(seeded_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["spend", "--period", "year", "--group-by", "provider", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    # `QuerySpendResult` fields.
    assert set(payload.keys()) == {
        "total_cost_usd",
        "total_calls",
        "total_input_tokens",
        "total_output_tokens",
        "groups",
    }
    assert len(payload["groups"]) == 3
    # Cheapest-first invariant — verifies the MCP-vs-CLI parity.
    assert payload["groups"][0]["key"] == "anthropic"


def test_spend_json_output_has_no_ansi_even_with_color_always(
    seeded_db: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["spend", "--period", "year", "--json", "--color", "always"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None
    json.loads(result.stdout)


# --- --color resolution (parity with `compare`) --------------------------


def test_spend_color_never_suppresses_ansi(seeded_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["spend", "--period", "year", "--color", "never"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None


def test_spend_color_always_emits_ansi(seeded_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["spend", "--period", "year", "--color", "always"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is not None


def test_spend_color_auto_disables_on_non_tty(seeded_db: Path, runner: CliRunner) -> None:
    """CliRunner's StringIO isn't a TTY; auto resolution → no color."""
    result = runner.invoke(app, ["spend", "--period", "year"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None


def test_spend_color_auto_disables_when_no_color_env_is_set(
    seeded_db: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    result = runner.invoke(app, ["spend", "--period", "year", "--color", "auto"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None
