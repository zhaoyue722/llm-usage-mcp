"""Aggregate read paths over `usage_events`: spend rollups + summaries.

Backs the two MCP tools that read totals rather than per-event detail:

- `aggregate_spend()` -> the `query_spend` MCP tool — totals + grouping
  by one of `provider | model | project | tag | day` over a time window.
- `summarize_usage()` -> the `usage_summary` MCP tool — totals + top-N
  providers/models + the single largest call, scoped to a named
  calendar period (today / week / month / year).

Time is in milliseconds since the Unix epoch throughout the core; the
MCP layer parses ISO-8601 strings via `parse_iso_to_ms()` and resolves
"now" via `time.time()` before calling in. Pure helpers (`parse_iso_to_ms`,
`period_window`) are public to allow targeted unit tests without
roundtripping through SQLAlchemy.

Tag-grouping semantics (Option A from the design discussion): each
multi-tag event contributes once per tag; events with NULL tags are
excluded from the result entirely. Project-grouping is symmetric: NULL
projects are dropped. Per-row totals can exceed the top-level total
for `group_by="tag"` because multi-tag rows are inherently double-
counted across tag groups.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from llm_usage.core.db.models import UsageEvent
from llm_usage.core.models import (
    GroupBy,
    LargestCall,
    Period,
    QuerySpendResult,
    SpendFilter,
    SpendGroup,
    TopModel,
    TopProvider,
    UsageSummaryResult,
)
from llm_usage.core.pricing import nano_to_usd

_MS_PER_DAY: Final[int] = 86_400_000
# `query_spend.start` default per spec: "default: 30 days ago".
_DEFAULT_QUERY_LOOKBACK_MS: Final[int] = 30 * _MS_PER_DAY
# `usage_summary` top-N for `top_providers` / `top_models`. The spec
# leaves N unspecified; 3 is the smallest count that still shows tiers
# (leader / runner-up / context) without crowding the summary.
_TOP_N: Final[int] = 3


# --- public helpers --------------------------------------------------------


def parse_iso_to_ms(s: str) -> int:
    """Parse an ISO-8601 datetime string to milliseconds since the Unix epoch.

    Tolerates the trailing-`Z` UTC shorthand (`2026-05-15T00:00:00Z`),
    explicit `+00:00`, and naive forms (date-only `2026-05-15`, or
    timestamp without TZ). Naive inputs are interpreted as UTC — the
    project's time domain throughout — rather than the local zone,
    which would make MCP-tool results depend on where the server runs.
    """
    # `fromisoformat` in 3.11+ accepts `Z`; explicit shim for older
    # interpreters would go here, but the project pins >= 3.13.
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def period_window(period: Period, now_ms: int) -> tuple[int, int]:
    """Return `(start_ms, end_ms)` for a named period anchored at `now_ms`.

    Calendar boundaries in UTC: `today` is from 00:00 UTC of the current
    day; `week` from 00:00 UTC of the current ISO week's Monday; `month`
    from the 1st of the current month; `year` from January 1st. `end_ms`
    is always `now_ms`. Choice rationale (from the design discussion):
    calendar boundaries match how users mentally track spend — "this
    month" means "since the 1st," not "the last 30 days."
    """
    now = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    day_floor = now.replace(hour=0, minute=0, second=0, microsecond=0)
    match period:
        case "today":
            start = day_floor
        case "week":
            # Monday of the current ISO week. `weekday()` returns 0 for
            # Monday, so this floors to Monday 00:00 UTC regardless of
            # which day `now` falls on.
            start = day_floor - timedelta(days=now.weekday())
        case "month":
            start = day_floor.replace(day=1)
        case "year":
            start = day_floor.replace(month=1, day=1)
    return int(start.timestamp() * 1000), now_ms


# --- query_spend -----------------------------------------------------------


def aggregate_spend(
    session: Session,
    *,
    start_ms: int | None,
    end_ms: int | None,
    group_by: GroupBy,
    filter: SpendFilter | None,
    now_ms: int | None = None,
) -> QuerySpendResult:
    """Compute totals + per-group rollups for the `query_spend` MCP tool.

    `start_ms` defaults to 30 days before `end_ms`; `end_ms` defaults to
    `now_ms` (which itself defaults to wall-clock now). `filter` AND-
    combines any of provider/model/project equality predicates. Window
    is `[start_ms, end_ms)` — half-open so two adjacent windows tile
    without double-counting a boundary event.

    Group results are ordered by cost descending (largest spend first);
    ties break alphabetically on the key. Projects-of-NULL and tags-of-
    NULL are excluded (see module docstring).
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if end_ms is None:
        end_ms = now_ms
    if start_ms is None:
        start_ms = end_ms - _DEFAULT_QUERY_LOOKBACK_MS

    cost_nano, calls, in_tokens, out_tokens = _totals(session, start_ms, end_ms, filter)
    groups = _groups(session, start_ms, end_ms, filter, group_by)

    return QuerySpendResult(
        total_cost_usd=nano_to_usd(cost_nano),
        total_calls=calls,
        total_input_tokens=in_tokens,
        total_output_tokens=out_tokens,
        groups=groups,
    )


def _totals(
    session: Session,
    start_ms: int,
    end_ms: int,
    filter: SpendFilter | None,
) -> tuple[int, int, int, int]:
    """`(cost_nano, calls, input_tokens, output_tokens)` over the window.

    Returned as a plain tuple rather than a Pydantic shape because the
    caller is a single in-module function. `COALESCE(..., 0)` keeps the
    types as `int` even when the window has zero matching rows —
    `SUM` over no rows is `NULL` in SQL, which would otherwise leak a
    `None` into a result field typed as `int`.
    """
    stmt = select(
        func.coalesce(func.sum(UsageEvent.cost_nano_usd), 0),
        func.count(),
        func.coalesce(func.sum(UsageEvent.input_tokens), 0),
        func.coalesce(func.sum(UsageEvent.output_tokens), 0),
    )
    stmt = _apply_window_and_filter(stmt, start_ms, end_ms, filter)
    row = session.execute(stmt).one()
    return int(row[0]), int(row[1]), int(row[2]), int(row[3])


def _groups(
    session: Session,
    start_ms: int,
    end_ms: int,
    filter: SpendFilter | None,
    group_by: GroupBy,
) -> list[SpendGroup]:
    """Dispatch on `group_by` and return rollups sorted cost-desc."""
    if group_by == "tag":
        return _tag_groups(session, start_ms, end_ms, filter)
    return _grouped_by_column(session, start_ms, end_ms, filter, _key_column(group_by))


def _key_column(group_by: GroupBy) -> Any:
    """Map a non-tag `group_by` axis to the SQL expression to group on.

    Returns `Any` because SQLAlchemy column expressions don't share a
    single static type: mapped attributes (`UsageEvent.provider`) are
    `InstrumentedAttribute[str]`, while `func.date(...)` returns a
    `Function` / `Label`. Both quack alike at runtime (you can `.label`,
    `.is_not`, group on them) but mypy can't bridge them without an
    explicit `Any`.
    """
    match group_by:
        case "provider":
            return UsageEvent.provider
        case "model":
            return UsageEvent.model
        case "project":
            return UsageEvent.project
        case "day":
            # SQLite: `date(unix_seconds, 'unixepoch')` produces a
            # `YYYY-MM-DD` calendar-day key in UTC. Dividing by 1000
            # converts ms epoch -> s epoch as a float; SQLite truncates.
            return func.date(UsageEvent.timestamp / 1000, "unixepoch")
        case "tag":
            # Handled separately via `json_each` in `_tag_groups`.
            raise AssertionError("tag is handled by _tag_groups")


def _grouped_by_column(
    session: Session,
    start_ms: int,
    end_ms: int,
    filter: SpendFilter | None,
    key_col: Any,
) -> list[SpendGroup]:
    """Build + execute the per-group rollup query for any non-tag axis."""
    stmt = (
        select(
            key_col.label("key"),
            func.coalesce(func.sum(UsageEvent.cost_nano_usd), 0).label("cost"),
            func.count().label("calls"),
            func.coalesce(func.sum(UsageEvent.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageEvent.output_tokens), 0).label("output_tokens"),
        )
        .where(key_col.is_not(None))  # drop NULL projects (Option A symmetry)
        .group_by(key_col)
        .order_by(desc("cost"), "key")
    )
    stmt = _apply_window_and_filter(stmt, start_ms, end_ms, filter)
    rows = session.execute(stmt).all()
    return [
        SpendGroup(
            key=str(row.key),
            cost_usd=nano_to_usd(int(row.cost)),
            calls=int(row.calls),
            input_tokens=int(row.input_tokens),
            output_tokens=int(row.output_tokens),
        )
        for row in rows
    ]


def _tag_groups(
    session: Session,
    start_ms: int,
    end_ms: int,
    filter: SpendFilter | None,
) -> list[SpendGroup]:
    """Tag rollup via `json_each` — Option A: untagged events excluded.

    Multi-tag rows contribute once per tag, so per-tag `calls` sums can
    exceed the window's total call count. SQLite's `json_each` is a
    table-valued function that emits one row per JSON-array element;
    cross-joining with the prefiltered events produces (event, tag)
    pairs that group cleanly on `je.value`. Expressed as raw SQL since
    the SQLAlchemy ORM doesn't model TVFs cleanly.
    """
    sql = text("""
        WITH filtered AS (
            SELECT cost_nano_usd, input_tokens, output_tokens, tags
            FROM usage_events
            WHERE timestamp >= :start_ms
              AND timestamp <  :end_ms
              AND tags IS NOT NULL
              AND (:provider IS NULL OR provider = :provider)
              AND (:model    IS NULL OR model    = :model)
              AND (:project  IS NULL OR project  = :project)
        )
        SELECT
            je.value                                AS key,
            COALESCE(SUM(filtered.cost_nano_usd),0) AS cost,
            COUNT(*)                                AS calls,
            COALESCE(SUM(filtered.input_tokens),0)  AS input_tokens,
            COALESCE(SUM(filtered.output_tokens),0) AS output_tokens
        FROM filtered, json_each(filtered.tags) AS je
        GROUP BY je.value
        ORDER BY cost DESC, key ASC
    """)
    params = {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "provider": filter.provider if filter is not None else None,
        "model": filter.model if filter is not None else None,
        "project": filter.project if filter is not None else None,
    }
    rows = session.execute(sql, params).all()
    return [
        SpendGroup(
            key=str(row.key),
            cost_usd=nano_to_usd(int(row.cost)),
            calls=int(row.calls),
            input_tokens=int(row.input_tokens),
            output_tokens=int(row.output_tokens),
        )
        for row in rows
    ]


def _apply_window_and_filter[T: tuple[Any, ...]](
    stmt: Select[T],
    start_ms: int,
    end_ms: int,
    filter: SpendFilter | None,
) -> Select[T]:
    """AND-combine the time window and optional equality filters.

    Generic over the `Select`'s tuple shape so callers keep their more
    specific return types (`Select[tuple[int, int, int, int]]` from
    `_totals`, `Select[tuple[str, int, int, int, int]]` from
    `_grouped_by_column`) instead of being downgraded to
    `Select[tuple[object, ...]]`.
    """
    stmt = stmt.where(UsageEvent.timestamp >= start_ms, UsageEvent.timestamp < end_ms)
    if filter is None:
        return stmt
    if filter.provider is not None:
        stmt = stmt.where(UsageEvent.provider == filter.provider)
    if filter.model is not None:
        stmt = stmt.where(UsageEvent.model == filter.model)
    if filter.project is not None:
        stmt = stmt.where(UsageEvent.project == filter.project)
    return stmt


# --- usage_summary ---------------------------------------------------------


def summarize_usage(
    session: Session,
    *,
    period: Period,
    now_ms: int | None = None,
) -> UsageSummaryResult:
    """Compute the `usage_summary` MCP tool's result for a calendar period.

    Resolves `period` to a `[start_ms, now_ms)` window via
    `period_window`, then issues three reads: a totals query (single
    aggregate), a top-N rollup per axis (provider, model), and a
    single-row lookup for the largest call by cost. `largest_call` is
    `None` when the window has zero events — matches the result
    schema's `LargestCall | None` to keep `usage_summary` valid on a
    fresh DB.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    start_ms, end_ms = period_window(period, now_ms)

    total_cost_nano, call_count = _summary_totals(session, start_ms, end_ms)

    top_providers = [
        TopProvider(provider=key, cost_usd=cost_usd, pct=pct)
        for key, cost_usd, pct in _top_n_by(
            session, start_ms, end_ms, UsageEvent.provider, total_cost_nano
        )
    ]
    top_models = [
        TopModel(model=key, cost_usd=cost_usd, pct=pct)
        for key, cost_usd, pct in _top_n_by(
            session, start_ms, end_ms, UsageEvent.model, total_cost_nano
        )
    ]

    return UsageSummaryResult(
        period=period,
        total_cost_usd=nano_to_usd(total_cost_nano),
        call_count=call_count,
        top_providers=top_providers,
        top_models=top_models,
        largest_call=_largest_call(session, start_ms, end_ms),
    )


def _summary_totals(session: Session, start_ms: int, end_ms: int) -> tuple[int, int]:
    """`(total_cost_nano, call_count)` over the period — no filter axis."""
    row = session.execute(
        select(
            func.coalesce(func.sum(UsageEvent.cost_nano_usd), 0),
            func.count(),
        ).where(UsageEvent.timestamp >= start_ms, UsageEvent.timestamp < end_ms)
    ).one()
    return int(row[0]), int(row[1])


def _top_n_by(
    session: Session,
    start_ms: int,
    end_ms: int,
    key_col: Any,
    total_cost_nano: int,
) -> list[tuple[str, float, float]]:
    """Return up to `_TOP_N` `(key, cost_usd, pct)` rows, cost-desc.

    `pct` is the group's share of `total_cost_nano`, rounded to 2 dp.
    When the total is zero (empty window or only zero-cost rows), `pct`
    is 0.0 — no division-by-zero, and the value still type-checks.
    """
    stmt = (
        select(
            key_col.label("key"),
            func.coalesce(func.sum(UsageEvent.cost_nano_usd), 0).label("cost"),
        )
        .where(UsageEvent.timestamp >= start_ms, UsageEvent.timestamp < end_ms)
        .where(key_col.is_not(None))
        .group_by(key_col)
        .order_by(desc("cost"), "key")
        .limit(_TOP_N)
    )
    rows = session.execute(stmt).all()
    result: list[tuple[str, float, float]] = []
    for row in rows:
        cost_nano = int(row.cost)
        pct = round(cost_nano / total_cost_nano * 100, 2) if total_cost_nano else 0.0
        result.append((str(row.key), nano_to_usd(cost_nano), pct))
    return result


def _largest_call(session: Session, start_ms: int, end_ms: int) -> LargestCall | None:
    """The single highest-cost event in the window, or `None` if empty.

    Ties break on insertion order via `id ASC` so re-runs are
    deterministic. `cost_nano_usd` is a regular indexed column (the
    `usage_events` table doesn't index it explicitly), so this is a
    full window scan + sort — acceptable at the local-SQLite scale
    this product targets.
    """
    row = session.scalars(
        select(UsageEvent)
        .where(UsageEvent.timestamp >= start_ms, UsageEvent.timestamp < end_ms)
        .order_by(desc(UsageEvent.cost_nano_usd), UsageEvent.id)
        .limit(1)
    ).first()
    if row is None:
        return None
    return LargestCall(
        id=row.id,
        model=row.model,
        cost_usd=nano_to_usd(row.cost_nano_usd),
        timestamp=row.timestamp,
    )


__all__ = [
    "aggregate_spend",
    "parse_iso_to_ms",
    "period_window",
    "summarize_usage",
]
