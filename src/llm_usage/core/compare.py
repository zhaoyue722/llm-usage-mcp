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
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from llm_usage.core.models import CompareProvidersResult, RankedEntry
from llm_usage.core.pricing import CostCalculator, all_pricing, nano_to_usd


def project_costs(
    session: Session,
    *,
    input_tokens: int,
    output_tokens: int,
    models: list[str] | None = None,
) -> CompareProvidersResult:
    """Rank every priced model by projected cost for the workload.

    `models`, when given, restricts the comparison to those model
    names. Cost is computed from `input_tokens` + `output_tokens` only
    — cache tokens are not estimated in v1 (`compare_providers.notes`
    is reserved for future per-row caveats and stays `None`).

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

    ranked: list[RankedEntry] = []
    if projected:
        cheapest_nano = projected[0][1]
        for pricing, cost_nano in projected:
            relative_pct = (
                100.0 if cheapest_nano == 0 else round(cost_nano / cheapest_nano * 100, 2)
            )
            ranked.append(
                RankedEntry(
                    provider=pricing.provider,
                    model=pricing.model,
                    cost_usd=nano_to_usd(cost_nano),
                    relative_cost_pct=relative_pct,
                    notes=None,
                )
            )

    return CompareProvidersResult(ranked=ranked)


__all__ = ["project_costs"]
