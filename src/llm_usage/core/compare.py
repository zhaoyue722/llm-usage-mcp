"""Project a hypothetical workload's cost across every priced model.

Shared between the `compare_providers` MCP tool (`mcp/server.py`) and
the `llm-usage compare` CLI (`cli.py`). Both surfaces ask the same
question — "given this many input/output tokens, what would each
priced model cost?" — and want the same ranking semantics: cost
ascending, alphabetical tie-break on (provider, model), `%` measured
against the cheapest.

Sourcing the projection from one place keeps the two surfaces honest:
when we eventually wire cache-token estimates or task-suitability
filters, both the MCP tool and the CLI gain the feature at the same
time, with the same tests covering the math.

## Family dedup (default-on)

LiteLLM's catalog often lists multiple variants of the same logical
model at identical prices — `gpt-5-mini` (alias) + `gpt-5-mini-2025-08-07`
(pinned snapshot), `qwen-turbo` + `qwen-turbo-latest`, etc. By default,
`project_costs` collapses rows that share both a **family root** (see
`core.pricing.family_root`) and an **identical cost** for the projected
workload, keeping the alphabetically-first member as the representative
and counting the rest into the kept row's `variant_count`. Set
`include_snapshots=True` to disable — every catalog row appears as its
own ranked entry with `variant_count=1`.

The dedup rule is gated on **both** family root and cost: when two
variants of the same model are listed at *different* prices (uncommon
but happens during snapshot promotions), both rows survive — they
answer different questions ("if I pin to this snapshot, my cost is
different").
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from llm_usage.core.models import CompareProvidersResult, RankedEntry
from llm_usage.core.pricing import (
    CostCalculator,
    Pricing,
    all_pricing,
    family_root,
    nano_to_usd,
)


def project_costs(
    session: Session,
    *,
    input_tokens: int,
    output_tokens: int,
    models: list[str] | None = None,
    include_snapshots: bool = False,
) -> CompareProvidersResult:
    """Rank every priced model by projected cost for the workload.

    `models`, when given, restricts the comparison to those model
    names. Cost is computed from `input_tokens` + `output_tokens` only
    — cache tokens are not estimated in v1 (`RankedEntry.notes` is
    reserved for future per-row caveats and stays `None`).

    `include_snapshots=False` (the default) collapses catalog rows that
    share `(family_root, cost)` so a user comparing distinct models
    doesn't see four `qwen-turbo` variants priced the same. The kept
    row carries `variant_count = N` (total catalog entries it
    represents); collapsed siblings are not surfaced individually but
    are visible via `include_snapshots=True`.

    Ranking is cheapest-first. `all_pricing()` returns rows pre-sorted
    by (provider, model), and Python's sort is stable, so cost-equal
    models break alphabetically. `relative_cost_pct` is each row's cost
    as a percentage of the cheapest row; cheapest is always 100%. When
    the workload projects to zero cost (both token counts zero), every
    row reports 100% rather than dividing by zero.
    """
    pricings = all_pricing(session)
    if models is not None:
        wanted = set(models)
        pricings = [p for p in pricings if p.model in wanted]

    projected = [
        (
            pricing,
            CostCalculator(pricing).cost_nano_usd(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        )
        for pricing in pricings
    ]
    projected.sort(key=lambda pair: pair[1])

    if not projected:
        return CompareProvidersResult(ranked=[])

    cheapest_nano = projected[0][1]

    if include_snapshots:
        ranked = [
            _ranked_entry(pricing, cost_nano, cheapest_nano, variant_count=1)
            for pricing, cost_nano in projected
        ]
    else:
        ranked = _dedup_by_family_and_cost(projected, cheapest_nano)

    return CompareProvidersResult(ranked=ranked)


def _dedup_by_family_and_cost(
    projected: list[tuple[Pricing, int]],
    cheapest_nano: int,
) -> list[RankedEntry]:
    """Collapse `(family_root, cost_nano)`-equivalent rows into one entry.

    Walks `projected` in its existing (cost-asc, alphabetical) order so
    the alphabetically-first member of each (family, cost) class is the
    one kept — for the common case of an alias + pinned snapshot tied
    on price, the alias wins because it's a lex prefix of the snapshot
    name. Subsequent same-(family, cost) rows bump the kept entry's
    `variant_count` instead of appending.

    Different prices within a family produce *separate* kept entries:
    e.g., `qwen-turbo $0.0003` and `qwen-turbo-2025-04-28 $0.0002` both
    survive, because the price divergence is meaningful information
    the user shouldn't have to opt-in to see.
    """
    # (family_root, cost_nano) -> index into `ranked`
    seen: dict[tuple[str, int], int] = {}
    ranked: list[RankedEntry] = []

    for pricing, cost_nano in projected:
        key = (family_root(pricing.model), cost_nano)
        if key in seen:
            # Same family, same cost — bump the representative's variant_count.
            kept = ranked[seen[key]]
            ranked[seen[key]] = kept.model_copy(update={"variant_count": kept.variant_count + 1})
            continue
        seen[key] = len(ranked)
        ranked.append(_ranked_entry(pricing, cost_nano, cheapest_nano, variant_count=1))
    return ranked


def _ranked_entry(
    pricing: Pricing,
    cost_nano: int,
    cheapest_nano: int,
    *,
    variant_count: int,
) -> RankedEntry:
    """Build a `RankedEntry` from one (pricing, cost) pair."""
    relative_pct = 100.0 if cheapest_nano == 0 else round(cost_nano / cheapest_nano * 100, 2)
    return RankedEntry(
        provider=pricing.provider,
        model=pricing.model,
        cost_usd=nano_to_usd(cost_nano),
        relative_cost_pct=relative_pct,
        notes=None,
        variant_count=variant_count,
    )


__all__ = ["project_costs"]
