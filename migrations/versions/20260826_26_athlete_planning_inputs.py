"""add athlete planning inputs

Revision ID: 20260826_26
Revises: 20260824_25
Create Date: 2026-08-26 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_26"
down_revision: str | None = "20260824_25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "athlete_planning_profiles",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("experience_level", sa.String(length=20), nullable=True),
        sa.Column("preferred_long_run_weekday", sa.Integer(), nullable=True),
        sa.Column("self_declared_reentry", sa.Boolean(), nullable=False),
        sa.Column("constraint_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
        sa.CheckConstraint(
            "experience_level IS NULL OR experience_level IN "
            "('novice', 'intermediate', 'advanced')",
            name="ck_athlete_planning_profiles_experience_level",
        ),
        sa.CheckConstraint(
            "preferred_long_run_weekday IS NULL OR preferred_long_run_weekday BETWEEN 0 AND 6",
            name="ck_athlete_planning_profiles_long_run_weekday",
        ),
    )
    op.create_table(
        "athlete_goals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("sport", sa.String(length=30), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("event_name", sa.String(length=200), nullable=True),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "event_type IN ('general_fitness', '5k', '10k', 'half_marathon', 'marathon')",
            name="ck_athlete_goals_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'achieved', 'archived')",
            name="ck_athlete_goals_status",
        ),
    )
    op.create_index(op.f("ix_athlete_goals_user_id"), "athlete_goals", ["user_id"])
    op.create_index(
        "ix_athlete_goals_user_status",
        "athlete_goals",
        ["user_id", "status"],
    )
    op.create_table(
        "athlete_availability",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("available_minutes", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "weekday", name="uq_athlete_availability_user_weekday"),
        sa.CheckConstraint(
            "weekday BETWEEN 0 AND 6",
            name="ck_athlete_availability_weekday",
        ),
        sa.CheckConstraint(
            "(available = 0) OR (available_minutes IS NOT NULL AND available_minutes > 0)",
            name="ck_athlete_availability_minutes",
        ),
        sa.CheckConstraint(
            "available_minutes IS NULL OR available_minutes BETWEEN 1 AND 1440",
            name="ck_athlete_availability_minutes_range",
        ),
    )
    op.create_index(op.f("ix_athlete_availability_user_id"), "athlete_availability", ["user_id"])
    op.create_table(
        "performance_anchors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("distance_m", sa.Float(), nullable=False),
        sa.Column("duration_s", sa.Float(), nullable=False),
        sa.Column("achieved_on", sa.Date(), nullable=False),
        sa.Column("reliable", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "kind IN ('race', 'time_trial', 'manual')",
            name="ck_performance_anchors_kind",
        ),
        sa.CheckConstraint(
            "distance_m > 0",
            name="ck_performance_anchors_distance_positive",
        ),
        sa.CheckConstraint(
            "duration_s > 0",
            name="ck_performance_anchors_duration_positive",
        ),
    )
    op.create_index(op.f("ix_performance_anchors_user_id"), "performance_anchors", ["user_id"])
    op.create_index(
        "ix_performance_anchors_user_achieved",
        "performance_anchors",
        ["user_id", "achieved_on"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_performance_anchors_user_achieved"),
        table_name="performance_anchors",
    )
    op.drop_index(op.f("ix_performance_anchors_user_id"), table_name="performance_anchors")
    op.drop_table("performance_anchors")
    op.drop_index(op.f("ix_athlete_availability_user_id"), table_name="athlete_availability")
    op.drop_table("athlete_availability")
    op.drop_index("ix_athlete_goals_user_status", table_name="athlete_goals")
    op.drop_index(op.f("ix_athlete_goals_user_id"), table_name="athlete_goals")
    op.drop_table("athlete_goals")
    op.drop_table("athlete_planning_profiles")
