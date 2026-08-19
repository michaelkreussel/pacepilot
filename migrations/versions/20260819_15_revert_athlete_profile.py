"""revert athlete profile schema

Revision ID: 20260819_15
Revises: 20260812_14
Create Date: 2026-08-19 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_15"
down_revision: str | None = "20260812_14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("athlete_manual_anchors")
    op.drop_table("athlete_availability")
    op.drop_table("athlete_goals")
    op.drop_table("athlete_profiles")

    with op.batch_alter_table("daily_fitness") as batch_op:
        batch_op.drop_column("power_zones")
        batch_op.drop_column("heart_rate_zones")
        batch_op.drop_column("configured_max_hr")
        batch_op.drop_column("personal_record_marathon_seconds")
        batch_op.drop_column("personal_record_half_seconds")
        batch_op.drop_column("personal_record_10k_seconds")
        batch_op.drop_column("personal_record_5k_seconds")
        batch_op.drop_column("personal_record_1k_seconds")

    with op.batch_alter_table("activities") as batch_op:
        batch_op.drop_column("fit_synced_at")
        batch_op.drop_column("fit_import_status")
        batch_op.drop_column("fit_file")


def downgrade() -> None:
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

    with op.batch_alter_table("daily_fitness") as batch_op:
        batch_op.add_column(sa.Column("personal_record_1k_seconds", sa.Integer()))
        batch_op.add_column(sa.Column("personal_record_5k_seconds", sa.Integer()))
        batch_op.add_column(sa.Column("personal_record_10k_seconds", sa.Integer()))
        batch_op.add_column(sa.Column("personal_record_half_seconds", sa.Integer()))
        batch_op.add_column(sa.Column("personal_record_marathon_seconds", sa.Integer()))
        batch_op.add_column(sa.Column("configured_max_hr", sa.Integer()))
        batch_op.add_column(sa.Column("heart_rate_zones", sa.JSON()))
        batch_op.add_column(sa.Column("power_zones", sa.JSON()))

    with op.batch_alter_table("activities") as batch_op:
        batch_op.add_column(sa.Column("fit_file", sa.String(length=500)))
        batch_op.add_column(sa.Column("fit_import_status", sa.String(length=20)))
        batch_op.add_column(sa.Column("fit_synced_at", sa.DateTime()))
