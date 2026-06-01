"""End-to-end tests for `llm-usage status` via Typer's `CliRunner`.

`collect_status` is unit-tested in `test_diagnostics.py`; the human
renderer is unit-tested in `test_cli_render.py`. These tests cover
the CLI shim itself: argument parsing, JSON output, `--no-net` plumb-
through, color resolution parity with the other subcommands, and
the guarantee that `status` doesn't mutate any local state.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from llm_usage.bootstrap import migrate_to_head
from llm_usage.cli import app

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
    # Use a privileged port (1) for the proxy so the TCP probe always
    # resolves to "not running" — tests should never depend on a real
    # listener for the diagnostic.
    monkeypatch.setenv("LLM_USAGE_PROXY_PORT", "1")
    return db


# --- happy path ----------------------------------------------------------


def test_status_uninitialized_db_exits_zero(isolated_db: Path, runner: CliRunner) -> None:
    """A brand-new install with no DB should not be a failure mode."""
    result = runner.invoke(app, ["status", "--color", "never", "--no-net"])
    assert result.exit_code == 0, result.stdout


def test_status_uninitialized_db_renders_hint(isolated_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["status", "--color", "never", "--no-net"])
    assert "not initialized" in result.stdout


def test_status_uninitialized_db_does_not_create_file(isolated_db: Path, runner: CliRunner) -> None:
    """`status` is observational — running it must never bootstrap the DB.

    Regression: if `status` were to call `bootstrap()` (it shouldn't),
    the test DB file would exist after the command returns.
    """
    runner.invoke(app, ["status", "--color", "never", "--no-net"])
    assert not isolated_db.exists()


def test_status_migrated_db_renders_database_block(isolated_db: Path, runner: CliRunner) -> None:
    migrate_to_head()
    result = runner.invoke(app, ["status", "--color", "never", "--no-net"])
    assert result.exit_code == 0
    assert "Database" in result.stdout
    assert "head (rev" in result.stdout  # schema rev printed
    assert "none recorded yet" in result.stdout


# --- --no-net plumb-through ---------------------------------------------


def test_status_no_net_reports_proxy_unknown(isolated_db: Path, runner: CliRunner) -> None:
    """The CLI flag should plumb `check_proxy=False` through, surfacing
    as the `unknown (--no-net)` marker on the proxy `status` line."""
    result = runner.invoke(app, ["status", "--color", "never", "--no-net"])
    assert "unknown" in result.stdout
    assert "not running" not in result.stdout


def test_status_default_checks_proxy(isolated_db: Path, runner: CliRunner) -> None:
    """Without `--no-net`, the probe runs. We override the port to one
    that's guaranteed not to be listening so the result is deterministic."""
    result = runner.invoke(app, ["status", "--color", "never"])
    assert "not running" in result.stdout
    assert "uv run llm-usage proxy" in result.stdout  # start hint included


# --- --json output -------------------------------------------------------


def test_status_json_emits_full_status_report_shape(isolated_db: Path, runner: CliRunner) -> None:
    migrate_to_head()
    result = runner.invoke(app, ["status", "--json", "--no-net"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {"version", "database", "proxy", "providers", "pricing"}
    assert payload["database"] is not None
    assert payload["proxy"]["reachable"] is None  # --no-net
    assert isinstance(payload["providers"], list)
    assert len(payload["providers"]) == 4


def test_status_json_no_ansi_even_with_color_always(isolated_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["status", "--json", "--color", "always", "--no-net"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None
    json.loads(result.stdout)  # parses


def test_status_json_uninitialized_db_serializes_null_database(
    isolated_db: Path, runner: CliRunner
) -> None:
    """The "DB not initialized" case round-trips to `database: null`
    in JSON, so consumers can detect it programmatically."""
    result = runner.invoke(app, ["status", "--json", "--no-net"])
    payload = json.loads(result.stdout)
    assert payload["database"] is None
    assert payload["pricing"] is None


# --- --color parity ------------------------------------------------------


def test_status_color_never_suppresses_ansi(isolated_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["status", "--color", "never", "--no-net"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None


def test_status_color_always_emits_ansi(isolated_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["status", "--color", "always", "--no-net"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is not None


def test_status_color_auto_disables_on_non_tty(isolated_db: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["status", "--no-net"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None


def test_status_color_auto_disables_when_no_color_env_is_set(
    isolated_db: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    result = runner.invoke(app, ["status", "--color", "auto", "--no-net"])
    assert result.exit_code == 0
    assert _ANSI_RE.search(result.stdout) is None


# --- provider key reflection --------------------------------------------


def test_status_provider_row_reflects_env_key_state(
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

    result = runner.invoke(app, ["status", "--json", "--no-net"])
    payload = json.loads(result.stdout)
    by_name = {p["name"]: p for p in payload["providers"]}
    assert by_name["anthropic"]["key_set"] is True
    assert by_name["openai"]["key_set"] is False
    assert by_name["deepseek"]["key_set"] is False
    assert by_name["qwen"]["key_set"] is False
