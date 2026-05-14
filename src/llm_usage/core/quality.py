"""Model quality data: the vendored quality table and its accessors.

The quality counterpart to `pricing.py`. There is no calculator here —
quality is plain data, not something computed. `model_quality.json` is
hand-authored (see `quality_data/README.md`); `load_vendored_quality`
parses it, `upsert_quality` materializes it into `quality_snapshot`,
and `get_quality` / `all_quality` read it back.

Kept a separate module + table from pricing on purpose: quality and
pricing have independent sources (a hand-authored file today, a public
leaderboard importer later, vs. LiteLLM's pricing JSON) and refresh
cadences. A future importer overwrites only `model_quality.json`.

`quality_score` is a normalized float in [0, 100]; higher is better.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.resources import files

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from llm_usage.core.db.models import QualitySnapshot

_MIN_SCORE = 0.0
_MAX_SCORE = 100.0


@dataclass(frozen=True)
class Quality:
    """Normalized quality score for one (provider, model).

    `quality_score` is a float in [0, 100]. `fetched_at` is the
    millisecond epoch the score was materialized; `None` until stamped
    (mirrors `Pricing.fetched_at` — `upsert_quality` requires it set).
    """

    provider: str
    model: str
    quality_score: float
    fetched_at: int | None = None

    @classmethod
    def from_orm(cls, row: QualitySnapshot) -> Quality:
        return cls(
            provider=row.provider,
            model=row.model,
            quality_score=row.quality_score,
            fetched_at=row.fetched_at,
        )


def load_vendored_quality(*, fetched_at: int | None = None) -> list[Quality]:
    """Load the bundled `model_quality.json` into `Quality` records.

    The vendored file is a nested `provider -> model -> score` object
    (see `quality_data/README.md`). `fetched_at` is the millisecond
    epoch stamped onto every record; defaults to "now". Pass an explicit
    value in tests for determinism.

    Raises `ValueError` (via `_parse_quality_data`) if any score is
    non-numeric or outside [0, 100] — a malformed vendored file should
    fail loudly at load, not surface later as a nonsense recommendation.
    """
    text = files("llm_usage.core.quality_data").joinpath("model_quality.json").read_text()
    data: dict[str, dict[str, object]] = json.loads(text)
    ts = fetched_at if fetched_at is not None else int(time.time() * 1000)
    return _parse_quality_data(data, fetched_at=ts)


def _parse_quality_data(data: dict[str, dict[str, object]], *, fetched_at: int) -> list[Quality]:
    """Convert a nested `provider -> model -> score` dict into `Quality` records.

    Raises `ValueError` on a non-numeric or out-of-range score. Split
    out from the file read so the validation is unit-testable without
    monkeypatching the package resource — mirrors how `pricing_loader`
    keeps `parse_litellm_entry` separate from `load_vendored_pricing`.
    """
    qualities: list[Quality] = []
    for provider, models in data.items():
        for model, raw_score in models.items():
            # bool is a subclass of int — exclude it explicitly so a
            # stray `true` in the JSON doesn't pass as the score 1.
            if not isinstance(raw_score, int | float) or isinstance(raw_score, bool):
                raise ValueError(
                    f"quality score for {provider}/{model} must be a number, got {raw_score!r}"
                )
            score = float(raw_score)
            if not _MIN_SCORE <= score <= _MAX_SCORE:
                raise ValueError(
                    f"quality score for {provider}/{model} must be in "
                    f"[{_MIN_SCORE}, {_MAX_SCORE}], got {score}"
                )
            qualities.append(
                Quality(
                    provider=provider,
                    model=model,
                    quality_score=score,
                    fetched_at=fetched_at,
                )
            )
    return qualities


def get_quality(session: Session, provider: str, model: str) -> Quality | None:
    """Look up the quality score for a (provider, model) in `quality_snapshot`.

    Returns `None` if the model has no quality entry — many models in
    `pricing_snapshot` are intentionally unscored (see
    `quality_data/README.md`).
    """
    row = session.get(QualitySnapshot, (provider, model))
    if row is None:
        return None
    return Quality.from_orm(row)


def all_quality(session: Session) -> list[Quality]:
    """Return every `quality_snapshot` row as a `Quality`, sorted.

    The "get all" sibling of `get_quality`. Order is stable
    (provider, model) so callers get deterministic results.
    """
    stmt = select(QualitySnapshot).order_by(QualitySnapshot.provider, QualitySnapshot.model)
    return [Quality.from_orm(row) for row in session.scalars(stmt).all()]


def upsert_quality(session: Session, qualities: Iterable[Quality]) -> int:
    """Idempotently write `Quality` records into `quality_snapshot`.

    Uses SQLite's `INSERT ... ON CONFLICT (provider, model) DO UPDATE`,
    so re-running with the same input refreshes `fetched_at` and the
    score without duplicate rows. Returns the count of input records
    processed. Does not commit — the caller owns the transaction,
    matching `upsert_pricing`'s convention.

    `fetched_at` is required (the column is NOT NULL); a `None` value
    raises `ValueError` rather than being silently stamped. Validation
    runs before the INSERT, so an invalid row aborts the whole call.
    """
    rows: list[dict[str, object]] = []
    for q in qualities:
        if q.fetched_at is None:
            raise ValueError(
                f"Quality for {q.provider}/{q.model} is missing fetched_at; "
                f"the column is NOT NULL in quality_snapshot"
            )
        rows.append(
            {
                "provider": q.provider,
                "model": q.model,
                "quality_score": q.quality_score,
                "fetched_at": q.fetched_at,
            }
        )
    if not rows:
        return 0

    stmt = sqlite_insert(QualitySnapshot).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["provider", "model"],
        set_={
            "quality_score": stmt.excluded.quality_score,
            "fetched_at": stmt.excluded.fetched_at,
        },
    )
    session.execute(stmt)
    return len(rows)
