"""Initial PacePilot schema.

Revision ID: 20260805_01
Revises:
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260805_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_TABLES = {
    "activities",
    "daily_health",
    "garmin_accounts",
    "garmin_devices",
    "sync_runs",
    "users",
    "workout_steps",
    "workouts",
}


def upgrade() -> None:
    existing_tables = set(inspect(op.get_bind()).get_table_names()) - {"alembic_version"}
    if existing_tables:
        if existing_tables == SCHEMA_TABLES:
            # Early MVP builds created this exact schema before Alembic ran.
            # Returning lets Alembic adopt it without touching existing data.
            return
        unexpected = ", ".join(sorted(existing_tables))
        raise RuntimeError(
            "The database contains a partial or unknown schema and cannot be baselined: "
            f"{unexpected}"
        )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "garmin_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("email", sa.String(320)),
        sa.Column("connected_at", sa.DateTime()),
        sa.Column("last_sync_at", sa.DateTime()),
        sa.Column("sync_status", sa.String(30), nullable=False),
        sa.Column("sync_error", sa.String(1000)),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "garmin_devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("garmin_device_id", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("model", sa.String(200)),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["garmin_accounts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("account_id", "garmin_device_id"),
    )
    op.create_table(
        "activities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("garmin_activity_id", sa.String(100), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("activity_type", sa.String(100), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("distance_m", sa.Float()),
        sa.Column("duration_s", sa.Float()),
        sa.Column("average_hr", sa.Integer()),
        sa.Column("max_hr", sa.Integer()),
        sa.Column("calories", sa.Integer()),
        sa.Column("elevation_gain_m", sa.Float()),
        sa.Column("raw_file", sa.String(500)),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "garmin_activity_id"),
    )
    op.create_index("ix_activities_user_id", "activities", ["user_id"])
    op.create_index("ix_activities_activity_type", "activities", ["activity_type"])
    op.create_index("ix_activities_started_at", "activities", ["started_at"])
    op.create_table(
        "daily_health",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("sleep_seconds", sa.Integer()),
        sa.Column("sleep_score", sa.Integer()),
        sa.Column("resting_hr", sa.Integer()),
        sa.Column("hrv_average", sa.Float()),
        sa.Column("steps", sa.Integer()),
        sa.Column("stress_average", sa.Integer()),
        sa.Column("body_battery_high", sa.Integer()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "day"),
    )
    op.create_index("ix_daily_health_user_id", "daily_health", ["user_id"])
    op.create_index("ix_daily_health_day", "daily_health", ["day"])
    op.create_table(
        "workouts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("sport", sa.String(30), nullable=False),
        sa.Column("scheduled_for", sa.Date()),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("garmin_workout_id", sa.String(100)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_workouts_user_id", "workouts", ["user_id"])
    op.create_index("ix_workouts_scheduled_for", "workouts", ["scheduled_for"])
    op.create_index("ix_workouts_status", "workouts", ["status"])
    op.create_table(
        "workout_steps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workout_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("step_type", sa.String(30), nullable=False),
        sa.Column("duration_type", sa.String(30), nullable=False),
        sa.Column("duration_value", sa.Float()),
        sa.Column("target_type", sa.String(30), nullable=False),
        sa.Column("target_min", sa.Float()),
        sa.Column("target_max", sa.Float()),
        sa.Column("repeat_count", sa.Integer()),
        sa.ForeignKeyConstraint(["workout_id"], ["workouts.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_workout_steps_workout_id", "workout_steps", ["workout_id"])
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("activities_synced", sa.Integer(), nullable=False),
        sa.Column("health_days_synced", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(1000)),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_sync_runs_user_id", "sync_runs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_sync_runs_user_id", table_name="sync_runs")
    op.drop_table("sync_runs")
    op.drop_index("ix_workout_steps_workout_id", table_name="workout_steps")
    op.drop_table("workout_steps")
    op.drop_index("ix_workouts_status", table_name="workouts")
    op.drop_index("ix_workouts_scheduled_for", table_name="workouts")
    op.drop_index("ix_workouts_user_id", table_name="workouts")
    op.drop_table("workouts")
    op.drop_index("ix_daily_health_day", table_name="daily_health")
    op.drop_index("ix_daily_health_user_id", table_name="daily_health")
    op.drop_table("daily_health")
    op.drop_index("ix_activities_started_at", table_name="activities")
    op.drop_index("ix_activities_activity_type", table_name="activities")
    op.drop_index("ix_activities_user_id", table_name="activities")
    op.drop_table("activities")
    op.drop_table("garmin_devices")
    op.drop_table("garmin_accounts")
    op.drop_table("users")
