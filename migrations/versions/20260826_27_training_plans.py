"""add persisted weekly training plans

Revision ID: 20260826_27
Revises: 20260826_26
Create Date: 2026-08-26 12:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_27"
down_revision: str | None = "20260826_26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "training_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("current_revision_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "user_id", name="uq_training_plans_id_user_id"),
        sa.UniqueConstraint("user_id", "week_start", name="uq_training_plans_user_week"),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_training_plans_status"),
    )
    op.create_index(op.f("ix_training_plans_user_id"), "training_plans", ["user_id"])
    op.create_index(
        op.f("ix_training_plans_current_revision_id"),
        "training_plans",
        ["current_revision_id"],
    )
    op.create_table(
        "training_plan_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("week_end", sa.Date(), nullable=False),
        sa.Column("planner_version", sa.String(length=100), nullable=False),
        sa.Column("knowledge_base_version", sa.String(length=200), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("generation_context_json", sa.JSON(), nullable=False),
        sa.Column("validation_report_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id", "owner_user_id"],
            ["training_plans.id", "training_plans.user_id"],
            name="fk_training_plan_revisions_plan_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_training_plan_revisions_id_owner"),
        sa.UniqueConstraint("plan_id", "revision_number", name="uq_training_plan_revisions_number"),
        sa.UniqueConstraint(
            "plan_id", "input_fingerprint", name="uq_training_plan_revisions_fingerprint"
        ),
        sa.CheckConstraint(
            "revision_number >= 1", name="ck_training_plan_revisions_number_positive"
        ),
    )
    op.create_index(
        op.f("ix_training_plan_revisions_plan_id"), "training_plan_revisions", ["plan_id"]
    )
    op.create_table(
        "training_plan_workouts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_revision_id", sa.Integer(), nullable=False),
        sa.Column("workout_id", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("scheduled_for", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_revision_id", "owner_user_id"],
            ["training_plan_revisions.id", "training_plan_revisions.owner_user_id"],
            name="fk_training_plan_workouts_revision_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workout_id", "owner_user_id"],
            ["workouts.id", "workouts.user_id"],
            name="fk_training_plan_workouts_workout_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_revision_id", "position", name="uq_training_plan_workouts_position"
        ),
        sa.UniqueConstraint(
            "plan_revision_id", "workout_id", name="uq_training_plan_workouts_workout"
        ),
    )
    op.create_index(
        op.f("ix_training_plan_workouts_plan_revision_id"),
        "training_plan_workouts",
        ["plan_revision_id"],
    )
    op.create_index(
        op.f("ix_training_plan_workouts_workout_id"), "training_plan_workouts", ["workout_id"]
    )
    op.create_index(
        op.f("ix_training_plan_workouts_scheduled_for"),
        "training_plan_workouts",
        ["scheduled_for"],
    )
    op.execute(
        "CREATE TRIGGER prevent_training_plan_revisions_update "
        "BEFORE UPDATE ON training_plan_revisions "
        "BEGIN SELECT RAISE(ABORT, "
        "'Training plan revisions and memberships are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER prevent_training_plan_workouts_update "
        "BEFORE UPDATE ON training_plan_workouts "
        "BEGIN SELECT RAISE(ABORT, "
        "'Training plan revisions and memberships are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER validate_training_plan_current_revision "
        "BEFORE UPDATE OF current_revision_id ON training_plans "
        "WHEN NEW.current_revision_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM training_plan_revisions "
        "WHERE id = NEW.current_revision_id AND plan_id = NEW.id) "
        "BEGIN SELECT RAISE(ABORT, 'Current revision must belong to its plan'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS validate_training_plan_current_revision")
    op.execute("DROP TRIGGER IF EXISTS prevent_training_plan_workouts_update")
    op.execute("DROP TRIGGER IF EXISTS prevent_training_plan_revisions_update")
    op.drop_index(
        op.f("ix_training_plan_workouts_scheduled_for"), table_name="training_plan_workouts"
    )
    op.drop_index(op.f("ix_training_plan_workouts_workout_id"), table_name="training_plan_workouts")
    op.drop_index(
        op.f("ix_training_plan_workouts_plan_revision_id"),
        table_name="training_plan_workouts",
    )
    op.drop_table("training_plan_workouts")
    op.drop_index(op.f("ix_training_plan_revisions_plan_id"), table_name="training_plan_revisions")
    op.drop_table("training_plan_revisions")
    op.drop_index(op.f("ix_training_plans_current_revision_id"), table_name="training_plans")
    op.drop_index(op.f("ix_training_plans_user_id"), table_name="training_plans")
    op.drop_table("training_plans")
