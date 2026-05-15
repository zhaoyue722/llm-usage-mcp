"""Tests for `llm_usage.core.quality`.

Three layers:

- `_parse_quality_data` — the validation seam: numeric + range checks.
- `load_vendored_quality` — the real vendored file parses and every
  score is in range; model keys match `pricing_snapshot` so
  `recommend_provider`'s future join won't silently drop rows.
- `get_quality` / `all_quality` / `upsert_quality` — DB round trips.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from llm_usage.bootstrap import migrate_to_head
from llm_usage.core.db.models import PricingSnapshot, QualitySnapshot
from llm_usage.core.db.session import get_session
from llm_usage.core.pricing import upsert_pricing
from llm_usage.core.pricing_loader import load_vendored_pricing
from llm_usage.core.quality import (
    Quality,
    _parse_quality_data,
    all_quality,
    get_quality,
    load_vendored_quality,
    upsert_quality,
)


@pytest.fixture
def migrated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh DB with the schema, no rows."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    return db


# --- _parse_quality_data: validation ---------------------------------------


def test_parse_quality_data_basic() -> None:
    data: dict[str, dict[str, object]] = {
        "anthropic": {"claude-opus-4-7": 96, "claude-haiku-4-5": 82.5}
    }
    result = _parse_quality_data(data, fetched_at=1_700_000_000_000)
    assert len(result) == 2
    by_model = {q.model: q for q in result}
    assert by_model["claude-opus-4-7"].quality_score == 96.0
    assert by_model["claude-haiku-4-5"].quality_score == 82.5
    assert all(q.provider == "anthropic" for q in result)
    assert all(q.fetched_at == 1_700_000_000_000 for q in result)


def test_parse_quality_data_rejects_non_numeric_score() -> None:
    with pytest.raises(ValueError, match="must be a number"):
        _parse_quality_data({"openai": {"gpt-5.2": "high"}}, fetched_at=1)


def test_parse_quality_data_rejects_bool_score() -> None:
    """bool is an int subclass — a stray `true` must not pass as score 1."""
    with pytest.raises(ValueError, match="must be a number"):
        _parse_quality_data({"openai": {"gpt-5.2": True}}, fetched_at=1)


def test_parse_quality_data_rejects_score_above_100() -> None:
    with pytest.raises(ValueError, match=r"must be in \[0.0, 100.0\]"):
        _parse_quality_data({"openai": {"gpt-5.2": 150}}, fetched_at=1)


def test_parse_quality_data_rejects_negative_score() -> None:
    with pytest.raises(ValueError, match=r"must be in \[0.0, 100.0\]"):
        _parse_quality_data({"openai": {"gpt-5.2": -5}}, fetched_at=1)


def test_parse_quality_data_accepts_boundary_scores() -> None:
    data: dict[str, dict[str, object]] = {"openai": {"floor": 0, "ceiling": 100}}
    result = _parse_quality_data(data, fetched_at=1)
    scores = {q.model: q.quality_score for q in result}
    assert scores == {"floor": 0.0, "ceiling": 100.0}


# --- load_vendored_quality: the real file ----------------------------------


def test_vendored_quality_loads_and_is_in_range() -> None:
    qualities = load_vendored_quality(fetched_at=1_700_000_000_000)
    assert len(qualities) > 0
    for q in qualities:
        assert 0.0 <= q.quality_score <= 100.0
        assert q.fetched_at == 1_700_000_000_000
        assert q.provider and q.model


def test_vendored_quality_covers_all_v1_providers() -> None:
    qualities = load_vendored_quality(fetched_at=1)
    providers = {q.provider for q in qualities}
    assert providers == {"anthropic", "openai", "qwen", "deepseek"}


def test_vendored_quality_fetched_at_defaults_to_now() -> None:
    import time

    before = int(time.time() * 1000)
    qualities = load_vendored_quality()
    after = int(time.time() * 1000)
    assert qualities
    assert all(before <= q.fetched_at <= after for q in qualities)  # type: ignore[operator]


def test_vendored_quality_keys_exist_in_pricing(migrated_db: Path) -> None:
    """Every scored model must also be in pricing_snapshot.

    `recommend_provider` will join the two tables on (provider, model);
    a quality entry with no matching pricing row is dead weight (and a
    sign of a typo or a stale model name).
    """
    with get_session() as session:
        upsert_pricing(session, load_vendored_pricing(fetched_at=1))
        upsert_quality(session, load_vendored_quality(fetched_at=1))
        session.commit()

        priced = {
            (row.provider, row.model) for row in session.scalars(select(PricingSnapshot)).all()
        }
        scored = {
            (row.provider, row.model) for row in session.scalars(select(QualitySnapshot)).all()
        }

    orphans = scored - priced
    assert not orphans, f"quality entries with no pricing row: {sorted(orphans)}"


# --- Quality dataclass -----------------------------------------------------


def test_quality_from_orm_round_trips(migrated_db: Path) -> None:
    original = Quality(
        provider="anthropic",
        model="claude-opus-4-7",
        quality_score=96.0,
        fetched_at=1_700_000_000_000,
    )
    with get_session() as session:
        upsert_quality(session, [original])
        session.commit()
        row = session.get(QualitySnapshot, ("anthropic", "claude-opus-4-7"))
        assert row is not None
        restored = Quality.from_orm(row)
    assert restored == original


# --- get_quality / all_quality ---------------------------------------------


def test_get_quality_returns_none_for_unscored_model(migrated_db: Path) -> None:
    with get_session() as session:
        upsert_quality(session, load_vendored_quality(fetched_at=1))
        session.commit()
        assert get_quality(session, "anthropic", "model-not-scored") is None


def test_get_quality_returns_the_score(migrated_db: Path) -> None:
    with get_session() as session:
        upsert_quality(
            session,
            [Quality("openai", "gpt-5.2", 91.0, fetched_at=1)],
        )
        session.commit()
        q = get_quality(session, "openai", "gpt-5.2")
    assert q is not None
    assert q.quality_score == 91.0


def test_all_quality_returns_sorted(migrated_db: Path) -> None:
    rows = [
        Quality("openai", "gpt-5.2", 91.0, fetched_at=1),
        Quality("anthropic", "claude-opus-4-7", 96.0, fetched_at=1),
        Quality("anthropic", "claude-haiku-4-5", 82.0, fetched_at=1),
    ]
    with get_session() as session:
        upsert_quality(session, rows)
        session.commit()
        result = all_quality(session)
    keys = [(q.provider, q.model) for q in result]
    assert keys == sorted(keys)
    assert len(result) == 3


def test_all_quality_empty_when_table_empty(migrated_db: Path) -> None:
    with get_session() as session:
        assert all_quality(session) == []


# --- upsert_quality --------------------------------------------------------


def test_upsert_quality_is_idempotent(migrated_db: Path) -> None:
    rows = load_vendored_quality(fetched_at=1)
    with get_session() as session:
        first = upsert_quality(session, rows)
        session.commit()
        second = upsert_quality(session, rows)
        session.commit()
        total = session.scalar(select(func.count()).select_from(QualitySnapshot))
    assert first == second == len(rows)
    assert total == len(rows)  # no duplicates


def test_upsert_quality_updates_score_and_fetched_at(migrated_db: Path) -> None:
    with get_session() as session:
        upsert_quality(session, [Quality("openai", "gpt-5.2", 91.0, fetched_at=1)])
        session.commit()
        upsert_quality(session, [Quality("openai", "gpt-5.2", 88.0, fetched_at=2)])
        session.commit()
        row = session.get(QualitySnapshot, ("openai", "gpt-5.2"))
    assert row is not None
    assert row.quality_score == 88.0
    assert row.fetched_at == 2


def test_upsert_quality_requires_fetched_at(migrated_db: Path) -> None:
    with get_session() as session, pytest.raises(ValueError, match="missing fetched_at"):
        upsert_quality(session, [Quality("openai", "gpt-5.2", 91.0, fetched_at=None)])


def test_upsert_quality_empty_iterable_returns_zero(migrated_db: Path) -> None:
    with get_session() as session:
        assert upsert_quality(session, []) == 0
