"""MCP server: 7 tools + 2 resources, all stubs raising `NotImplementedError`.

This module wires up the JSON-RPC surface defined in `docs/spec.md` using
the official `mcp[cli]` SDK's `FastMCP`. Every tool's signature mirrors
the corresponding Pydantic params model in `core/models.py` (flat
parameters, per FastMCP's idiom — passing a single Pydantic model would
wrap the schema in a redundant `{"params": {...}}` envelope), and every
tool's return type is the matching `*Result` model so the structured-
output JSON schema is generated automatically.

Tool bodies raise `NotImplementedError(f"<name> stub")`. The schemas
they expose are real and locked, so MCP clients (Claude Code, Cursor)
can discover the surface today; behavior gets wired up tool-by-tool in
follow-up changes.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from llm_usage.core.models import (
    CompareProvidersResult,
    GetPricingResult,
    GroupBy,
    ListProvidersResult,
    Period,
    QualityPriority,
    QuerySpendResult,
    RecommendProviderResult,
    RecordUsageResult,
    SpendFilter,
    TaskType,
    UsageSummaryResult,
)

server: FastMCP = FastMCP(name="llm-usage")


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
    """Return current pricing for one model, one provider, or all models."""
    raise NotImplementedError("get_pricing stub")


@server.tool()
async def usage_summary(period: Period = "week") -> UsageSummaryResult:
    """Return a one-shot summary of usage over `period`.

    Period is one of today | week | month | year; default is "week".
    """
    raise NotImplementedError("usage_summary stub")


@server.tool()
async def list_providers() -> ListProvidersResult:
    """List every provider we know about, with their models and OpenAI-compat flag."""
    raise NotImplementedError("list_providers stub")


# --- resources -------------------------------------------------------------


@server.resource(
    "usage://recent_events",
    name="recent_events",
    description="Most recent LLM call events recorded by the local capture layer.",
    mime_type="application/json",
)
def recent_events() -> str:
    """Return a JSON array of the N most recent `usage_events` rows.

    Returned as text so MCP clients that don't render structured JSON
    still get something legible.
    """
    raise NotImplementedError("recent_events resource stub")


@server.resource(
    "usage://pricing_table",
    name="pricing_table",
    description="Current `pricing_snapshot` table — one row per (provider, model).",
    mime_type="application/json",
)
def pricing_table() -> str:
    """Return the materialized pricing_snapshot as a JSON array."""
    raise NotImplementedError("pricing_table resource stub")
