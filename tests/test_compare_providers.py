"""Tests for the `compare_providers` MCP tool.

Cost projection is asserted exactly against controlled `Pricing` rows
inserted by the test (round numbers), not `pytest.approx` of vendored
LiteLLM values — so the tests don't drift on a pricing refresh.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import llm_usage.mcp.server as server_module
from llm_usage.bootstrap import migrate_to_head
from llm_usage.core.db.session import get_session
from llm_usage.core.models import CompareProvidersResult
from llm_usage.core.pricing import Pricing, upsert_pricing

# Three controlled models with round per-million rates so both the
# absolute cost and the relative_cost_pct come out exact.
#   cheap  : $1/M in,  $2/M out
#   mid    : $2/M in,  $4/M out   (2x cheap)
#   premium: $5/M in, $10/M out   (5x cheap)
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
    """Fresh DB with the schema and three controlled pricing rows."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(session, _PRICINGS)
        session.commit()
    return db


@pytest.fixture
def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh DB with the schema but no pricing rows."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    return db


def _compare(
    *,
    expected_input_tokens: int,
    expected_output_tokens: int,
    models: list[str] | None = None,
    include_snapshots: bool = False,
) -> CompareProvidersResult:
    # `@server.tool()` erases the return type for mypy's view; the local
    # annotation pins it back to the real `*Result` model.
    result: CompareProvidersResult = asyncio.run(
        server_module.compare_providers(
            expected_input_tokens=expected_input_tokens,
            expected_output_tokens=expected_output_tokens,
            models=models,
            include_snapshots=include_snapshots,
        )
    )
    return result


# --- ranking + cost --------------------------------------------------------


def test_ranks_cheapest_first(priced_db: Path) -> None:
    result = _compare(expected_input_tokens=1_000_000, expected_output_tokens=1_000_000)
    models = [entry.model for entry in result.ranked]
    assert models == ["cheap-1", "mid-1", "premium-1"]


def test_absolute_cost_is_exact(priced_db: Path) -> None:
    result = _compare(expected_input_tokens=1_000_000, expected_output_tokens=1_000_000)
    by_model = {entry.model: entry for entry in result.ranked}
    # cheap: 1M @ $1/M + 1M @ $2/M = $3.00
    assert by_model["cheap-1"].cost_usd == pytest.approx(3.0)
    # mid: 1M @ $2/M + 1M @ $4/M = $6.00
    assert by_model["mid-1"].cost_usd == pytest.approx(6.0)
    # premium: 1M @ $5/M + 1M @ $10/M = $15.00
    assert by_model["premium-1"].cost_usd == pytest.approx(15.0)


def test_relative_cost_pct_against_cheapest(priced_db: Path) -> None:
    result = _compare(expected_input_tokens=1_000_000, expected_output_tokens=1_000_000)
    by_model = {entry.model: entry for entry in result.ranked}
    # cheapest is the 100% baseline; mid is 2x, premium is 5x.
    assert by_model["cheap-1"].relative_cost_pct == 100.0
    assert by_model["mid-1"].relative_cost_pct == 200.0
    assert by_model["premium-1"].relative_cost_pct == 500.0


def test_every_model_appears(priced_db: Path) -> None:
    result = _compare(expected_input_tokens=100, expected_output_tokens=100)
    assert len(result.ranked) == 3
    assert {e.provider for e in result.ranked} == {"deepseek", "openai", "anthropic"}


# --- models filter ---------------------------------------------------------


def test_models_filter_restricts(priced_db: Path) -> None:
    result = _compare(
        expected_input_tokens=1_000_000,
        expected_output_tokens=0,
        models=["cheap-1", "premium-1"],
    )
    models = [entry.model for entry in result.ranked]
    assert models == ["cheap-1", "premium-1"]  # mid-1 excluded
    # relative_cost_pct re-baselines against the cheapest *in the filtered set*.
    by_model = {entry.model: entry for entry in result.ranked}
    assert by_model["cheap-1"].relative_cost_pct == 100.0
    assert by_model["premium-1"].relative_cost_pct == 500.0


def test_models_filter_matching_nothing_returns_empty(priced_db: Path) -> None:
    result = _compare(
        expected_input_tokens=100,
        expected_output_tokens=100,
        models=["does-not-exist"],
    )
    assert result.ranked == []


# --- empty / edge cases ----------------------------------------------------


def test_empty_pricing_table_returns_empty_ranked(empty_db: Path) -> None:
    result = _compare(expected_input_tokens=100, expected_output_tokens=100)
    assert result.ranked == []


def test_zero_token_workload_all_cost_zero_all_100_pct(priced_db: Path) -> None:
    result = _compare(expected_input_tokens=0, expected_output_tokens=0)
    assert len(result.ranked) == 3
    for entry in result.ranked:
        assert entry.cost_usd == 0.0
        assert entry.relative_cost_pct == 100.0


# --- notes field ----------------------------------------------------------


def test_notes_always_none_in_v1(priced_db: Path) -> None:
    """`RankedEntry.notes` is reserved for future per-row caveats; v1 leaves it None."""
    result = _compare(expected_input_tokens=100, expected_output_tokens=100)
    assert result.ranked
    assert all(entry.notes is None for entry in result.ranked)


# --- family dedup --------------------------------------------------------


def _family_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """DB with a family-rich pricing set for dedup tests.

    The four `qwen-foo*` rows share family root `qwen-foo` at identical
    rates (so they collapse into one entry with variant_count=4 when
    dedup is on). The two `gpt-bar*` rows share family root `gpt-bar`
    at DIFFERENT rates (so both survive even with dedup on). The
    `claude-baz` row is a unique-family canonical name (variant_count=1).
    """
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(
            session,
            [
                # Family `qwen-foo` — alias + two snapshots + `-latest`,
                # all at $1/M in + $2/M out. Dedup → one row, variant=4.
                Pricing("qwen", "qwen-foo", 1.0, 2.0, fetched_at=1),
                Pricing("qwen", "qwen-foo-2024-11-01", 1.0, 2.0, fetched_at=1),
                Pricing("qwen", "qwen-foo-2025-04-28", 1.0, 2.0, fetched_at=1),
                Pricing("qwen", "qwen-foo-latest", 1.0, 2.0, fetched_at=1),
                # Family `gpt-bar` — alias and snapshot at DIFFERENT
                # prices. Both must survive (price divergence ≠ noise).
                Pricing("openai", "gpt-bar", 3.0, 6.0, fetched_at=1),
                Pricing("openai", "gpt-bar-2025-08-07", 2.0, 4.0, fetched_at=1),
                # Unique-family row.
                Pricing("anthropic", "claude-baz", 5.0, 10.0, fetched_at=1),
            ],
        )
        session.commit()
    return db


def test_compare_default_collapses_same_price_family_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4 qwen-foo* rows priced identically → 1 entry with variant_count=4."""
    _family_db(tmp_path, monkeypatch)
    result = _compare(expected_input_tokens=1_000_000, expected_output_tokens=1_000_000)
    by_family = {entry.model: entry for entry in result.ranked}
    assert "qwen-foo" in by_family  # canonical (alias) wins the lex tie-break
    assert by_family["qwen-foo"].variant_count == 4
    # No snapshot variants leak through.
    assert "qwen-foo-2024-11-01" not in by_family
    assert "qwen-foo-latest" not in by_family


def test_compare_default_keeps_different_price_family_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gpt-bar and gpt-bar-2025-08-07 share a family root but have
    different prices — both rows must survive (no silent collapse)."""
    _family_db(tmp_path, monkeypatch)
    result = _compare(expected_input_tokens=1_000_000, expected_output_tokens=1_000_000)
    models = [entry.model for entry in result.ranked]
    assert "gpt-bar" in models
    assert "gpt-bar-2025-08-07" in models
    # Each is its own row, each variant_count=1.
    by_model = {entry.model: entry for entry in result.ranked}
    assert by_model["gpt-bar"].variant_count == 1
    assert by_model["gpt-bar-2025-08-07"].variant_count == 1


def test_compare_default_dedup_picks_alphabetically_first_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Within a (family, cost) class the alphabetically-first entry
    wins. `qwen-foo` < `qwen-foo-2024-11-01` < `qwen-foo-2025-04-28` <
    `qwen-foo-latest`, so the alias is the kept representative."""
    _family_db(tmp_path, monkeypatch)
    result = _compare(expected_input_tokens=1_000_000, expected_output_tokens=1_000_000)
    qwen_rows = [entry for entry in result.ranked if entry.provider == "qwen"]
    assert len(qwen_rows) == 1
    assert qwen_rows[0].model == "qwen-foo"


def test_compare_include_snapshots_returns_every_catalog_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `include_snapshots=True` the dedup is disabled — every
    catalog row appears as its own entry with variant_count=1."""
    _family_db(tmp_path, monkeypatch)
    result = _compare(
        expected_input_tokens=1_000_000,
        expected_output_tokens=1_000_000,
        include_snapshots=True,
    )
    models = [entry.model for entry in result.ranked]
    assert "qwen-foo" in models
    assert "qwen-foo-2024-11-01" in models
    assert "qwen-foo-2025-04-28" in models
    assert "qwen-foo-latest" in models
    # Every row is solo.
    assert all(entry.variant_count == 1 for entry in result.ranked)


def test_compare_default_unique_families_keep_variant_count_one(
    priced_db: Path,
) -> None:
    """With the standard fixture (three distinct family roots, no
    alias/snapshot variants), every row should report variant_count=1."""
    result = _compare(expected_input_tokens=100, expected_output_tokens=100)
    assert all(entry.variant_count == 1 for entry in result.ranked)
