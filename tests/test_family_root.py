"""Unit tests for `core.pricing.family_root`.

The helper is a pure string transform — no DB, no session. Pins the
patterns covered (dated suffix variants + `-latest`) and the
back-compat behavior on names that don't match any pattern.

Used by `core/compare` (default dedup) and `core/recommend`
(alternatives dedup, tie counting), so a regression here would
silently break both surfaces' family-aware behavior.
"""

from __future__ import annotations

import pytest

from llm_usage.core.pricing import family_root


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # YYYY-MM-DD suffix (dashed form) — the OpenAI / Anthropic convention.
        ("gpt-5-mini-2025-08-07", "gpt-5-mini"),
        ("gpt-5-nano-2025-08-07", "gpt-5-nano"),
        ("gpt-4.1-nano-2025-04-14", "gpt-4.1-nano"),
        ("claude-sonnet-4-5-2025-09-29", "claude-sonnet-4-5"),
        # YYYYMMDD suffix (compact form) — also Anthropic.
        ("claude-opus-4-7-20260416", "claude-opus-4-7"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        # `-latest` alias.
        ("qwen-turbo-latest", "qwen-turbo"),
        # No suffix — already a canonical name; returned unchanged.
        ("qwen-turbo", "qwen-turbo"),
        ("deepseek-coder", "deepseek-coder"),
        ("deepseek-v4-flash", "deepseek-v4-flash"),
        ("claude-sonnet-4-6", "claude-sonnet-4-6"),
        # Empty input — degenerate but shouldn't crash.
        ("", ""),
    ],
)
def test_family_root_strips_known_suffixes(name: str, expected: str) -> None:
    assert family_root(name) == expected


def test_family_root_is_idempotent() -> None:
    """Applying `family_root` to its own output is a no-op."""
    samples = [
        "gpt-5-mini",
        "gpt-5-mini-2025-08-07",
        "claude-opus-4-7-20260416",
        "qwen-turbo-latest",
        "deepseek-coder",
    ]
    for name in samples:
        once = family_root(name)
        twice = family_root(once)
        assert once == twice, f"non-idempotent on {name!r}: {once!r} -> {twice!r}"


def test_family_root_does_not_strip_non_terminal_dates() -> None:
    """A date-like segment in the middle of the name shouldn't trigger
    the strip — only end-anchored matches collapse. (No real model
    name does this today, but pins the regex anchoring.)"""
    # Hypothetical: `gpt-2025-08-07-mini`. The date is in the middle,
    # not at the end, so family_root should return it unchanged.
    assert family_root("gpt-2025-08-07-mini") == "gpt-2025-08-07-mini"


def test_family_root_strips_only_the_outermost_dated_suffix() -> None:
    """A name with two date-like suffixes (`foo-2025-01-01-2025-08-07`)
    should strip only the trailing one; the inner date stays. (Edge
    case — not in any catalog today, but documents the regex's
    single-pass behavior.)"""
    assert family_root("foo-2025-01-01-2025-08-07") == "foo-2025-01-01"
