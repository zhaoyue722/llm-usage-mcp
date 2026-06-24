"""cost_usd to cost_nano_usd

Revision ID: 9e3b7a72bd41
Revises: 278ba38a2efd
Create Date: 2026-05-06 19:47:15.807662

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9e3b7a72bd41"
down_revision: str | Sequence[str] | None = "278ba38a2efd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Convert cost storage from REAL USD to INTEGER nano-USD.

    Done in three phases so existing rows survive: add the new column
    nullable, backfill via `cost_usd * 1e9`, then tighten to NOT NULL and
    drop the old column.
    """
    with op.batch_alter_table("usage_events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cost_nano_usd", sa.Integer(), nullable=True))

    op.execute(
        sa.text("UPDATE usage_events SET cost_nano_usd = CAST(cost_usd * 1000000000 AS INTEGER)")
    )

    with op.batch_alter_table("usage_events", schema=None) as batch_op:
        batch_op.alter_column("cost_nano_usd", existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column("cost_usd")


def downgrade() -> None:
    """Reverse the conversion: nano-USD INTEGER back to USD REAL."""
    with op.batch_alter_table("usage_events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cost_usd", sa.Float(), nullable=True))

    op.execute(sa.text("UPDATE usage_events SET cost_usd = cost_nano_usd / 1000000000.0"))

    with op.batch_alter_table("usage_events", schema=None) as batch_op:
        batch_op.alter_column("cost_usd", existing_type=sa.Float(), nullable=False)
        batch_op.drop_column("cost_nano_usd")
