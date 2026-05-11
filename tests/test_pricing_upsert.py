"""Tests for `upsert_pricing` — idempotent writes to `pricing_snapshot`.

The function must:

- insert new (provider, model) rows on first call;
- on a second call with the same key, refresh `fetched_at` and any rate
  changes without producing duplicate rows (the table's composite PK
  guarantees uniqueness, so a bug here surfaces as an IntegrityError);
- preserve `None` for absent cache rates rather than coercing to `0`;
- require a non-None `fetched_at` (the column is NOT NULL in the schema);
- leave commit responsibility to the caller.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from llm_usage.core import (
    Base,
    Pricing,
    PricingSnapshot,
    get_pricing,
    load_vendored_pricing,
    upsert_pricing,
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine("sqlite://", future=True)
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _anthropic(fetched_at: int = 1_700_000_000_000) -> Pricing:
    return Pricing(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_per_million_usd=3.00,
        output_per_million_usd=15.00,
        cache_write_per_million_usd=3.75,
        cache_read_per_million_usd=0.30,
        fetched_at=fetched_at,
    )


def _openai(fetched_at: int = 1_700_000_000_000) -> Pricing:
    # Cache-write absorbed into input — cache_write_per_million_usd stays None.
    return Pricing(
        provider="openai",
        model="gpt-4o",
        input_per_million_usd=2.50,
        output_per_million_usd=10.00,
        cache_write_per_million_usd=None,
        cache_read_per_million_usd=1.25,
        fetched_at=fetched_at,
    )


def _qwen_no_cache(fetched_at: int = 1_700_000_000_000) -> Pricing:
    return Pricing(
        provider="qwen",
        model="qwen-turbo-legacy",
        input_per_million_usd=0.30,
        output_per_million_usd=0.60,
        cache_write_per_million_usd=None,
        cache_read_per_million_usd=None,
        fetched_at=fetched_at,
    )


# --- empty input -----------------------------------------------------------


def test_upsert_empty_list_is_noop(engine: Engine) -> None:
    with Session(engine) as session:
        count = upsert_pricing(session, [])
        session.commit()
        assert count == 0
        assert session.scalars(select(PricingSnapshot)).all() == []


# --- first insert ----------------------------------------------------------


def test_upsert_inserts_new_rows(engine: Engine) -> None:
    with Session(engine) as session:
        count = upsert_pricing(session, [_anthropic(), _openai(), _qwen_no_cache()])
        session.commit()
        assert count == 3

        rows = session.scalars(select(PricingSnapshot)).all()
        keys = {(r.provider, r.model) for r in rows}
        assert keys == {
            ("anthropic", "claude-sonnet-4-6"),
            ("openai", "gpt-4o"),
            ("qwen", "qwen-turbo-legacy"),
        }


def test_upsert_preserves_null_cache_rates(engine: Engine) -> None:
    """Absent cache rates must stay NULL, not silently coerce to 0."""
    with Session(engine) as session:
        upsert_pricing(session, [_qwen_no_cache()])
        session.commit()

        row = session.get(PricingSnapshot, ("qwen", "qwen-turbo-legacy"))
        assert row is not None
        assert row.cache_write_per_million_usd is None
        assert row.cache_read_per_million_usd is None


# --- idempotency on repeat calls -------------------------------------------


def test_upsert_is_idempotent_for_unchanged_input(engine: Engine) -> None:
    """Calling upsert twice with the same input must not duplicate or change rows."""
    with Session(engine) as session:
        upsert_pricing(session, [_anthropic()])
        session.commit()

        upsert_pricing(session, [_anthropic()])
        session.commit()

        rows = session.scalars(select(PricingSnapshot)).all()
        assert len(rows) == 1
        assert rows[0].input_per_million_usd == 3.00
        assert rows[0].fetched_at == 1_700_000_000_000


def test_upsert_updates_changed_rates_for_existing_key(engine: Engine) -> None:
    """When a rate changes on the same (provider, model), the row updates in place."""
    with Session(engine) as session:
        upsert_pricing(session, [_anthropic(fetched_at=1)])
        session.commit()

        bumped = Pricing(
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_per_million_usd=3.50,
            output_per_million_usd=15.00,
            cache_write_per_million_usd=3.75,
            cache_read_per_million_usd=0.30,
            fetched_at=2,
        )
        upsert_pricing(session, [bumped])
        session.commit()

        rows = session.scalars(select(PricingSnapshot)).all()
        assert len(rows) == 1
        assert rows[0].input_per_million_usd == 3.50
        assert rows[0].fetched_at == 2


def test_upsert_refreshes_fetched_at_even_when_rates_unchanged(engine: Engine) -> None:
    """A pricing refresh should bump fetched_at so callers can tell how recent the snapshot is."""
    with Session(engine) as session:
        upsert_pricing(session, [_anthropic(fetched_at=1)])
        session.commit()

        upsert_pricing(session, [_anthropic(fetched_at=999)])
        session.commit()

        row = session.get(PricingSnapshot, ("anthropic", "claude-sonnet-4-6"))
        assert row is not None
        assert row.fetched_at == 999


def test_upsert_can_clear_a_cache_rate(engine: Engine) -> None:
    """If a provider drops cache pricing, the new None must overwrite the old value."""
    with Session(engine) as session:
        upsert_pricing(session, [_openai()])  # cache_read = 1.25
        session.commit()

        no_more_cache = Pricing(
            provider="openai",
            model="gpt-4o",
            input_per_million_usd=2.50,
            output_per_million_usd=10.00,
            cache_write_per_million_usd=None,
            cache_read_per_million_usd=None,
            fetched_at=1_700_000_000_000,
        )
        upsert_pricing(session, [no_more_cache])
        session.commit()

        row = session.get(PricingSnapshot, ("openai", "gpt-4o"))
        assert row is not None
        assert row.cache_read_per_million_usd is None


def test_upsert_mixes_inserts_and_updates_in_one_call(engine: Engine) -> None:
    """Half the batch is new, half collides — both paths exercised in a single statement."""
    with Session(engine) as session:
        upsert_pricing(session, [_anthropic(fetched_at=1)])
        session.commit()

        batch = [_anthropic(fetched_at=2), _openai(fetched_at=2)]
        count = upsert_pricing(session, batch)
        session.commit()
        assert count == 2

        anthropic = session.get(PricingSnapshot, ("anthropic", "claude-sonnet-4-6"))
        openai = session.get(PricingSnapshot, ("openai", "gpt-4o"))
        assert anthropic is not None and anthropic.fetched_at == 2
        assert openai is not None and openai.fetched_at == 2


# --- contract: fetched_at required -----------------------------------------


def test_upsert_raises_when_fetched_at_is_none(engine: Engine) -> None:
    """fetched_at is NOT NULL in the schema; passing None is a caller bug."""
    bad = Pricing(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_per_million_usd=3.00,
        output_per_million_usd=15.00,
        fetched_at=None,
    )
    with Session(engine) as session, pytest.raises(ValueError, match="fetched_at"):
        upsert_pricing(session, [bad])


def test_upsert_does_not_write_partial_batch_when_one_row_invalid(engine: Engine) -> None:
    """Validation runs before the INSERT, so a bad row poisons the whole call."""
    with Session(engine) as session:
        with pytest.raises(ValueError, match="fetched_at"):
            upsert_pricing(
                session,
                [
                    _anthropic(),
                    Pricing(
                        provider="x",
                        model="y",
                        input_per_million_usd=1.0,
                        output_per_million_usd=2.0,
                        fetched_at=None,
                    ),
                ],
            )
        assert session.scalars(select(PricingSnapshot)).all() == []


# --- caller controls commit -----------------------------------------------


def test_upsert_does_not_commit(engine: Engine) -> None:
    """Without a commit, a new session must see no rows."""
    with Session(engine) as session:
        upsert_pricing(session, [_anthropic()])
        # deliberately no commit

    with Session(engine) as session:
        assert session.scalars(select(PricingSnapshot)).all() == []


# --- end-to-end pipeline ---------------------------------------------------


def test_loader_to_upsert_to_get_pricing_round_trip(engine: Engine) -> None:
    """load_vendored_pricing -> upsert -> get_pricing returns matching values."""
    pricings = load_vendored_pricing(fetched_at=1_700_000_000_000)
    with Session(engine) as session:
        count = upsert_pricing(session, pricings)
        session.commit()
        assert count == len(pricings)

        # Anthropic is in the vendored data; assert one well-known model round-trips.
        anthropic = next(
            (p for p in pricings if p.provider == "anthropic" and "sonnet" in p.model),
            None,
        )
        assert anthropic is not None
        fetched = get_pricing(session, anthropic.provider, anthropic.model)
        assert fetched is not None
        assert fetched.input_per_million_usd == anthropic.input_per_million_usd
        assert fetched.output_per_million_usd == anthropic.output_per_million_usd
        assert fetched.fetched_at == 1_700_000_000_000
