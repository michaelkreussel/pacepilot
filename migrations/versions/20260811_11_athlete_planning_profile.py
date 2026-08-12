"""add athlete planning profile

Revision ID: 20260811_11
Revises: 20260811_10
Create Date: 2026-08-11 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_11"
down_revision: str | None = "20260811_10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "athlete_profiles",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("primary_sport", sa.String(length=30), nullable=True),
        sa.Column("experience_level", sa.String(length=20), nullable=True),
        sa.Column("experience_years", sa.Integer(), nullable=True),
        sa.Column("constraint_note", sa.Text(), nullable=True),
        sa.Column("constraint_until", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "athlete_goals",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("sport", sa.String(length=30), nullable=False),
        sa.Column("event_name", sa.String(length=200), nullable=True),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("target_duration_s", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "athlete_availability",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("max_duration_minutes", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "max_duration_minutes >= 15 AND max_duration_minutes <= 1440",
            name="ck_athlete_availability_duration",
        ),
        sa.CheckConstraint("weekday >= 0 AND weekday <= 6", name="ck_athlete_availability_weekday"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "weekday"),
    )
    op.create_table(
        "athlete_manual_anchors",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("sport", sa.String(length=30), nullable=False),
        sa.Column("metric", sa.String(length=40), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("observed_on", sa.Date(), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("value > 0", name="ck_athlete_manual_anchor_value"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "sport", "metric"),
    )


def downgrade() -> None:
    op.drop_table("athlete_manual_anchors")
    op.drop_table("athlete_availability")
    op.drop_table("athlete_goals")
    op.drop_table("athlete_profiles")
