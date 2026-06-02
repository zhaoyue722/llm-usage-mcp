"""MCP server: 7 tools + 2 resources, all wired.

This module wires up the JSON-RPC surface defined in `docs/spec.md` using
the official `mcp[cli]` SDK's `FastMCP`. Every tool's signature mirrors
the corresponding Pydantic params model in `core/models.py` (flat
parameters, per FastMCP's idiom — passing a single Pydantic model would
wrap the schema in a redundant `{"params": {...}}` envelope), and every
tool's return type is the matching `*Result` model so the structured-
output JSON schema is generated automatically.

Tool bodies stay thin: this layer is responsible for I/O concerns
(session acquisition, ISO-8601 parsing for `query_spend`'s window
strings) while the domain logic lives in `core/` modules — pricing
math in `core/pricing.py`, write-path in `core/recording.py`, aggregate
reads in `core/spend.py`, projection in `core/compare.py`, and
recommendation in `core/recommend.py` (the latter two are shared with
the `llm-usage compare` / `llm-usage recommend` CLI commands so MCP
and CLI stay in lockstep on ranking semantics).
"""

from __future__ import annotations

import json
from typing import Any, Final

from mcp.server.fastmcp import FastMCP
from sqlalchemy import desc, select

from llm_usage.core.compare import project_costs as _project_compare
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
    RecommendProviderResult,
    RecordUsageResult,
    SpendFilter,
    UsageSummaryResult,
)
from llm_usage.core.pricing import nano_to_usd, query_pricing
from llm_usage.core.providers import OPENAI_COMPATIBLE
from llm_usage.core.recommend import recommend as _recommend
from llm_usage.core.recording import record_event
from llm_usage.core.spend import (
    aggregate_spend,
    parse_iso_to_ms,
    summarize_usage,
)

server: FastMCP = FastMCP(name="llm-usage")

# How many rows `usage://recent_events` returns. Spec leaves N unspecified;
# 50 is enough for "what just happened" without overwhelming a client that
# has to render the JSON inline in a chat message.
_RECENT_EVENTS_LIMIT: Final[int] = 50


def _query_pricing(provider: str | None, model: str | None) -> list[PricingEntry]:
    """Thin wrapper around `core.pricing.query_pricing` for the MCP tool.

    Keeps the MCP `get_pricing` tool's external contract identical
    (single-value `provider` / `model` params) while delegating the
    actual SQL to the shared core helper that the `llm-usage models`
    CLI also uses.
    """
    with get_session() as session:
        return query_pricing(
            session,
            providers=[provider] if provider is not None else None,
            models=[model] if model is not None else None,
        )


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
    include_failed: bool = False,
) -> QuerySpendResult:
    """Return spending broken down by a chosen axis over a time window.

    `start` and `end` are ISO-8601 strings (trailing-`Z`, `+00:00`, or
    naive — naive is interpreted as UTC). Default window is the last
    30 days. `group_by` is one of provider | model | project | tag | day.
    `filter` AND-combines optional provider/model/project equality
    predicates.

    `include_failed` defaults to `False` so failure rows (e.g. streams
    that died mid-flight with partial counts) are excluded from totals
    and groups. Pass `True` to fold them back in — useful for debugging
    capture-layer behavior, not for honest spend numbers.

    Tag semantics: events with NULL/empty tags are excluded from
    `group_by="tag"` results entirely; multi-tag events contribute once
    per tag (so per-group `calls` sums can exceed the window total).
    Project semantics are symmetric: NULL projects are dropped from
    `group_by="project"`. Groups are ordered cost-desc with
    alphabetical ties.
    """
    start_ms = parse_iso_to_ms(start) if start is not None else None
    end_ms = parse_iso_to_ms(end) if end is not None else None
    with get_session() as session:
        return aggregate_spend(
            session,
            start_ms=start_ms,
            end_ms=end_ms,
            group_by=group_by,
            filter=filter,
            include_failed=include_failed,
        )


@server.tool()
async def compare_providers(
    expected_input_tokens: int,
    expected_output_tokens: int,
    models: list[str] | None = None,
    include_snapshots: bool = False,
) -> CompareProvidersResult:
    """Project the cost of a hypothetical workload across providers/models.

    Returns models ranked by absolute cost ascending, with
    `relative_cost_pct` measured against the cheapest entry
    (cheapest = 100%). `models`, if given, restricts the comparison to
    those model names. Cost is computed from input/output tokens only;
    `RankedEntry.notes` is always `None` in v1 (the field is retained
    for future per-row caveats like "tiered pricing approximated").

    `include_snapshots=False` (the default) family-dedups the ranked
    list: rows sharing both a model-family root (`gpt-5-mini` ↔
    `gpt-5-mini-2025-08-07`) AND an identical projected cost collapse
    to one representative, with `RankedEntry.variant_count` recording
    how many catalog rows the entry stands for. Set
    `include_snapshots=True` to see every catalog row (each with
    `variant_count=1`) — useful when comparing snapshot-by-snapshot
    pricing for production pinning.
    """
    with get_session() as session:
        return _project_compare(
            session,
            input_tokens=expected_input_tokens,
            output_tokens=expected_output_tokens,
            models=models,
            include_snapshots=include_snapshots,
        )


@server.tool()
async def recommend_provider(
    task_description: str | None = None,
    expected_input_tokens: int | None = None,
    expected_output_tokens: int | None = None,
    budget_usd: float | None = None,
    providers: list[str] | None = None,
    models: list[str] | None = None,
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

    `providers` / `models` are optional whitelists (AND-combine when
    both passed). Both are applied before the budget cut, so an over-
    budget fallback returns the cheapest within the filter set rather
    than the cheapest priced model overall. A whitelist that matches
    nothing raises rather than fabricating a result — likely a
    spelling error in the caller's name list.

    `task_description` is **optional** and echoed into the reasoning
    but does not drive selection — the tool isn't an LLM and can't
    interpret free text. Omit it (or pass `None`) and the reasoning
    opens with `"Recommending …"` instead of `"For task 'X': …"`.
    """
    with get_session() as session:
        return _recommend(
            session,
            task_description=task_description,
            expected_input_tokens=expected_input_tokens,
            expected_output_tokens=expected_output_tokens,
            budget_usd=budget_usd,
            providers=providers,
            models=models,
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
async def usage_summary(
    period: Period = "week",
    include_failed: bool = False,
) -> UsageSummaryResult:
    """Return a one-shot summary of usage over a named calendar period.

    `period` is one of today | week | month | year (default: "week").
    Boundaries are calendar UTC: `today` = since 00:00 UTC today,
    `week` = since Monday 00:00 UTC, `month` = since the 1st of the
    month, `year` = since January 1st. Returns totals, the top-3
    providers and top-3 models by cost (with `pct` of total), and the
    single most expensive call in the window — or `largest_call=None`
    when the window is empty.

    `include_failed` defaults to `False`: totals, top-N rollups, and
    `largest_call` all exclude `success=False` rows (partial-stream
    captures and other failure rows). Pass `True` for symmetric
    debugging access to the failure population.
    """
    with get_session() as session:
        return summarize_usage(session, period=period, include_failed=include_failed)


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
                openai_compatible=OPENAI_COMPATIBLE.get(provider, False),
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
