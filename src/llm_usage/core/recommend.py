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

from llm_usage.core.models import Alternative, RecommendProviderResult
from llm_usage.core.pricing import CostCalculator, Pricing, all_pricing, nano_to_usd

# Workload assumed when the caller doesn't pass `expected_input_tokens`
# / `expected_output_tokens`. 1k/1k is a generic chat-style turn — large
# enough that pricing differences show up in dollars, small enough not
# to bias toward cheap models on the basis of the assumption alone. The
# reasoning string flags when the defaults are in use so the caller
# knows the estimate is nominal.
NOMINAL_INPUT_TOKENS: Final[int] = 1_000
NOMINAL_OUTPUT_TOKENS: Final[int] = 1_000

# How many runner-up candidates to surface as `alternatives` on the
# result. Two so the user sees 3 total options (chosen + 2 runner-ups)
# — matches the "top-3" pattern in `usage_summary` and is the canonical
# small-N humans handle well without scanning. Empty alternatives when
# the candidate pool has only one element.
_DEFAULT_ALTERNATIVES_COUNT: Final[int] = 2

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
    providers: list[str] | None = None,
    models: list[str] | None = None,
    tokens_flag_names: tuple[str, str] = _DEFAULT_TOKEN_FLAGS,
) -> RecommendProviderResult:
    """Pick the cheapest priced model that fits `budget_usd` (if any).

    `providers` and `models` are optional whitelists. Each is matched
    against the corresponding field on a `pricing_snapshot` row; both
    AND-combine when passed together (a candidate must be in both
    lists). Unknown names silently filter to nothing — consistent with
    the rest of the read surface ("unknown returns empty," not "unknown
    is an error"). The budget filter runs *after* the whitelist, so an
    over-budget fallback returns the cheapest within the whitelist
    rather than the cheapest priced model overall.

    Raises `ValueError` when:

    - `pricing_snapshot` is empty (the DB hasn't been bootstrapped), or
    - the `providers` / `models` filter leaves zero candidates.

    The two error messages are distinguishable so the caller's
    wrapper (MCP tool, CLI command) can surface a precise hint.
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

    # Apply whitelist filters before the budget cut so an over-budget
    # fallback returns the cheapest within the user's filter set, not
    # the cheapest priced model overall.
    candidates = _apply_filters(projected, providers=providers, models=models)
    if not candidates:
        raise ValueError(_no_candidates_message(providers, models))

    if budget_usd is not None:
        affordable = [pair for pair in candidates if nano_to_usd(pair[1]) <= budget_usd]
    else:
        affordable = candidates
    over_budget = budget_usd is not None and not affordable

    # Over budget: fall back to the cheapest among the filtered
    # candidates. "Your budget is too low, here's the closest" is more
    # useful than raising. The reasoning makes the situation explicit.
    pool = candidates if over_budget else affordable
    chosen_pricing, chosen_cost_nano = pool[0]
    # Take the next-cheapest entries from the *same pool* the chosen
    # row came from, so alternatives don't contradict the filter or
    # the budget fallback. Empty list when there's only one element
    # — the renderer uses this as a signal to skip the section.
    alternatives = [
        Alternative(
            provider=p.provider,
            model=p.model,
            estimated_cost_usd=nano_to_usd(cost_nano),
        )
        for p, cost_nano in pool[1 : 1 + _DEFAULT_ALTERNATIVES_COUNT]
    ]

    reasoning = _build_reasoning(
        task_description=task_description,
        chosen_pricing=chosen_pricing,
        chosen_cost_nano=chosen_cost_nano,
        pool_size=len(pool),
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
        alternatives=alternatives,
        reasoning=reasoning,
    )


def _apply_filters(
    projected: list[tuple[Pricing, int]],
    *,
    providers: list[str] | None,
    models: list[str] | None,
) -> list[tuple[Pricing, int]]:
    """Whitelist filter on `projected` rows, AND-combining the two axes.

    `None` on an axis means "don't filter on this axis" (vs an empty
    list, which would filter to nothing — the caller shouldn't pass an
    empty list, but if they do we honor it rather than silently
    rewriting it to "no filter").

    Provider matching is **case-insensitive**: provider names are a
    closed set with a well-known canonical case (lowercase in the DB,
    branded in display — `Qwen`, `DeepSeek`, `OpenAI`, `Anthropic`).
    A user typing the branded form they see in the output (`Qwen`)
    should match against the DB's lowercase row (`qwen`) without
    surprise.

    Model matching stays case-sensitive: model names are open catalog
    literals (e.g. `gpt-5-nano`, `claude-opus-4-7`), and case-folding
    them risks colliding two distinct catalog entries that differ
    only by case — unlikely with LiteLLM today, but the safer default.
    """
    if providers is None and models is None:
        return projected
    provider_set = {name.lower() for name in providers} if providers is not None else None
    model_set = set(models) if models is not None else None
    return [
        pair
        for pair in projected
        if (provider_set is None or pair[0].provider.lower() in provider_set)
        and (model_set is None or pair[0].model in model_set)
    ]


def _no_candidates_message(providers: list[str] | None, models: list[str] | None) -> str:
    """Error message when the whitelist filter empties the candidate pool.

    Lists exactly what was filtered so the caller can spot a typo'd
    provider name or model name without re-reading their command.
    """
    parts: list[str] = []
    if providers is not None:
        parts.append(f"providers={sorted(set(providers))}")
    if models is not None:
        parts.append(f"models={sorted(set(models))}")
    filter_repr = ", ".join(parts) if parts else "(no filters)"
    return (
        "no priced models match the recommend filter "
        f"({filter_repr}); check spelling or widen the filter."
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
