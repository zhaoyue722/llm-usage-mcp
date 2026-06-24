"""Schema migration and pricing materialization on every server boot.

`main()` invokes `bootstrap()` before starting the MCP server. The two
steps are exposed separately so tests (and future tooling like an
`llm-usage init` CLI command) can run them in isolation.

- `migrate_to_head()` runs `alembic upgrade head` programmatically. The
  command is idempotent: at-head is a no-op, missing revisions get
  applied. The migrations ship *inside* the package
  (`llm_usage/migrations/`), so we point Alembic's `script_location`
  at that bundled directory via `__file__` — this works identically
  from a development checkout and from a `pip install`ed wheel (the
  root `alembic.ini` is only for the dev `alembic` autogenerate CLI).
- `materialize_pricing()` loads the vendored LiteLLM JSON (merged with
  `pricing_overrides.json`) via `load_vendored_pricing()` and upserts
  every entry on every boot. The upsert is idempotent by
  `(provider, model)`, so re-running over an already-populated table
  is safe and fast (~200 rows). Running unconditionally is what makes
  edits to `pricing_overrides.json` actually reach `pricing_snapshot`
  on the next restart — an earlier "first-run only" guard silently
  ignored override edits once the table had any rows. Returns the
  number of rows written so the caller can log it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine.url import make_url

from llm_usage.config import get_settings
from llm_usage.core.db.session import get_session
from llm_usage.core.pricing import upsert_pricing
from llm_usage.core.pricing_loader import load_vendored_pricing

logger = logging.getLogger(__name__)

# The migrations ship inside the package (`llm_usage/migrations/`), so this
# resolves correctly whether running from a source checkout or an installed
# wheel — the fix for v0.1.0's "alembic.ini not found" boot failure on
# `pip install`. The root `alembic.ini` is only for the dev autogenerate CLI.
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _alembic_config() -> Config:
    """Alembic `Config` pointed at the bundled migrations (no ini file).

    Shared by `migrate_to_head()` (which then adds the DB URL) and the
    `status` diagnostics (which only needs the script directory to read
    the head revision). Building it programmatically is what lets the
    installed wheel migrate without an `alembic.ini` on disk.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    return cfg


def migrate_to_head() -> None:
    """Run `alembic upgrade head` against the configured DB. Idempotent.

    `migrations/env.py` resolves the database URL itself via
    `resolve_db_url()`, so the `sqlalchemy.url` set here is a
    belt-and-braces default.
    """
    cfg = _alembic_config()
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


def materialize_pricing() -> int:
    """Upsert the vendored pricing JSON into `pricing_snapshot` unconditionally.

    Runs on every server boot. The upsert is idempotent by
    `(provider, model)`, so on a stable rate card subsequent boots
    write the same rows back; on a *changed* rate card (LiteLLM
    refresh or a `pricing_overrides.json` edit), the diff propagates
    on the next restart.

    Returns the number of rows written.
    """
    pricings = load_vendored_pricing()
    with get_session() as session:
        written = upsert_pricing(session, pricings)
        session.commit()
        return written


def bootstrap() -> None:
    """Bring the DB up to head schema and refresh pricing.

    The `quality_snapshot` table is created by the migration but left
    empty in v1 — `recommend_provider` ranks by cost only. A future
    release adds a quality importer and a `materialize_quality_*` step
    here; the table is already in place to receive it.
    """
    migrate_to_head()
    pricing_rows = materialize_pricing()
    logger.info("materialized %d pricing rows from vendored JSON", pricing_rows)
