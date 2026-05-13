"""MCP server: 7 tools + 2 resources.

This module wires up the JSON-RPC surface defined in `docs/spec.md` using
the official `mcp[cli]` SDK's `FastMCP`. Every tool's signature mirrors
the corresponding Pydantic params model in `core/models.py` (flat
parameters, per FastMCP's idiom — passing a single Pydantic model would
wrap the schema in a redundant `{"params": {...}}` envelope), and every
tool's return type is the matching `*Result` model so the structured-
output JSON schema is generated automatically.

Status:

- **Wired up** (read-only paths): `list_providers`, `get_pricing`,
  `usage://pricing_table`, `usage://recent_events`. These query the
  database that `bootstrap()` seeds on first run.
- **Stubs** (still raise `NotImplementedError`): `record_usage`,
  `query_spend`, `compare_providers`, `recommend_provider`,
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
    QualityPriority,
    QuerySpendResult,
    RecommendProviderResult,
    RecordUsageResult,
    SpendFilter,
    TaskType,
    UsageSummaryResult,
)
from llm_usage.core.pricing import nano_to_usd

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
    raise NotImplementedError("record_usage stub")


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
    task_type: TaskType | None = None,
    models: list[str] | None = None,
    include_cached_estimate: bool = False,
) -> CompareProvidersResult:
    """Project the cost of a hypothetical workload across providers/models.

    Returns models ranked by absolute cost, with `relative_cost_pct`
    relative to the cheapest entry (cheapest = 100%).
    """
    raise NotImplementedError("compare_providers stub")


@server.tool()
async def recommend_provider(
    task_description: str,
    expected_input_tokens: int | None = None,
    expected_output_tokens: int | None = None,
    budget_usd: float | None = None,
    quality_priority: QualityPriority | None = None,
) -> RecommendProviderResult:
    """Recommend a single provider/model for a task given priorities.

    `quality_priority` is one of lowest_cost | balanced | highest_quality
    and defaults to balanced semantics on the implementation side.
    """
    raise NotImplementedError("recommend_provider stub")


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
