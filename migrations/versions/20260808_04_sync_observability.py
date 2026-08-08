"""add sync observability

Revision ID: 20260808_04
Revises: 20260808_03
Create Date: 2026-08-08 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_04"
down_revision: str | None = "20260808_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sync_runs") as batch_op:
        batch_op.add_column(
            sa.Column("activities_processed", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("activities_total", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("days_completed", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("days_total", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("operations_completed", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("operations_total", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("current_day", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("current_operation", sa.String(length=200), nullable=True))

    op.create_table(
        "sync_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sync_run_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("level", sa.String(length=10), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("resource", sa.String(length=50), nullable=True),
        sa.Column("day", sa.Date(), nullable=True),
        sa.Column("operation", sa.String(length=200), nullable=True),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("record_count", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sync_events_created_at"), "sync_events", ["created_at"], unique=False)
    op.create_index(
        op.f("ix_sync_events_sync_run_id"), "sync_events", ["sync_run_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_sync_events_sync_run_id"), table_name="sync_events")
    op.drop_index(op.f("ix_sync_events_created_at"), table_name="sync_events")
    op.drop_table("sync_events")
    with op.batch_alter_table("sync_runs") as batch_op:
        batch_op.drop_column("current_operation")
        batch_op.drop_column("current_day")
        batch_op.drop_column("operations_total")
        batch_op.drop_column("operations_completed")
        batch_op.drop_column("days_total")
        batch_op.drop_column("days_completed")
        batch_op.drop_column("activities_total")
        batch_op.drop_column("activities_processed")
