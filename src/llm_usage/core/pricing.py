"""Pricing data and cost calculation.

The DB schema follows Anthropic-style cache semantics: `input_tokens`,
`cache_write_tokens`, and `cache_read_tokens` are disjoint counts, each
billed at its own rate. Capture adapters normalize provider-specific
shapes (OpenAI's `cached_tokens` subset, DeepSeek's hit/miss partition)
into this form at write time, so by the time `CostCalculator` runs it's
plain arithmetic.

Cost is computed and stored in **nano-USD** (10^-9 USD) as `int`. This
gives exact aggregate arithmetic with vast headroom (INT64 caps ~$9.2 B)
and resolution finer than any provider's smallest billable token unit.
The MCP layer converts to float USD at the API boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from llm_usage.core.db.models import PricingSnapshot, PricingTier

# 1 USD = 1e9 nano-USD; rates are per-million-USD, so per-token cost in
# nano-USD = tokens * rate * (1e9 / 1e6) = tokens * rate * 1_000.
_NANO_PER_MILLION_USD = 1_000

_NANO_PER_USD = 1_000_000_000


def usd_to_nano(usd: float) -> int:
    """Convert float USD to integer nano-USD with banker's rounding.

    The MCP-tool boundary accepts float USD (per spec) but storage is
    integer nano-USD. Use this when crossing in from the float side
    (e.g., a future budget threshold the user types in dollars).
    """
    return round(usd * _NANO_PER_USD)


def nano_to_usd(nano: int) -> float:
    """Convert integer nano-USD to float USD.

    Pair with `usd_to_nano` at the MCP-tool boundary. The spec mandates
    that all dollar amounts surfaced to agents are float USD — this is
    the single conversion point so callers never roll their own `/1e9`.
    """
    return nano / _NANO_PER_USD


@dataclass(frozen=True)
class Tier:
    """One bracket of a model's prompt-size-tiered pricing schedule.

    Range is `[range_start, range_end)` in prompt tokens. `tier_index`
    preserves the order LiteLLM emits tiers in (0, 1, 2, …) so a
    later lookup-by-prompt-size can iterate deterministically. The
    rates here are per-million-USD, matching `Pricing` — units are
    converted from per-token at load time in `pricing_loader.py`.

    Dataclass mirror of the `PricingTier` ORM row. Sits inside
    `Pricing.tiers` for models that carry tiered pricing; the empty
    tuple for everything else. v1 cost code (PR1's scope) ignores
    this field — it's persisted by the loader/upsert path so a later
    slice can switch the cost calculator to pick a tier based on
    prompt size.
    """

    tier_index: int
    range_start: int
    range_end: int
    input_per_million_usd: float
    output_per_million_usd: float

    @classmethod
    def from_orm(cls, row: PricingTier) -> Tier:
        return cls(
            tier_index=row.tier_index,
            range_start=row.range_start,
            range_end=row.range_end,
            input_per_million_usd=row.input_per_million_usd,
            output_per_million_usd=row.output_per_million_usd,
        )


@dataclass(frozen=True)
class Pricing:
    """USD-per-million-token rates for one (provider, model).

    Rates remain `float` because pricing JSON often carries decimals (e.g.,
    Anthropic cache_read at $0.30/M). Cost calculation converts to integer
    nano-USD only at the end.

    `cache_*_per_million_usd` is `None` for providers/models that don't
    bill caching as a separate line item (e.g., Qwen pre-2026, OpenAI's
    write-side which is absorbed into input). The corresponding token
    column on `usage_events` should be `0` in those cases; if it isn't,
    `CostCalculator` raises rather than silently zeroing the contribution.

    `tiers` is the per-prompt-size pricing schedule for models that
    carry `tiered_pricing` in LiteLLM's JSON; an empty tuple for the
    flat-rate majority. The tier-0 rate is *also* written into
    `input_per_million_usd` / `output_per_million_usd` so cost code
    that doesn't (yet) know about tiers keeps working — tiers are the
    additive payload, not a replacement.
    """

    provider: str
    model: str
    input_per_million_usd: float
    output_per_million_usd: float
    cache_write_per_million_usd: float | None = None
    cache_read_per_million_usd: float | None = None
    fetched_at: int | None = None
    tiers: tuple[Tier, ...] = field(default_factory=tuple)

    @classmethod
    def from_orm(cls, row: PricingSnapshot) -> Pricing:
        # Reads only the flat row — `tiers` stays at the default empty
        # tuple. PR1 doesn't add a read-path for tiers because no
        # consumer needs them yet; the tier-aware cost calculator
        # (PR2) will join `pricing_tier` separately.
        return cls(
            provider=row.provider,
            model=row.model,
            input_per_million_usd=row.input_per_million_usd,
            output_per_million_usd=row.output_per_million_usd,
            cache_write_per_million_usd=row.cache_write_per_million_usd,
            cache_read_per_million_usd=row.cache_read_per_million_usd,
            fetched_at=row.fetched_at,
        )


class CostCalculator:
    """Compute nano-USD cost for one LLM call.

    Bound to a single `Pricing` instance — construct one calculator per
    (provider, model) you're billing for. The math is simple; the value
    here is the validation: cache tokens with a missing rate raise
    `ValueError` (instead of silently rounding to zero), and negative
    token counts raise.
    """

    def __init__(self, pricing: Pricing) -> None:
        self._pricing = pricing

    @property
    def pricing(self) -> Pricing:
        return self._pricing

    def cost_nano_usd(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> int:
        for name, value in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("cache_write_tokens", cache_write_tokens),
            ("cache_read_tokens", cache_read_tokens),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative, got {value}")

        cost_micro = (
            input_tokens * self._pricing.input_per_million_usd
            + output_tokens * self._pricing.output_per_million_usd
            + self._cache_contribution(
                cache_write_tokens,
                self._pricing.cache_write_per_million_usd,
                "cache_write",
            )
            + self._cache_contribution(
                cache_read_tokens,
                self._pricing.cache_read_per_million_usd,
                "cache_read",
            )
        )
        # `cost_micro` is at this point `tokens * per_million_usd` summed;
        # multiplying by _NANO_PER_MILLION_USD (=1_000) converts the
        # per-million-USD rate into per-token nano-USD. round() banker-
        # rounds, which is the standard for money.
        return round(cost_micro * _NANO_PER_MILLION_USD)

    def _cache_contribution(self, tokens: int, rate: float | None, label: str) -> float:
        if tokens == 0:
            return 0.0
        if rate is None:
            raise ValueError(
                f"{tokens} {label}_tokens recorded but "
                f"{self._pricing.provider}/{self._pricing.model} has no "
                f"{label}_per_million_usd in pricing"
            )
        return tokens * rate


def get_pricing(session: Session, provider: str, model: str) -> Pricing | None:
    """Look up pricing for a (provider, model) in `pricing_snapshot`.

    Returns `None` if the model isn't in the table — the MCP layer's
    `record_usage` tool surfaces this as a `warning` and stores cost = 0
    rather than failing the recording.
    """
    row = session.get(PricingSnapshot, (provider, model))
    if row is None:
        return None
    return Pricing.from_orm(row)


def all_pricing(session: Session) -> list[Pricing]:
    """Return every `pricing_snapshot` row as a `Pricing`, sorted.

    The "get all" sibling of `get_pricing`. Order is stable
    (provider, model) so cost-projection callers (`compare_providers`,
    later `recommend_provider`) get deterministic tie-breaking when two
    models project to the same cost.
    """
    stmt = select(PricingSnapshot).order_by(PricingSnapshot.provider, PricingSnapshot.model)
    return [Pricing.from_orm(row) for row in session.scalars(stmt).all()]


def upsert_pricing(session: Session, pricings: Iterable[Pricing]) -> int:
    """Idempotently write `Pricing` records into `pricing_snapshot` (+ tiers).

    Uses SQLite's `INSERT ... ON CONFLICT (provider, model) DO UPDATE` so
    re-running with the same input refreshes `fetched_at` and any changed
    rates without producing duplicate rows. Returns the count of input
    records processed (insert + update combined). Does not commit — the
    caller owns the transaction, matching `get_pricing`'s convention.

    `fetched_at` is required (the column is NOT NULL); a `None` value
    surfaces as `ValueError` rather than being silently stamped with
    `now`. Validation runs before the INSERT, so an invalid row in the
    batch aborts the whole call (no partial write).

    Tier handling (snapshot semantics): for every (provider, model) in
    the batch, existing `pricing_tier` rows are deleted before the new
    `Pricing.tiers` are inserted. So:
      - models with tiers in the input get fresh tier rows;
      - models that had tiers before but don't now lose their old
        rows (the new snapshot wins);
      - models with no tiers in either side stay zero-tier.
    This keeps `pricing_tier` consistent with the snapshot without
    needing a foreign key (SQLite doesn't enforce FKs by default).
    """
    pricings_list = list(pricings)
    rows: list[dict[str, object]] = []
    tier_rows: list[dict[str, object]] = []
    for p in pricings_list:
        if p.fetched_at is None:
            raise ValueError(
                f"Pricing for {p.provider}/{p.model} is missing fetched_at; "
                f"the column is NOT NULL in pricing_snapshot"
            )
        rows.append(
            {
                "provider": p.provider,
                "model": p.model,
                "input_per_million_usd": p.input_per_million_usd,
                "output_per_million_usd": p.output_per_million_usd,
                "cache_write_per_million_usd": p.cache_write_per_million_usd,
                "cache_read_per_million_usd": p.cache_read_per_million_usd,
                "fetched_at": p.fetched_at,
            }
        )
        for tier in p.tiers:
            tier_rows.append(
                {
                    "provider": p.provider,
                    "model": p.model,
                    "tier_index": tier.tier_index,
                    "range_start": tier.range_start,
                    "range_end": tier.range_end,
                    "input_per_million_usd": tier.input_per_million_usd,
                    "output_per_million_usd": tier.output_per_million_usd,
                    "fetched_at": p.fetched_at,
                }
            )
    if not rows:
        return 0

    stmt = sqlite_insert(PricingSnapshot).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["provider", "model"],
        set_={
            "input_per_million_usd": stmt.excluded.input_per_million_usd,
            "output_per_million_usd": stmt.excluded.output_per_million_usd,
            "cache_write_per_million_usd": stmt.excluded.cache_write_per_million_usd,
            "cache_read_per_million_usd": stmt.excluded.cache_read_per_million_usd,
            "fetched_at": stmt.excluded.fetched_at,
        },
    )
    session.execute(stmt)

    # Tier-row reconciliation: clear existing tier rows for every
    # (provider, model) in this batch — even those without new tiers
    # — so a model that *used to* have tiered_pricing and no longer
    # does loses its stale rows. Then insert whatever the new batch
    # carries. One DELETE per upserted key + one INSERT total. For
    # the ~180-model full refresh this is sub-millisecond; the
    # alternative (a single tuple_().in_(...) DELETE) trades a
    # marginally faster path for less-portable SQL.
    for p in pricings_list:
        session.execute(
            delete(PricingTier).where(
                PricingTier.provider == p.provider,
                PricingTier.model == p.model,
            )
        )
    if tier_rows:
        session.execute(sqlite_insert(PricingTier).values(tier_rows))

    return len(rows)
