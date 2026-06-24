"""Tests for `llm-usage about` and its metadata parsing.

`about` is the human-facing companion to `--version`: a watch-pom panel
showing version, author, license, and the project homepage. The fields
come from installed package metadata, so the parsing is split into pure
helpers (`_parse_about`, `_extract_homepage`) that are exercised here
against a synthetic `email.message.Message` — no reliance on what the
test environment happens to have installed.

The command-level tests assert structure and the fallback contract
rather than an exact author string, so they pass whether or not the
editable checkout's metadata is fresh.
"""

from __future__ import annotations

import json
import re
from email.message import Message
from importlib.metadata import PackageMetadata
from typing import cast

import pytest
from typer.testing import CliRunner

from llm_usage.cli import (
    _ABOUT_AUTHOR_FALLBACK,
    _ABOUT_HOMEPAGE_FALLBACK,
    _ABOUT_LICENSE_FALLBACK,
    _extract_homepage,
    _parse_about,
    app,
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _strip(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _md(**headers: str) -> PackageMetadata:
    """Build a metadata message; repeat `Project-URL=...` via `urls`.

    `email.message.Message` satisfies the `PackageMetadata` protocol's
    `get` / `get_all` surface, which is all the parsers touch.
    """
    msg = Message()
    for key, value in headers.items():
        if key == "urls":
            continue
        msg[key.replace("_", "-")] = value
    for entry in headers.get("urls", "").split("|"):
        if entry:
            msg["Project-URL"] = entry
    return cast(PackageMetadata, msg)


# --- _parse_about --------------------------------------------------------


def test_parse_about_pulls_all_fields() -> None:
    info = _parse_about(
        _md(
            Version="1.2.3",
            Author_email="Y.Zhao <zhaoyue722@gmail.com>",
            License_Expression="MIT",
            urls="Homepage, https://example.com/repo|Issues, https://example.com/issues",
        )
    )
    assert info.version == "1.2.3"
    assert info.author == "Y.Zhao"  # email address stripped off
    assert info.license == "MIT"
    assert info.homepage == "https://example.com/repo"


def test_parse_about_falls_back_per_field_when_missing() -> None:
    """An empty metadata object yields fallbacks, never blank cells."""
    info = _parse_about(_md())
    assert info.version == "unknown"
    assert info.author == _ABOUT_AUTHOR_FALLBACK
    assert info.license == _ABOUT_LICENSE_FALLBACK
    assert info.homepage == _ABOUT_HOMEPAGE_FALLBACK


def test_parse_about_uses_plain_author_header_when_no_email() -> None:
    info = _parse_about(_md(Author="Jane Doe"))
    assert info.author == "Jane Doe"


def test_parse_about_prefers_license_expression_over_license() -> None:
    info = _parse_about(_md(License_Expression="Apache-2.0", License="ignored free text"))
    assert info.license == "Apache-2.0"


# --- _extract_homepage ---------------------------------------------------


def test_extract_homepage_matches_label_case_insensitively() -> None:
    md = _md(urls="homepage, https://example.com/x")
    assert _extract_homepage(md) == "https://example.com/x"


def test_extract_homepage_ignores_other_labels() -> None:
    md = _md(urls="Repository, https://example.com/repo|Issues, https://example.com/issues")
    assert _extract_homepage(md) == _ABOUT_HOMEPAGE_FALLBACK


# --- the command ---------------------------------------------------------


def test_about_shows_the_four_fields(runner: CliRunner) -> None:
    """The default panel labels every field and exits 0."""
    result = runner.invoke(app, ["about", "--color", "never"])
    assert result.exit_code == 0, result.stdout
    plain = _strip(result.stdout)
    for label in ("author", "license", "homepage"):
        assert label in plain
    # The watch-pom is present, tying the panel to the proxy/MCP banners.
    assert "o-''" in plain
    # Homepage and license carry their real values (fallbacks at worst).
    assert "github.com" in plain
    assert _ABOUT_LICENSE_FALLBACK in plain


def test_about_json_is_machine_readable(runner: CliRunner) -> None:
    result = runner.invoke(app, ["about", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert set(payload) == {"version", "author", "license", "homepage"}
    assert payload["homepage"].startswith("http")


def test_about_json_emits_no_ansi(runner: CliRunner) -> None:
    """`--json` must stay pipe-clean even with `--color always`."""
    result = runner.invoke(app, ["about", "--json", "--color", "always"])
    assert "\x1b[" not in result.stdout


def test_about_listed_in_help(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert "about" in _strip(result.stdout)
