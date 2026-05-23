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
    PricingTier,
    Tier,
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


# --- tier writes -----------------------------------------------------------


def _qwen_flash_with_tiers(fetched_at: int = 1_700_000_000_000) -> Pricing:
    """A Pricing carrying two tiers, mirroring qwen-flash's shape."""
    return Pricing(
        provider="qwen",
        model="qwen-flash",
        # Flat fallback = tier 0's rates (per the loader's contract).
        input_per_million_usd=0.05,
        output_per_million_usd=0.40,
        fetched_at=fetched_at,
        tiers=(
            Tier(
                tier_index=0,
                range_start=0,
                range_end=256_000,
                input_per_million_usd=0.05,
                output_per_million_usd=0.40,
            ),
            Tier(
                tier_index=1,
                range_start=256_000,
                range_end=1_000_000,
                input_per_million_usd=0.25,
                output_per_million_usd=2.00,
            ),
        ),
    )


def _all_tiers(session: Session) -> list[PricingTier]:
    return list(session.scalars(select(PricingTier).order_by(PricingTier.tier_index)).all())


def test_upsert_writes_tier_rows_when_pricing_has_tiers(engine: Engine) -> None:
    """A tiered Pricing materializes one pricing_tier row per tier."""
    with Session(engine) as session:
        upsert_pricing(session, [_qwen_flash_with_tiers()])
        session.commit()

        tiers = _all_tiers(session)
        assert len(tiers) == 2
        assert tiers[0].provider == "qwen"
        assert tiers[0].model == "qwen-flash"
        assert tiers[0].tier_index == 0
        assert tiers[0].range_start == 0
        assert tiers[0].range_end == 256_000
        assert tiers[0].input_per_million_usd == 0.05
        assert tiers[1].tier_index == 1
        assert tiers[1].range_end == 1_000_000


def test_upsert_writes_no_tier_rows_for_flat_pricing(engine: Engine) -> None:
    """Flat-rate models don't produce pricing_tier rows."""
    with Session(engine) as session:
        upsert_pricing(session, [_anthropic(), _qwen_no_cache()])
        session.commit()
        assert _all_tiers(session) == []


def test_upsert_tier_replace_semantics(engine: Engine) -> None:
    """Re-upserting a tiered model deletes its old tier rows and inserts the new ones.

    Concrete sequence: upsert with 2 tiers, then upsert the same model
    with 3 tiers. The final state must be exactly the 3 new tiers — no
    leftover row from the first upsert.
    """
    with Session(engine) as session:
        upsert_pricing(session, [_qwen_flash_with_tiers()])
        session.commit()
        assert len(_all_tiers(session)) == 2

        # Different shape: 3 tiers, with adjusted ranges + rates.
        bumped = Pricing(
            provider="qwen",
            model="qwen-flash",
            input_per_million_usd=0.03,
            output_per_million_usd=0.30,
            fetched_at=1_700_000_000_001,
            tiers=(
                Tier(0, 0, 128_000, 0.03, 0.30),
                Tier(1, 128_000, 512_000, 0.10, 1.00),
                Tier(2, 512_000, 1_000_000, 0.20, 2.00),
            ),
        )
        upsert_pricing(session, [bumped])
        session.commit()

        tiers = _all_tiers(session)
        assert [t.tier_index for t in tiers] == [0, 1, 2]
        assert tiers[0].range_end == 128_000
        assert tiers[2].range_end == 1_000_000
        # The 256_000 boundary from the prior upsert must be gone.
        boundaries = {t.range_end for t in tiers}
        assert 256_000 not in boundaries


def test_upsert_drops_tiers_when_pricing_loses_them(engine: Engine) -> None:
    """A model that had tiers and now doesn't loses its tier rows.

    Snapshot semantics: the new upsert is the source of truth. If the
    upstream JSON drops `tiered_pricing` for a model, the next refresh
    must clean up the orphaned rows rather than leaving stale state.
    """
    with Session(engine) as session:
        upsert_pricing(session, [_qwen_flash_with_tiers()])
        session.commit()
        assert len(_all_tiers(session)) == 2

        flat_only = Pricing(
            provider="qwen",
            model="qwen-flash",
            input_per_million_usd=0.05,
            output_per_million_usd=0.40,
            fetched_at=1_700_000_000_001,
            tiers=(),
        )
        upsert_pricing(session, [flat_only])
        session.commit()
        assert _all_tiers(session) == []


def test_upsert_leaves_unrelated_models_tiers_alone(engine: Engine) -> None:
    """Reconciling one model's tiers must not touch another model's rows."""
    other_tiered = Pricing(
        provider="qwen",
        model="qwen-other-tiered",
        input_per_million_usd=1.0,
        output_per_million_usd=2.0,
        fetched_at=1_700_000_000_000,
        tiers=(Tier(0, 0, 100_000, 1.0, 2.0),),
    )
    with Session(engine) as session:
        upsert_pricing(session, [_qwen_flash_with_tiers(), other_tiered])
        session.commit()
        assert len(_all_tiers(session)) == 3  # 2 for qwen-flash + 1 for the other

        # Re-upsert ONLY qwen-flash, with different tiers.
        flat_only = Pricing(
            provider="qwen",
            model="qwen-flash",
            input_per_million_usd=0.05,
            output_per_million_usd=0.40,
            fetched_at=1_700_000_000_001,
            tiers=(),
        )
        upsert_pricing(session, [flat_only])
        session.commit()

        tiers = _all_tiers(session)
        # qwen-flash tiers gone; qwen-other-tiered's tier untouched.
        assert len(tiers) == 1
        assert tiers[0].model == "qwen-other-tiered"


def test_load_vendored_upsert_writes_expected_tier_count(engine: Engine) -> None:
    """End-to-end: loader → upsert produces a tier row per (model, tier).

    Catches breakage where the loader stops emitting tiers OR the upsert
    fails to persist them. Specific count (40 today) would be brittle, so
    assert structural properties instead: at least one tiered model
    exists, every tier row belongs to some pricing_snapshot row, and
    tier_indexes start at 0 per model.
    """
    pricings = load_vendored_pricing(fetched_at=1_700_000_000_000)
    with Session(engine) as session:
        upsert_pricing(session, pricings)
        session.commit()

        tier_count = session.execute(
            select(PricingTier).order_by(PricingTier.provider, PricingTier.model)
        ).all()
        assert len(tier_count) > 0

        snapshot_keys = {
            (r.provider, r.model) for r in session.scalars(select(PricingSnapshot)).all()
        }
        for tier_row in session.scalars(select(PricingTier)).all():
            assert (tier_row.provider, tier_row.model) in snapshot_keys

        # Every model's tiers must start at index 0 (sanity: order
        # preserved across loader → upsert → query).
        from collections import defaultdict

        by_model: dict[tuple[str, str], list[int]] = defaultdict(list)
        for t in session.scalars(select(PricingTier)).all():
            by_model[(t.provider, t.model)].append(t.tier_index)
        for indexes in by_model.values():
            assert min(indexes) == 0
