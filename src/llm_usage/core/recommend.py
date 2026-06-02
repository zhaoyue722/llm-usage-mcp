"""Recommend the cheapest priced model for a workload + budget.

Shared between the `recommend_provider` MCP tool (`mcp/server.py`)
and the `llm-usage recommend` CLI (`cli.py`). Both surfaces ask the
same question — "given this workload and (optional) budget, what's
the cheapest priced model?" — and want the same answer.

v1 ranks by cost only. The selection logic is intentionally simple:
project each priced model's cost for the workload, drop anything that
breaks the budget, pick the cheapest of what's left. When nothing
fits, fall back to the cheapest overall — a wrong-but-explained
recommendation is more useful than a hard error. Quality scoring
(the `quality_snapshot` table) is reserved for a post-v1 release;
until then, the API stays cost-only and honest.

Design notes pinned here:

- The `reasoning` field is **templated**, not LLM-generated. The
  contract: surface what was assumed (workload defaults, budget
  constraints, that v1 ranks by cost only) and which model was
  chosen, so the caller can sanity-check the recommendation.
- `task_description` is echoed verbatim into the reasoning but
  doesn't drive selection. The tool isn't an LLM and can't interpret
  free text.
- `tokens_flag_names` parametrizes the "specify ___ for a precise
  estimate" phrase so MCP and CLI emit advice that matches their
  respective surfaces (`expected_input_tokens / expected_output_tokens`
  for the MCP tool params, `--in / --out` for the CLI flags).
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.orm import Session

from llm_usage.core.models import RecommendProviderResult
from llm_usage.core.pricing import CostCalculator, Pricing, all_pricing, nano_to_usd

# Workload assumed when the caller doesn't pass `expected_input_tokens`
# / `expected_output_tokens`. 1k/1k is a generic chat-style turn — large
# enough that pricing differences show up in dollars, small enough not
# to bias toward cheap models on the basis of the assumption alone. The
# reasoning string flags when the defaults are in use so the caller
# knows the estimate is nominal.
NOMINAL_INPUT_TOKENS: Final[int] = 1_000
NOMINAL_OUTPUT_TOKENS: Final[int] = 1_000

# Default flag-name pair used in the `reasoning` template when the
# caller doesn't override it. Matches the MCP tool's parameter names.
# The CLI passes `("--in", "--out")` so the reasoning suggests CLI
# flags rather than Python parameter names.
_DEFAULT_TOKEN_FLAGS: Final[tuple[str, str]] = (
    "expected_input_tokens",
    "expected_output_tokens",
)


def recommend(
    session: Session,
    *,
    task_description: str,
    expected_input_tokens: int | None = None,
    expected_output_tokens: int | None = None,
    budget_usd: float | None = None,
    tokens_flag_names: tuple[str, str] = _DEFAULT_TOKEN_FLAGS,
) -> RecommendProviderResult:
    """Pick the cheapest priced model that fits `budget_usd` (if any).

    Raises `ValueError` when `pricing_snapshot` is empty — the result
    schema's required fields can't be filled, and the caller's bug is
    almost certainly "DB not bootstrapped" rather than a quirk of the
    workload. The MCP and CLI wrappers translate this into their
    respective error-presentation idioms.
    """
    input_tokens = (
        expected_input_tokens if expected_input_tokens is not None else NOMINAL_INPUT_TOKENS
    )
    output_tokens = (
        expected_output_tokens if expected_output_tokens is not None else NOMINAL_OUTPUT_TOKENS
    )
    tokens_defaulted = expected_input_tokens is None or expected_output_tokens is None

    projected = _project_costs(session, input_tokens=input_tokens, output_tokens=output_tokens)
    if not projected:
        raise ValueError(
            "no priced models available to recommend from; "
            "is the database bootstrapped? (`pricing_snapshot` is empty)"
        )

    if budget_usd is not None:
        affordable = [pair for pair in projected if nano_to_usd(pair[1]) <= budget_usd]
    else:
        affordable = projected
    over_budget = budget_usd is not None and not affordable

    # When over budget, fall back to the cheapest available overall —
    # "your budget is too low, here's the closest" is more useful than
    # raising. The reasoning makes the situation explicit.
    chosen_pricing, chosen_cost_nano = (projected if over_budget else affordable)[0]

    reasoning = _build_reasoning(
        task_description=task_description,
        chosen_pricing=chosen_pricing,
        chosen_cost_nano=chosen_cost_nano,
        pool_size=len(projected if over_budget else affordable),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tokens_defaulted=tokens_defaulted,
        tokens_flag_names=tokens_flag_names,
        budget_usd=budget_usd,
        over_budget=over_budget,
    )
    return RecommendProviderResult(
        provider=chosen_pricing.provider,
        model=chosen_pricing.model,
        estimated_cost_usd=nano_to_usd(chosen_cost_nano),
        reasoning=reasoning,
    )


def _project_costs(
    session: Session, *, input_tokens: int, output_tokens: int
) -> list[tuple[Pricing, int]]:
    """Project each priced model's cost for the workload, cheapest first.

    `all_pricing` returns rows pre-sorted by (provider, model), and
    Python's sort is stable, so cost-equal models break alphabetically.
    """
    pricings = all_pricing(session)
    projected = [
        (
            p,
            CostCalculator(p).cost_nano_usd(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        )
        for p in pricings
    ]
    projected.sort(key=lambda pair: pair[1])
    return projected


def _build_reasoning(
    *,
    task_description: str,
    chosen_pricing: Pricing,
    chosen_cost_nano: int,
    pool_size: int,
    input_tokens: int,
    output_tokens: int,
    tokens_defaulted: bool,
    tokens_flag_names: tuple[str, str],
    budget_usd: float | None,
    over_budget: bool,
) -> str:
    """Natural-language explanation of the recommendation."""
    cost_usd = nano_to_usd(chosen_cost_nano)
    workload = f"{input_tokens:,} input / {output_tokens:,} output tokens"
    if tokens_defaulted:
        in_flag, out_flag = tokens_flag_names
        workload += f" (nominal defaults — specify {in_flag} / {out_flag} for a precise estimate)"

    chosen_name = f"{chosen_pricing.provider}/{chosen_pricing.model}"

    if over_budget:
        assert budget_usd is not None
        return (
            f"For task {task_description!r}: no priced model fits a "
            f"${budget_usd:.4f} budget at {workload}. Recommending the "
            f"cheapest available, {chosen_name} (estimated ${cost_usd:.4f}). "
            f"Raise the budget or narrow the workload to fit."
        )

    budget_note = f" within a ${budget_usd:.4f} budget" if budget_usd is not None else ""
    return (
        f"For task {task_description!r}{budget_note}: recommending {chosen_name} — "
        f"the cheapest projected cost among {pool_size} priced model(s). Estimated "
        f"${cost_usd:.4f} for {workload}. v1 ranks by cost only; "
        f"task_description is echoed for context but does not drive selection."
    )


__all__ = [
    "NOMINAL_INPUT_TOKENS",
    "NOMINAL_OUTPUT_TOKENS",
    "recommend",
]
