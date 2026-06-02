"""End-to-end tests for `llm-usage providers` via Typer's `CliRunner`.

`collect_providers` is unit-tested in `test_providers.py`; the human
renderer is unit-tested in `test_cli_render.py`. These tests cover the
CLI shim itself: argument parsing, `--json` output, `--models` plumb-
through, color resolution parity with the other subcommands, and the
guarantee that `providers` doesn't mutate any local state.
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
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point Settings at a tmp DB path that's *not* pre-created.

    Tests opt into a migrated DB by calling `migrate_to_head()` in the
    test body when they need one. The "not initialized" path is the
    default so we exercise the most common first-run state.
    """
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    return db


# --- happy path ----------------------------------------------------------


def test_providers_uninitialized_db_exits_zero(isolated_db: Path, runner: CliRunner) -> None:
    """A brand-new install with no DB should not be a failure mode."""
    result = runner.invoke(app, ["providers", "--color", "never"])
    assert result.exit_code == 0, result.stdout


def test_providers_uninitialized_db_does_not_create_file(
    isolated_db: Path, runner: CliRunner
) -> None:
    """`providers` is observational — running it must not bootstrap the DB.

    Regression: if `providers` were to call `bootstrap()` (it shouldn't),
    the test DB file would exist after the command returns.
    """
    runner.invoke(app, ["providers", "--color", "never"])
    assert not isolated_db.exists()


def test_providers_uninitialized_db_renders_every_known_provider(
    isolated_db: Path, runner: CliRunner
) -> None:
    """Even with no DB, every `KNOWN_PROVIDERS` row should appear."""
    result = runner.invoke(app, ["providers", "--color", "never"])
    assert result.exit_code == 0
    for name in ("Anthropic", "OpenAI", "DeepSeek", "Qwen"):
        assert name in result.stdout


def test_providers_migrated_db_with_pricing_reports_model_count(
    isolated_db: Path, runner: CliRunner
) -> None:
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(
            session,
            [
                Pricing(
                    provider="anthropic",
                    model="claude-opus-4-7",
                    input_per_million_usd=15.0,
                    output_per_million_usd=75.0,
                    fetched_at=1,
                ),
                Pricing(
                    provider="anthropic",
                    model="claude-sonnet-4-6",
                    input_per_million_usd=3.0,
                    output_per_million_usd=15.0,
                    fetched_at=1,
                ),
            ],
        )
        session.commit()

    result = runner.invoke(app, ["providers", "--color", "never"])
    assert result.exit_code == 0
    anthro_line = next(line for line in result.stdout.split("\n") if "Anthropic" in line)
    assert "2 models priced" in anthro_line


# --- --models plumb-through ----------------------------------------------


def test_providers_models_flag_expands_model_list(isolated_db: Path, runner: CliRunner) -> None:
    """`--models` should print one indented line per priced model."""
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(
            session,
            [
                Pricing(
                    provider="anthropic",
                    model="claude-opus-4-7",
                    input_per_million_usd=15.0,
                    output_per_million_usd=75.0,
                    fetched_at=1,
                ),
            ],
        )
        session.commit()

    result = runner.invoke(app, ["providers", "--models", "--color", "never"])
    assert result.exit_code == 0
    assert "claude-opus-4-7" in result.stdout


def test_providers_default_omits_model_list(isolated_db: Path, runner: CliRunner) -> None:
    """Without `--models`, the per-provider model names should not appear."""
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(
            session,
            [
                Pricing(
                    provider="anthropic",
                    model="claude-opus-4-7",
                    input_per_million_usd=15.0,
                    output_per_million_usd=75.0,
                    fetched_at=1,
                ),
            ],
        )
        session.commit()

    result = runner.invoke(app, ["providers", "--color", "never"])
    assert result.exit_code == 0
    assert "claude-opus-4-7" not in result.stdout


def test_providers_short_models_flag_works(isolated_db: Path, runner: CliRunner) -> None:
    """`-m` is an alias for `--models`."""
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(
            session,
            [
                Pricing(
                    provider="openai",
                    model="gpt-4o",
                    input_per_million_usd=2.5,
                    output_per_million_usd=10.0,
                    fetched_at=1,
                ),
            ],
        )
        session.commit()

    result = runner.invoke(app, ["providers", "-m", "--color", "never"])
    assert result.exit_code == 0
    assert "gpt-4o" in result.stdout


# --- --json output -------------------------------------------------------


def test_providers_json_emits_full_providers_report_shape(
    isolated_db: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["providers", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {"providers"}
    assert len(payload["providers"]) == 4
    for row in payload["providers"]:
        assert set(row.keys()) == {
            "name",
            "display_name",
            "openai_compatible",
            "key_set",
            "base_url",
            "models",
        }


def test_providers_json_no_ansi_even_with_color_always(
    isolated_db: Path, runner: CliRunner
) -> None:
    """`--json` should never emit ANSI, regardless of `--color`."""
    result = runner.invoke(app, ["providers", "--json", "--color", "always"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None
    json.loads(result.stdout)  # parses


def test_providers_json_includes_models_list_for_seeded_provider(
    isolated_db: Path, runner: CliRunner
) -> None:
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(
            session,
            [
                Pricing(
                    provider="qwen",
                    model="qwen-max",
                    input_per_million_usd=1.0,
                    output_per_million_usd=2.0,
                    fetched_at=1,
                ),
            ],
        )
        session.commit()

    result = runner.invoke(app, ["providers", "--json"])
    payload = json.loads(result.stdout)
    by_name = {p["name"]: p for p in payload["providers"]}
    assert by_name["qwen"]["models"] == ["qwen-max"]
    # Unseeded providers in the same JSON should report an empty list,
    # *not* be missing — different from the MCP `list_providers` tool.
    assert by_name["openai"]["models"] == []


# --- --color parity ------------------------------------------------------


def test_providers_color_never_suppresses_ansi(isolated_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["providers", "--color", "never"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None


def test_providers_color_always_emits_ansi(isolated_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["providers", "--color", "always"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is not None


def test_providers_color_auto_disables_on_non_tty(isolated_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["providers"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None


def test_providers_color_auto_disables_when_no_color_env_is_set(
    isolated_db: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    result = runner.invoke(app, ["providers", "--color", "auto"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None


# --- provider key reflection --------------------------------------------


def test_providers_row_reflects_env_key_state(
    isolated_db: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting one key should flip just that provider's status to
    `key set` while others remain `key missing`."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-only")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    result = runner.invoke(app, ["providers", "--json"])
    payload = json.loads(result.stdout)
    by_name = {p["name"]: p for p in payload["providers"]}
    assert by_name["anthropic"]["key_set"] is True
    assert by_name["openai"]["key_set"] is False
    assert by_name["deepseek"]["key_set"] is False
    assert by_name["qwen"]["key_set"] is False
