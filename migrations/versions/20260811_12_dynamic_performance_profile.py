"""add dynamic performance profile data

Revision ID: 20260811_12
Revises: 20260811_11
Create Date: 2026-08-11 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_12"
down_revision: str | None = "20260811_11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "athlete_imported_metrics",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("sport", sa.String(length=30), nullable=False),
        sa.Column("metric", sa.String(length=40), nullable=False),
        sa.Column("resource", sa.String(length=40), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("source_day", sa.Date(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("value > 0", name="ck_athlete_imported_metric_value"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "sport", "metric", "resource"),
    )
    op.create_table(
        "athlete_zone_settings",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("sport", sa.String(length=30), nullable=False),
        sa.Column("zone_type", sa.String(length=20), nullable=False),
        sa.Column("zone_number", sa.Integer(), nullable=False),
        sa.Column("lower_boundary", sa.Float(), nullable=False),
        sa.Column("upper_boundary", sa.Float(), nullable=True),
        sa.Column("resource", sa.String(length=40), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("lower_boundary >= 0", name="ck_athlete_zone_lower_boundary"),
        sa.CheckConstraint("zone_number >= 1 AND zone_number <= 10", name="ck_athlete_zone_number"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "sport", "zone_type", "zone_number"),
    )


def downgrade() -> None:
    op.drop_table("athlete_zone_settings")
    op.drop_table("athlete_imported_metrics")
