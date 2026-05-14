"""add quality_snapshot

Revision ID: e4958a730a6c
Revises: 9e3b7a72bd41
Create Date: 2026-05-14 22:16:04.913533

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4958a730a6c"
down_revision: str | Sequence[str] | None = "9e3b7a72bd41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add `quality_snapshot` — model quality scores, sibling of pricing_snapshot.

    Composite (provider, model) PK and `fetched_at` mirror
    `pricing_snapshot`; `quality_score` is a normalized 0-100 float.
    Kept a separate table because quality and pricing have independent
    sources and refresh cadences.
    """
    op.create_table(
        "quality_snapshot",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=False),
        sa.Column("fetched_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("provider", "model"),
    )


def downgrade() -> None:
    """Drop `quality_snapshot`."""
    op.drop_table("quality_snapshot")
