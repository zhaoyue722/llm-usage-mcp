"""add pricing_tier

Revision ID: a7c2e1b9d4f8
Revises: e4958a730a6c
Create Date: 2026-05-23 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c2e1b9d4f8"
down_revision: str | Sequence[str] | None = "e4958a730a6c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add `pricing_tier` — per-model prompt-size pricing brackets.

    A model with `tiered_pricing` in LiteLLM's JSON (today: a handful
    of Qwen / DashScope models; in principle any model with size-
    bracketed rates) gets one row here per tier. `pricing_snapshot`
    keeps the tier-0 rate as its flat fallback so existing cost code
    that reads only the snapshot continues to work; tier-aware cost
    code (next slice) joins on `(provider, model)` and picks the row
    where `prompt_tokens` falls in `[range_start, range_end)`.

    Composite primary key (provider, model, tier_index) mirrors the
    pricing_snapshot composite PK and preserves the order LiteLLM
    emits tiers in. No FK to pricing_snapshot — same posture as
    quality_snapshot, which is a sibling table without FK.
    """
    op.create_table(
        "pricing_tier",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("tier_index", sa.Integer(), nullable=False),
        sa.Column("range_start", sa.Integer(), nullable=False),
        sa.Column("range_end", sa.Integer(), nullable=False),
        sa.Column("input_per_million_usd", sa.Float(), nullable=False),
        sa.Column("output_per_million_usd", sa.Float(), nullable=False),
        sa.Column("fetched_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("provider", "model", "tier_index"),
    )


def downgrade() -> None:
    """Drop `pricing_tier`."""
    op.drop_table("pricing_tier")
