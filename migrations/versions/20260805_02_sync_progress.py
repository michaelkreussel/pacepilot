"""Add observable Garmin sync progress.

Revision ID: 20260805_02
Revises: 20260805_01
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_02"
down_revision: str | None = "20260805_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sync_runs") as batch_op:
        batch_op.add_column(sa.Column("stage", sa.String(50)))
        batch_op.add_column(sa.Column("message", sa.String(500)))
        batch_op.add_column(
            sa.Column("current_item", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("total_items", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("sync_runs") as batch_op:
        batch_op.drop_column("total_items")
        batch_op.drop_column("current_item")
        batch_op.drop_column("message")
        batch_op.drop_column("stage")
