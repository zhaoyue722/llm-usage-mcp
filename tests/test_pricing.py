"""Tests for `Pricing`, `CostCalculator`, and `get_pricing`.

Test data uses real Anthropic Claude Sonnet 4.6 rates (as of mid-2026)
so the tests double as living documentation for what cache pricing
actually looks like in the wild.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from llm_usage.core import (
    Base,
    CostCalculator,
    Pricing,
    PricingSnapshot,
    get_pricing,
    nano_to_usd,
    usd_to_nano,
)

# Anthropic Claude Sonnet 4.6: $3/$15 input/output, $3.75/$0.30 cache write/read.
ANTHROPIC_PRICING = Pricing(
    provider="anthropic",
    model="claude-sonnet-4-6",
    input_per_million_usd=3.00,
    output_per_million_usd=15.00,
    cache_write_per_million_usd=3.75,
    cache_read_per_million_usd=0.30,
    fetched_at=1_700_000_000_000,
)

# A model with no cache pricing (e.g., legacy Qwen).
NO_CACHE_PRICING = Pricing(
    provider="qwen",
    model="qwen-turbo-legacy",
    input_per_million_usd=0.30,
    output_per_million_usd=0.60,
)

# OpenAI gpt-4o: $2.50/$10.00 input/output, $1.25/M cache-read (no separate cache-write).
OPENAI_PRICING = Pricing(
    provider="openai",
    model="gpt-4o",
    input_per_million_usd=2.50,
    output_per_million_usd=10.00,
    cache_write_per_million_usd=None,
    cache_read_per_million_usd=1.25,
)

# DeepSeek deepseek-chat: $0.28/$0.42 input/output, $0.028/M cache-read.
DEEPSEEK_PRICING = Pricing(
    provider="deepseek",
    model="deepseek-chat",
    input_per_million_usd=0.28,
    output_per_million_usd=0.42,
    cache_write_per_million_usd=None,
    cache_read_per_million_usd=0.028,
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine("sqlite://", future=True)
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


# --- Pricing ----------------------------------------------------------------


def test_pricing_is_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        ANTHROPIC_PRICING.input_per_million_usd = 99.0  # type: ignore[misc]


def test_pricing_from_orm_round_trip() -> None:
    row = PricingSnapshot(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_per_million_usd=3.00,
        output_per_million_usd=15.00,
        cache_write_per_million_usd=3.75,
        cache_read_per_million_usd=0.30,
        fetched_at=1_700_000_000_000,
    )
    p = Pricing.from_orm(row)

    assert p.provider == "anthropic"
    assert p.model == "claude-sonnet-4-6"
    assert p.input_per_million_usd == 3.00
    assert p.output_per_million_usd == 15.00
    assert p.cache_write_per_million_usd == 3.75
    assert p.cache_read_per_million_usd == 0.30
    assert p.fetched_at == 1_700_000_000_000


def test_pricing_from_orm_with_no_cache_rates() -> None:
    row = PricingSnapshot(
        provider="qwen",
        model="qwen-turbo-legacy",
        input_per_million_usd=0.30,
        output_per_million_usd=0.60,
        cache_write_per_million_usd=None,
        cache_read_per_million_usd=None,
        fetched_at=1_700_000_000_000,
    )
    p = Pricing.from_orm(row)
    assert p.cache_write_per_million_usd is None
    assert p.cache_read_per_million_usd is None


# --- CostCalculator: arithmetic --------------------------------------------


def test_cost_one_million_input_tokens() -> None:
    """1M input tokens at $3/M = $3.00 = 3,000,000,000 nano-USD."""
    calc = CostCalculator(ANTHROPIC_PRICING)
    assert calc.cost_nano_usd(input_tokens=1_000_000, output_tokens=0) == 3_000_000_000


def test_cost_one_million_output_tokens() -> None:
    """1M output tokens at $15/M = $15.00 = 15,000,000,000 nano-USD."""
    calc = CostCalculator(ANTHROPIC_PRICING)
    assert calc.cost_nano_usd(input_tokens=0, output_tokens=1_000_000) == 15_000_000_000


def test_cost_one_million_cache_write_tokens() -> None:
    """1M cache-write tokens at $3.75/M = $3.75 = 3,750,000,000 nano-USD."""
    calc = CostCalculator(ANTHROPIC_PRICING)
    assert (
        calc.cost_nano_usd(input_tokens=0, output_tokens=0, cache_write_tokens=1_000_000)
        == 3_750_000_000
    )


def test_cost_one_million_cache_read_tokens() -> None:
    """1M cache-read tokens at $0.30/M = $0.30 = 300,000,000 nano-USD."""
    calc = CostCalculator(ANTHROPIC_PRICING)
    assert (
        calc.cost_nano_usd(input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000)
        == 300_000_000
    )


def test_cost_combined_anthropic_session() -> None:
    """Realistic session: 100K cache-write + 900K cache-read + small i/o.

    Per-line USD:
      1_000   input   * $3.00/M = $0.003
        500   output  * $15.00/M = $0.0075
    100_000   c_write * $3.75/M = $0.375
    900_000   c_read  * $0.30/M = $0.270
    Total = $0.6555 = 655_500_000 nano-USD.
    """
    calc = CostCalculator(ANTHROPIC_PRICING)
    cost = calc.cost_nano_usd(
        input_tokens=1_000,
        output_tokens=500,
        cache_write_tokens=100_000,
        cache_read_tokens=900_000,
    )
    assert cost == 655_500_000


def test_cost_zero_tokens_is_zero() -> None:
    calc = CostCalculator(ANTHROPIC_PRICING)
    assert calc.cost_nano_usd(input_tokens=0, output_tokens=0) == 0


def test_cost_openai_input_output_only() -> None:
    """Smoke test on OpenAI rates: 100K input + 50K output, no cache.

    100_000 * $2.50/M = $0.25  = 250_000_000 nano-USD
     50_000 * $10.00/M = $0.50 = 500_000_000 nano-USD
    Total = 750_000_000 nano-USD.
    """
    calc = CostCalculator(OPENAI_PRICING)
    cost = calc.cost_nano_usd(input_tokens=100_000, output_tokens=50_000)
    assert cost == 750_000_000


def test_cost_deepseek_input_output_only() -> None:
    """Smoke test on DeepSeek rates: 1M input + 500K output, no cache.

    1_000_000 * $0.28/M = $0.28 = 280_000_000 nano-USD
      500_000 * $0.42/M = $0.21 = 210_000_000 nano-USD
    Total = 490_000_000 nano-USD.
    """
    calc = CostCalculator(DEEPSEEK_PRICING)
    cost = calc.cost_nano_usd(input_tokens=1_000_000, output_tokens=500_000)
    assert cost == 490_000_000


def test_pricing_property_exposes_underlying_pricing() -> None:
    calc = CostCalculator(ANTHROPIC_PRICING)
    assert calc.pricing is ANTHROPIC_PRICING


# --- CostCalculator: validation --------------------------------------------


def test_cost_no_cache_rate_with_zero_cache_tokens_is_fine() -> None:
    """Models without cache pricing handle zero cache tokens without raising."""
    calc = CostCalculator(NO_CACHE_PRICING)
    cost = calc.cost_nano_usd(input_tokens=1_000, output_tokens=500)
    # 1_000 * $0.30/M = $0.0003; 500 * $0.60/M = $0.0003; total $0.0006 = 600_000 nano-USD.
    assert cost == 600_000


def test_cost_raises_when_cache_write_tokens_but_no_write_rate() -> None:
    calc = CostCalculator(NO_CACHE_PRICING)
    with pytest.raises(ValueError, match="cache_write_tokens"):
        calc.cost_nano_usd(input_tokens=0, output_tokens=0, cache_write_tokens=100)


def test_cost_raises_when_cache_read_tokens_but_no_read_rate() -> None:
    calc = CostCalculator(NO_CACHE_PRICING)
    with pytest.raises(ValueError, match="cache_read_tokens"):
        calc.cost_nano_usd(input_tokens=0, output_tokens=0, cache_read_tokens=100)


def test_cost_error_message_identifies_provider_and_model() -> None:
    calc = CostCalculator(NO_CACHE_PRICING)
    with pytest.raises(ValueError, match="qwen/qwen-turbo-legacy"):
        calc.cost_nano_usd(input_tokens=0, output_tokens=0, cache_read_tokens=1)


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"input_tokens": -1, "output_tokens": 0}, "input_tokens"),
        ({"input_tokens": 0, "output_tokens": -1}, "output_tokens"),
        ({"input_tokens": 0, "output_tokens": 0, "cache_write_tokens": -1}, "cache_write_tokens"),
        ({"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": -1}, "cache_read_tokens"),
    ],
)
def test_cost_raises_on_negative_tokens(kwargs: dict[str, int], field: str) -> None:
    calc = CostCalculator(ANTHROPIC_PRICING)
    with pytest.raises(ValueError, match=field):
        calc.cost_nano_usd(**kwargs)


# --- get_pricing -----------------------------------------------------------


def test_get_pricing_returns_none_for_unknown_model(engine: Engine) -> None:
    with Session(engine) as session:
        assert get_pricing(session, "anthropic", "no-such-model") is None


def test_get_pricing_returns_pricing_for_known_model(engine: Engine) -> None:
    with Session(engine) as session:
        session.add(
            PricingSnapshot(
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_per_million_usd=3.00,
                output_per_million_usd=15.00,
                cache_write_per_million_usd=3.75,
                cache_read_per_million_usd=0.30,
                fetched_at=1_700_000_000_000,
            )
        )
        session.commit()

        pricing = get_pricing(session, "anthropic", "claude-sonnet-4-6")
        assert pricing is not None
        assert pricing.input_per_million_usd == 3.00
        assert pricing.cache_read_per_million_usd == 0.30
        assert pricing.fetched_at == 1_700_000_000_000


def test_calculator_end_to_end_via_get_pricing(engine: Engine) -> None:
    """Full flow: insert pricing, fetch via get_pricing, compute cost."""
    with Session(engine) as session:
        session.add(
            PricingSnapshot(
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_per_million_usd=3.00,
                output_per_million_usd=15.00,
                cache_write_per_million_usd=3.75,
                cache_read_per_million_usd=0.30,
                fetched_at=1_700_000_000_000,
            )
        )
        session.commit()

        pricing = get_pricing(session, "anthropic", "claude-sonnet-4-6")
        assert pricing is not None
        calc = CostCalculator(pricing)
        cost = calc.cost_nano_usd(input_tokens=1_000_000, output_tokens=0)
        assert cost == 3_000_000_000


# --- usd_to_nano / nano_to_usd boundary helpers ---------------------------


def test_usd_to_nano_zero() -> None:
    assert usd_to_nano(0.0) == 0


def test_usd_to_nano_one_dollar() -> None:
    assert usd_to_nano(1.0) == 1_000_000_000


def test_usd_to_nano_one_cache_read_token() -> None:
    """A single Anthropic cache-read token at $0.30/M = $3e-7 = 300 nano-USD.

    Pins the sub-nano-relevant boundary: the smallest value the storage
    layer must represent without rounding to zero.
    """
    assert usd_to_nano(3e-7) == 300


def test_usd_to_nano_rounds_to_nearest_nano() -> None:
    """0.5 nano rounds to 0 (banker's rounding to even)."""
    assert usd_to_nano(0.5e-9) == 0
    assert usd_to_nano(1.5e-9) == 2  # banker's rounding: 1.5 -> 2 (toward even)


def test_nano_to_usd_zero() -> None:
    assert nano_to_usd(0) == 0.0


def test_nano_to_usd_one_dollar() -> None:
    assert nano_to_usd(1_000_000_000) == 1.0


def test_nano_to_usd_returns_float() -> None:
    """The MCP boundary returns float USD per spec — verify the type."""
    assert isinstance(nano_to_usd(300), float)


def test_round_trip_typical_call_cost() -> None:
    """The $0.6555 worked example from the pricing entry: 655_500_000 nano-USD.

    Round-tripping through usd_to_nano(nano_to_usd(n)) must be exact for
    any value the calculator can produce — there should be no drift at
    the API boundary.
    """
    nano = 655_500_000
    assert usd_to_nano(nano_to_usd(nano)) == nano


def test_round_trip_large_cost() -> None:
    """One million dollars in nano-USD round-trips without loss."""
    nano = 1_000_000 * 1_000_000_000  # $1M
    assert usd_to_nano(nano_to_usd(nano)) == nano
