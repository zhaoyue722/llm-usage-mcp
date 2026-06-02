"""Unit tests for `core.pricing.query_pricing`.

Shared by the MCP `get_pricing` tool (which still works after the
refactor — covered by `test_read_path_tools.py`) and the new CLI
`models` command. These tests cover the filter combinations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_usage.bootstrap import migrate_to_head
from llm_usage.core.db.session import get_session
from llm_usage.core.pricing import Pricing, query_pricing, upsert_pricing


@pytest.fixture
def priced_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """DB with a small controlled catalog for filter tests."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(
            session,
            [
                Pricing("anthropic", "claude-haiku-4-5", 1.0, 5.0, fetched_at=1),
                Pricing("anthropic", "claude-sonnet-4-5", 3.0, 15.0, fetched_at=1),
                Pricing("openai", "gpt-5-nano", 0.05, 0.4, fetched_at=1),
                Pricing("openai", "gpt-5-mini", 0.25, 1.0, fetched_at=1),
                Pricing("qwen", "qwen-turbo", 0.05, 0.2, fetched_at=1),
            ],
        )
        session.commit()
    return db


def test_query_pricing_no_filters_returns_full_catalog(priced_db: Path) -> None:
    with get_session() as session:
        rows = query_pricing(session)
    assert len(rows) == 5


def test_query_pricing_sort_is_provider_then_model(priced_db: Path) -> None:
    """`query_pricing` sorts by (provider, model) so callers and tests
    have a deterministic order to assert against."""
    with get_session() as session:
        rows = query_pricing(session)
    assert [(r.provider, r.model) for r in rows] == [
        ("anthropic", "claude-haiku-4-5"),
        ("anthropic", "claude-sonnet-4-5"),
        ("openai", "gpt-5-mini"),
        ("openai", "gpt-5-nano"),
        ("qwen", "qwen-turbo"),
    ]


def test_query_pricing_providers_filter_narrows_results(priced_db: Path) -> None:
    with get_session() as session:
        rows = query_pricing(session, providers=["openai"])
    assert {r.provider for r in rows} == {"openai"}
    assert len(rows) == 2


def test_query_pricing_providers_filter_is_case_insensitive(priced_db: Path) -> None:
    """Provider names are a closed-set with known canonical case;
    matching the branded form (`OpenAI`) should hit the lowercase
    DB row (`openai`) — same UX as `recommend --provider`."""
    with get_session() as session:
        rows = query_pricing(session, providers=["OpenAI"])
    assert {r.provider for r in rows} == {"openai"}


def test_query_pricing_providers_filter_accepts_multiple(priced_db: Path) -> None:
    """The list form is an OR-style whitelist: `[openai, qwen]` matches
    any row whose provider is in the set."""
    with get_session() as session:
        rows = query_pricing(session, providers=["openai", "qwen"])
    assert {r.provider for r in rows} == {"openai", "qwen"}
    assert len(rows) == 3


def test_query_pricing_models_filter_is_case_sensitive(priced_db: Path) -> None:
    """Model names are open catalog literals — case-folding them would
    risk collapsing distinct entries. Mismatched case returns empty."""
    with get_session() as session:
        rows_lower = query_pricing(session, models=["gpt-5-nano"])
        rows_upper = query_pricing(session, models=["GPT-5-Nano"])
    assert len(rows_lower) == 1
    assert len(rows_upper) == 0


def test_query_pricing_match_substring_is_case_insensitive(priced_db: Path) -> None:
    """`match` substring on model name — case-insensitive so a user
    searching `--match Nano` hits the lowercase `gpt-5-nano`."""
    with get_session() as session:
        rows = query_pricing(session, match="nano")
    assert {r.model for r in rows} == {"gpt-5-nano"}


def test_query_pricing_match_with_provider_filter_and_combines(priced_db: Path) -> None:
    """All filters AND-combine. `providers=[openai] + match=mini`
    intersects to OpenAI's gpt-5-mini, not Anthropic or Qwen."""
    with get_session() as session:
        rows = query_pricing(session, providers=["openai"], match="mini")
    assert [(r.provider, r.model) for r in rows] == [("openai", "gpt-5-mini")]


def test_query_pricing_filter_with_no_matches_returns_empty(priced_db: Path) -> None:
    """Unknown name yields empty list — consistent with the rest of
    the read surface ('unknown returns empty', not 'unknown is an error')."""
    with get_session() as session:
        rows = query_pricing(session, providers=["no-such-provider"])
    assert rows == []


def test_query_pricing_returns_pricing_entry_pydantic_models(priced_db: Path) -> None:
    """The MCP get_pricing tool and the usage://pricing_table resource
    both expect `PricingEntry` shapes. Pin the type so a refactor
    can't quietly return `Pricing` dataclasses or raw rows."""
    from llm_usage.core.models import PricingEntry

    with get_session() as session:
        rows = query_pricing(session)
    assert all(isinstance(r, PricingEntry) for r in rows)
