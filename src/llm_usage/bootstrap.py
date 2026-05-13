"""First-run schema migration and pricing materialization.

`main()` invokes `bootstrap()` before starting the MCP server. The two
steps are exposed separately so tests (and future tooling like an
`llm-usage init` CLI command) can run them in isolation.

- `migrate_to_head()` runs `alembic upgrade head` programmatically. The
  command is idempotent: at-head is a no-op, missing revisions get
  applied. We locate `alembic.ini` by walking up from this file until
  we find it — works for `uv run llm-usage-mcp` from a development
  checkout, which is the v1 distribution model. Packaging the
  migrations as package data is a follow-up for PyPI publishing
  (Day 12-13 in the action plan).
- `materialize_pricing_if_empty()` checks the `pricing_snapshot` row
  count; on a fresh DB it loads the vendored LiteLLM JSON via
  `load_vendored_pricing()` and idempotently upserts. On subsequent
  boots the table is non-empty and the call is a no-op. Returns the
  number of rows materialized so the caller can log it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.engine.url import make_url

from llm_usage.config import get_settings
from llm_usage.core.db.models import PricingSnapshot
from llm_usage.core.db.session import get_session
from llm_usage.core.pricing import upsert_pricing
from llm_usage.core.pricing_loader import load_vendored_pricing

logger = logging.getLogger(__name__)


def _find_alembic_root() -> Path:
    """Walk up from this module to find the repo root holding `alembic.ini`.

    Raises `RuntimeError` if no parent contains both `alembic.ini` and an
    `alembic/` directory — the standard layout. The error message points
    at the v1 distribution model (development checkout) so a future
    pip-installed user gets a clear signal that migrations weren't
    bundled.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "alembic.ini").is_file() and (parent / "alembic").is_dir():
            return parent
    raise RuntimeError(
        "alembic.ini not found in any parent directory of llm_usage. "
        "Run from a development checkout (`uv run llm-usage-mcp` in the "
        "repo), or wait for a release that bundles migrations as package "
        "data."
    )


def migrate_to_head() -> None:
    """Run `alembic upgrade head` against the configured DB. Idempotent."""
    root = _find_alembic_root()
    cfg = Config(str(root / "alembic.ini"))
    db_url = get_settings().db_url
    cfg.set_main_option("sqlalchemy.url", db_url)
    _ensure_sqlite_parent_dir(db_url)
    command.upgrade(cfg, "head")


def _ensure_sqlite_parent_dir(url: str) -> None:
    """Create the parent dir for a file-backed SQLite DB if missing.

    Alembic's `engine_from_config()` (used inside `alembic/env.py`)
    doesn't auto-create directories; on a fresh install where
    `~/.llm-usage/` doesn't yet exist, SQLite raises
    `OperationalError: unable to open database file` before any
    migration runs. The runtime engine factory in
    `core/db/session.py:create_engine()` already does this same step
    for non-Alembic paths; we replicate it here so the two paths agree.
    """
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        return
    db_path = parsed.database
    if not db_path or db_path == ":memory:":
        return
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)


def materialize_pricing_if_empty() -> int:
    """Populate `pricing_snapshot` from the vendored JSON on a fresh DB.

    Returns the number of rows written. A return value of `0` means the
    table was already populated (the table is the source of truth once
    seeded; weekly refresh is a separate, post-v1 concern).
    """
    with get_session() as session:
        existing = session.scalar(select(func.count()).select_from(PricingSnapshot)) or 0
        if existing > 0:
            return 0
        pricings = load_vendored_pricing()
        written = upsert_pricing(session, pricings)
        session.commit()
        return written


def bootstrap() -> None:
    """Bring the DB up to head schema and seed pricing if empty."""
    migrate_to_head()
    materialized = materialize_pricing_if_empty()
    if materialized:
        logger.info("materialized %d pricing rows from vendored JSON", materialized)
