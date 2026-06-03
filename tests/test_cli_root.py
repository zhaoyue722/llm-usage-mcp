"""Tests for the `llm-usage` Typer app's root-level behavior.

Covers the top-level surface that doesn't belong to any subcommand:
- `--version` / `-V` reports the package version and exits cleanly.
- The bare `llm-usage` invocation still shows `--help` (regression
  guard for `no_args_is_help=True`).
- Typer's completion flags are wired up (`--install-completion` and
  `--show-completion` ship when `add_completion=True`).
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from llm_usage.cli import app

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _strip(text: str) -> str:
    return _ANSI_RE.sub("", text)


# --- --version / -V ------------------------------------------------------


def test_version_long_flag_prints_version_and_exits_zero(runner: CliRunner) -> None:
    """`llm-usage --version` writes a single `llm-usage X.Y.Z` line to
    stdout and exits 0 — the standard CLI version convention."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.startswith("llm-usage ")
    # Single line of output.
    assert result.stdout.count("\n") == 1


def test_version_short_flag_works(runner: CliRunner) -> None:
    """`-V` is an alias for `--version` (matches `python -V`, `git -V` style)."""
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.startswith("llm-usage ")


def test_version_short_circuits_before_subcommand(runner: CliRunner) -> None:
    """`is_eager=True` on the `--version` flag means it runs before
    Typer tries to parse a subcommand — so `llm-usage --version
    compare ...` still just prints the version and exits, without
    requiring valid `compare` args."""
    result = runner.invoke(app, ["--version", "compare"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.startswith("llm-usage ")


def test_version_value_agrees_with_diagnostics_helper(runner: CliRunner) -> None:
    """`--version` and `core.diagnostics._package_version()` must
    agree — same fallback logic so the CLI and the `status` command
    don't drift."""
    from llm_usage.core.diagnostics import _package_version

    result = runner.invoke(app, ["--version"])
    expected = f"llm-usage {_package_version()}"
    assert result.stdout.strip() == expected


# --- no-args still shows help -------------------------------------------


def test_bare_invocation_still_shows_help(runner: CliRunner) -> None:
    """Adding the root callback for `--version` must not break the
    `no_args_is_help=True` behavior. `llm-usage` with no args should
    still print the usage / help block, not silently succeed."""
    result = runner.invoke(app, [])
    plain = _strip(result.stdout)
    # Typer's `no_args_is_help` exits 2 (usage error) on no-args. Both
    # 0 and 2 are acceptable behavior from a user's perspective — the
    # important thing is that help text reaches the user.
    assert result.exit_code in (0, 2)
    assert "Usage:" in plain or "usage:" in plain
    # Each registered subcommand should be listed.
    for name in ("proxy", "compare", "models", "recommend", "spend", "status", "providers"):
        assert name in plain


# --- shell completion ---------------------------------------------------


def test_install_completion_flag_is_registered(runner: CliRunner) -> None:
    """`add_completion=True` injects `--install-completion`. Pinning
    this confirms the flag is wired up without actually running the
    install (which would modify the user's shell config)."""
    result = runner.invoke(app, ["--help"])
    plain = _strip(result.stdout)
    assert "--install-completion" in plain


def test_show_completion_flag_is_registered(runner: CliRunner) -> None:
    """`--show-completion` (the install-completion sibling) is also
    expected when `add_completion=True`."""
    result = runner.invoke(app, ["--help"])
    plain = _strip(result.stdout)
    assert "--show-completion" in plain
