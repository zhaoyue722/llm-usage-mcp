"""MCP server: 7 tools + 2 resources.

This module wires up the JSON-RPC surface defined in `docs/spec.md` using
the official `mcp[cli]` SDK's `FastMCP`. Every tool's signature mirrors
the corresponding Pydantic params model in `core/models.py` (flat
parameters, per FastMCP's idiom — passing a single Pydantic model would
wrap the schema in a redundant `{"params": {...}}` envelope), and every
tool's return type is the matching `*Result` model so the structured-
output JSON schema is generated automatically.

Status:

- **Wired up**: `record_usage` (write path), `list_providers`,
  `get_pricing`, `compare_providers`, `recommend_provider`,
  `usage://pricing_table`, `usage://recent_events`. These read/write
  the database that `bootstrap()` seeds on first run.
- **Stubs** (still raise `NotImplementedError`): `query_spend`,
  `usage_summary`. Their schemas are locked so MCP clients see the
  full surface; bodies land tool-by-tool in follow-up changes.
"""

from __future__ import annotations

import json
from typing import Any, Final

from mcp.server.fastmcp import FastMCP
from sqlalchemy import desc, select

from llm_usage.core.db.models import PricingSnapshot, UsageEvent
from llm_usage.core.db.session import get_session
from llm_usage.core.models import (
    CompareProvidersResult,
    GetPricingResult,
    GroupBy,
    ListProvidersResult,
    Period,
    PricingEntry,
    ProviderEntry,
    QuerySpendResult,
    RankedEntry,
    RecommendProviderResult,
    RecordUsageResult,
    SpendFilter,
    UsageSummaryResult,
)
from llm_usage.core.pricing import CostCalculator, Pricing, all_pricing, nano_to_usd
from llm_usage.core.recording import record_event

server: FastMCP = FastMCP(name="llm-usage")

# Static provider metadata. Anthropic uses its own `/v1/messages` shape;
# OpenAI, Qwen (via DashScope's compatible-mode endpoint), and DeepSeek
# all speak OpenAI's `/v1/chat/completions` wire format. This isn't user
# data, so it lives in code rather than in the DB.
_OPENAI_COMPATIBLE: Final[dict[str, bool]] = {
    "anthropic": False,
    "openai": True,
    "qwen": True,
    "deepseek": True,
}

# How many rows `usage://recent_events` returns. Spec leaves N unspecified;
# 50 is enough for "what just happened" without overwhelming a client that
# has to render the JSON inline in a chat message.
_RECENT_EVENTS_LIMIT: Final[int] = 50

# Workload assumed by `recommend_provider` when the caller doesn't pass
# `expected_input_tokens` / `expected_output_tokens`. 1k/1k is a
# generic chat-style turn — large enough that pricing differences show
# up in dollars, small enough not to bias toward cheap models on the
# basis of the assumption alone. The reasoning string flags when the
# defaults are in use so the caller knows the estimate is nominal.
_NOMINAL_INPUT_TOKENS: Final[int] = 1_000
_NOMINAL_OUTPUT_TOKENS: Final[int] = 1_000


def _query_pricing(provider: str | None, model: str | None) -> list[PricingEntry]:
    """Read `pricing_snapshot` rows matching the optional filters.

    Returns an empty list when nothing matches. Sort order is stable
    (provider, model) so callers and tests can rely on it. Shared by
    the `get_pricing` tool and the `usage://pricing_table` resource.
    """
    stmt = select(PricingSnapshot)
    if provider is not None:
        stmt = stmt.where(PricingSnapshot.provider == provider)
    if model is not None:
        stmt = stmt.where(PricingSnapshot.model == model)
    stmt = stmt.order_by(PricingSnapshot.provider, PricingSnapshot.model)

    with get_session() as session:
        rows = session.scalars(stmt).all()

    return [
        PricingEntry(
            provider=row.provider,
            model=row.model,
            input_per_million_usd=row.input_per_million_usd,
            output_per_million_usd=row.output_per_million_usd,
            cache_write_per_million_usd=row.cache_write_per_million_usd,
            cache_read_per_million_usd=row.cache_read_per_million_usd,
            fetched_at=row.fetched_at,
        )
        for row in rows
    ]


def _event_to_json(row: UsageEvent) -> dict[str, Any]:
    """Materialize one `usage_events` row as a JSON-serializable dict.

    `tags` and `metadata` are stored as JSON-encoded text in SQLite; they
    get parsed back into objects here so the resource is a single,
    well-formed JSON document (no string-inside-string). Both fields are
    nullable — preserve `None` rather than emitting `null` literals from
    a parse of an empty string.
    """
    return {
        "id": row.id,
        "timestamp": row.timestamp,
        "provider": row.provider,
        "model": row.model,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "cache_write_tokens": row.cache_write_tokens,
        "cache_read_tokens": row.cache_read_tokens,
        "cost_nano_usd": row.cost_nano_usd,
        "cost_usd": nano_to_usd(row.cost_nano_usd),
        "duration_ms": row.duration_ms,
        "success": row.success,
        "error_type": row.error_type,
        "request_id": row.request_id,
        "project": row.project,
        "tags": json.loads(row.tags) if row.tags else None,
        "metadata": json.loads(row.event_metadata) if row.event_metadata else None,
    }


def _project_costs(input_tokens: int, output_tokens: int) -> list[tuple[Pricing, int]]:
    """Project each priced model's cost for the workload, cheapest first.

    Read from `pricing_snapshot` only — v1's `recommend_provider` ranks
    by cost alone (a quality dimension requires real benchmark data
    that the v1 pricing-data layer does not have). `all_pricing` is
    pre-sorted by (provider, model), and Python's sort is stable, so
    cost-equal models break to the alphabetically-first one.
    """
    with get_session() as session:
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
    budget_usd: float | None,
    over_budget: bool,
) -> str:
    """Natural-language explanation of the recommendation.

    The reasoning isn't generated by an LLM — it's a template. The
    contract: surface what was assumed (workload defaults, budget
    constraints, that v1 ranks by cost only) and which model was
    chosen, so the caller can sanity-check the recommendation. The
    `task_description` is echoed verbatim; v1 does not interpret it.
    """
    cost_usd = nano_to_usd(chosen_cost_nano)
    workload = f"{input_tokens:,} input / {output_tokens:,} output tokens"
    if tokens_defaulted:
        workload += (
            " (nominal defaults — specify expected_input_tokens / "
            "expected_output_tokens for a precise estimate)"
        )

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


# --- tools -----------------------------------------------------------------


@server.tool()
async def record_usage(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    duration_ms: int | None = None,
    success: bool = True,
    error_type: str | None = None,
    request_id: str | None = None,
    project: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> RecordUsageResult:
    """Record a single LLM API call with token counts.

    Cost is computed automatically from the pricing table at insert time.
    `request_id` enables idempotent recording — replaying a log file
    won't double-count.
    """
    with get_session() as session:
        recorded = record_event(
            session,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_read_tokens=cache_read_tokens,
            duration_ms=duration_ms,
            success=success,
            error_type=error_type,
            request_id=request_id,
            project=project,
            tags=tags,
            metadata=metadata,
        )
        session.commit()
    return RecordUsageResult(
        id=recorded.id,
        cost_usd=nano_to_usd(recorded.cost_nano_usd),
        warning=recorded.warning,
    )


@server.tool()
async def query_spend(
    start: str | None = None,
    end: str | None = None,
    group_by: GroupBy = "provider",
    filter: SpendFilter | None = None,
) -> QuerySpendResult:
    """Return spending broken down by a chosen axis over a time window.

    `start` and `end` are ISO-8601 strings; default window is the last
    30 days. `group_by` is one of provider | model | project | tag | day.
    """
    raise NotImplementedError("query_spend stub")


@server.tool()
async def compare_providers(
    expected_input_tokens: int,
    expected_output_tokens: int,
    models: list[str] | None = None,
) -> CompareProvidersResult:
    """Project the cost of a hypothetical workload across providers/models.

    Returns models ranked by absolute cost ascending, with
    `relative_cost_pct` measured against the cheapest entry
    (cheapest = 100%). `models`, if given, restricts the comparison to
    those model names. Cost is computed from input/output tokens only;
    `RankedEntry.notes` is always `None` in v1 (the field is retained
    for future per-row caveats like "tiered pricing approximated").
    """
    with get_session() as session:
        pricings = all_pricing(session)

    if models is not None:
        wanted = set(models)
        pricings = [p for p in pricings if p.model in wanted]

    # Project each model's cost for the hypothetical workload. `pricings`
    # is already sorted by (provider, model), and Python's sort is stable,
    # so sorting by cost keeps that order as the tie-breaker.
    projected = [
        (
            pricing,
            CostCalculator(pricing).cost_nano_usd(
                input_tokens=expected_input_tokens,
                output_tokens=expected_output_tokens,
            ),
        )
        for pricing in pricings
    ]
    projected.sort(key=lambda pair: pair[1])

    ranked: list[RankedEntry] = []
    if projected:
        cheapest_nano = projected[0][1]
        for pricing, cost_nano in projected:
            # cheapest_nano is 0 only when the whole workload projects to
            # zero cost (e.g. both token counts are 0) — then every entry
            # is 0 too, so 100% is the correct relative figure for all.
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


@server.tool()
async def recommend_provider(
    task_description: str,
    expected_input_tokens: int | None = None,
    expected_output_tokens: int | None = None,
    budget_usd: float | None = None,
) -> RecommendProviderResult:
    """Recommend the cheapest priced model that fits the workload + budget.

    v1 ranks by cost only. A future release will incorporate quality
    benchmarks (see `quality_snapshot` — the table is reserved for that
    purpose) and accept a `quality_priority` axis; for v1 those would
    rely on data we don't yet have, so the surface stays cost-only and
    honest.

    `expected_input_tokens` / `expected_output_tokens` default to a
    nominal 1k/1k workload when absent; the `reasoning` notes when
    defaults are in use. `budget_usd`, when set, filters out models
    that exceed it — if nothing fits, falls back to the cheapest model
    overall (the result fields are required, so there's no "no match"
    return shape) and the `reasoning` says so plainly.
    `task_description` is echoed into the reasoning but does not drive
    selection — the tool isn't an LLM and can't interpret free text.
    """
    input_tokens = (
        expected_input_tokens if expected_input_tokens is not None else _NOMINAL_INPUT_TOKENS
    )
    output_tokens = (
        expected_output_tokens if expected_output_tokens is not None else _NOMINAL_OUTPUT_TOKENS
    )
    tokens_defaulted = expected_input_tokens is None or expected_output_tokens is None

    projected = _project_costs(input_tokens, output_tokens)
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
    # raising.
    chosen_pricing, chosen_cost_nano = (projected if over_budget else affordable)[0]

    reasoning = _build_reasoning(
        task_description=task_description,
        chosen_pricing=chosen_pricing,
        chosen_cost_nano=chosen_cost_nano,
        pool_size=len(projected if over_budget else affordable),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tokens_defaulted=tokens_defaulted,
        budget_usd=budget_usd,
        over_budget=over_budget,
    )
    return RecommendProviderResult(
        provider=chosen_pricing.provider,
        model=chosen_pricing.model,
        estimated_cost_usd=nano_to_usd(chosen_cost_nano),
        reasoning=reasoning,
    )


@server.tool()
async def get_pricing(
    provider: str | None = None,
    model: str | None = None,
) -> GetPricingResult:
    """Return current pricing for one model, one provider, or all models.

    Both filters are optional and AND-combined. An unknown
    (provider, model) returns an empty list rather than an error — the
    caller can distinguish "model not in our table" from "no model
    matches your filter" by passing `provider` alone.
    """
    return GetPricingResult(models=_query_pricing(provider, model))


@server.tool()
async def usage_summary(period: Period = "week") -> UsageSummaryResult:
    """Return a one-shot summary of usage over `period`.

    Period is one of today | week | month | year; default is "week".
    """
    raise NotImplementedError("usage_summary stub")


@server.tool()
async def list_providers() -> ListProvidersResult:
    """List every provider we know about, with their models and OpenAI-compat flag.

    Sources the provider/model lists from `pricing_snapshot`, so a
    provider whose pricing hasn't been seeded simply doesn't appear.
    After `bootstrap()` runs on a fresh install this includes every v1
    provider (anthropic, openai, qwen, deepseek). Order is alphabetical
    by provider, then by model within each provider.
    """
    stmt = select(PricingSnapshot.provider, PricingSnapshot.model).order_by(
        PricingSnapshot.provider, PricingSnapshot.model
    )
    with get_session() as session:
        rows = session.execute(stmt).all()

    by_provider: dict[str, list[str]] = {}
    for provider, model in rows:
        by_provider.setdefault(provider, []).append(model)

    return ListProvidersResult(
        providers=[
            ProviderEntry(
                name=provider,
                models=models,
                openai_compatible=_OPENAI_COMPATIBLE.get(provider, False),
            )
            for provider, models in sorted(by_provider.items())
        ]
    )


# --- resources -------------------------------------------------------------


@server.resource(
    "usage://recent_events",
    name="recent_events",
    description="Most recent LLM call events recorded by the local capture layer.",
    mime_type="application/json",
)
def recent_events() -> str:
    """Return a JSON array of the most recent `usage_events` rows.

    Latest first, capped at `_RECENT_EVENTS_LIMIT`. `cost_nano_usd` is
    preserved for exact arithmetic; `cost_usd` is added alongside as
    a float for human consumption. Returned as text so MCP clients that
    don't render structured JSON still get something legible.
    """
    stmt = select(UsageEvent).order_by(desc(UsageEvent.timestamp)).limit(_RECENT_EVENTS_LIMIT)
    with get_session() as session:
        rows = session.scalars(stmt).all()

    return json.dumps([_event_to_json(row) for row in rows], indent=2)


@server.resource(
    "usage://pricing_table",
    name="pricing_table",
    description="Current `pricing_snapshot` table — one row per (provider, model).",
    mime_type="application/json",
)
def pricing_table() -> str:
    """Return the materialized pricing_snapshot as a JSON array."""
    entries = _query_pricing(provider=None, model=None)
    return json.dumps([e.model_dump() for e in entries], indent=2)
