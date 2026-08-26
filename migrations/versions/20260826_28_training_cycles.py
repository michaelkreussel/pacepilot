"""add versioned multi-week training cycles

Revision ID: 20260826_28
Revises: 20260826_27
Create Date: 2026-08-26 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_28"
down_revision: str | None = "20260826_27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("athlete_goals") as batch:
        batch.create_unique_constraint("uq_athlete_goals_id_user_id", ["id", "user_id"])

    op.create_table(
        "training_cycles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("goal_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("current_revision_id", sa.Integer(), nullable=True),
        sa.Column("accepted_revision_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["goal_id", "user_id"],
            ["athlete_goals.id", "athlete_goals.user_id"],
            name="fk_training_cycles_goal_owner",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "user_id", name="uq_training_cycles_id_user_id"),
        sa.UniqueConstraint(
            "user_id", "goal_id", "start_date", name="uq_training_cycles_user_goal_start"
        ),
        sa.CheckConstraint(
            "event_type IN ('general_fitness', '5k', '10k', 'half_marathon', 'marathon')",
            name="ck_training_cycles_event_type",
        ),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_training_cycles_status"),
        sa.CheckConstraint("target_date > start_date", name="ck_training_cycles_dates"),
    )
    op.create_index(op.f("ix_training_cycles_user_id"), "training_cycles", ["user_id"])
    op.create_index(op.f("ix_training_cycles_goal_id"), "training_cycles", ["goal_id"])
    op.create_index(
        op.f("ix_training_cycles_current_revision_id"), "training_cycles", ["current_revision_id"]
    )
    op.create_index(
        op.f("ix_training_cycles_accepted_revision_id"), "training_cycles", ["accepted_revision_id"]
    )

    op.create_table(
        "training_cycle_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cycle_id", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("parent_revision_id", sa.Integer(), nullable=True),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("planner_version", sa.String(length=100), nullable=False),
        sa.Column("knowledge_base_version", sa.String(length=200), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.String(length=20), nullable=False),
        sa.Column("phase_plan_json", sa.JSON(), nullable=False),
        sa.Column("assumptions_json", sa.JSON(), nullable=False),
        sa.Column("impact_json", sa.JSON(), nullable=False),
        sa.Column("validation_report_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["cycle_id", "owner_user_id"],
            ["training_cycles.id", "training_cycles.user_id"],
            name="fk_training_cycle_revisions_cycle_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_revision_id", "cycle_id", "owner_user_id"],
            [
                "training_cycle_revisions.id",
                "training_cycle_revisions.cycle_id",
                "training_cycle_revisions.owner_user_id",
            ],
            name="fk_training_cycle_revisions_parent_same_cycle",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_training_cycle_revisions_id_owner"),
        sa.UniqueConstraint(
            "id",
            "cycle_id",
            "owner_user_id",
            name="uq_training_cycle_revisions_id_cycle_owner",
        ),
        sa.UniqueConstraint(
            "cycle_id", "revision_number", name="uq_training_cycle_revisions_number"
        ),
        sa.UniqueConstraint(
            "cycle_id", "input_fingerprint", name="uq_training_cycle_revisions_fingerprint"
        ),
        sa.CheckConstraint(
            "revision_number >= 1", name="ck_training_cycle_revisions_number_positive"
        ),
    )
    op.create_index(
        op.f("ix_training_cycle_revisions_cycle_id"),
        "training_cycle_revisions",
        ["cycle_id"],
    )
    op.create_index(
        op.f("ix_training_cycle_revisions_parent_revision_id"),
        "training_cycle_revisions",
        ["parent_revision_id"],
    )

    op.create_table(
        "training_cycle_weeks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cycle_revision_id", sa.Integer(), nullable=False),
        sa.Column("training_plan_revision_id", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("phase", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["cycle_revision_id", "owner_user_id"],
            ["training_cycle_revisions.id", "training_cycle_revisions.owner_user_id"],
            name="fk_training_cycle_weeks_revision_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["training_plan_revision_id", "owner_user_id"],
            ["training_plan_revisions.id", "training_plan_revisions.owner_user_id"],
            name="fk_training_cycle_weeks_plan_revision_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cycle_revision_id", "position", name="uq_training_cycle_weeks_position"
        ),
        sa.UniqueConstraint(
            "cycle_revision_id",
            "training_plan_revision_id",
            name="uq_training_cycle_weeks_plan_revision",
        ),
    )
    op.create_index(
        op.f("ix_training_cycle_weeks_cycle_revision_id"),
        "training_cycle_weeks",
        ["cycle_revision_id"],
    )
    op.create_index(
        op.f("ix_training_cycle_weeks_training_plan_revision_id"),
        "training_cycle_weeks",
        ["training_plan_revision_id"],
    )
    op.create_index(
        op.f("ix_training_cycle_weeks_owner_user_id"), "training_cycle_weeks", ["owner_user_id"]
    )
    op.create_index(
        op.f("ix_training_cycle_weeks_week_start"), "training_cycle_weeks", ["week_start"]
    )
    op.execute(
        "CREATE TRIGGER prevent_training_cycle_revisions_update "
        "BEFORE UPDATE ON training_cycle_revisions "
        "BEGIN SELECT RAISE(ABORT, "
        "'Training cycle revisions and memberships are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER prevent_training_cycle_weeks_update "
        "BEFORE UPDATE ON training_cycle_weeks "
        "BEGIN SELECT RAISE(ABORT, "
        "'Training cycle revisions and memberships are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER validate_training_cycle_revision_pointers "
        "BEFORE UPDATE OF current_revision_id, accepted_revision_id ON training_cycles "
        "WHEN (NEW.current_revision_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM training_cycle_revisions WHERE id = NEW.current_revision_id "
        "AND cycle_id = NEW.id AND owner_user_id = NEW.user_id)) OR "
        "(NEW.accepted_revision_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM training_cycle_revisions WHERE id = NEW.accepted_revision_id "
        "AND cycle_id = NEW.id AND owner_user_id = NEW.user_id)) "
        "BEGIN SELECT RAISE(ABORT, 'Cycle revision must belong to its cycle'); END"
    )
    op.execute(
        "CREATE TRIGGER validate_training_cycle_revision_pointers_insert "
        "BEFORE INSERT ON training_cycles "
        "WHEN (NEW.current_revision_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM training_cycle_revisions WHERE id = NEW.current_revision_id "
        "AND cycle_id = NEW.id AND owner_user_id = NEW.user_id)) OR "
        "(NEW.accepted_revision_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM training_cycle_revisions WHERE id = NEW.accepted_revision_id "
        "AND cycle_id = NEW.id AND owner_user_id = NEW.user_id)) "
        "BEGIN SELECT RAISE(ABORT, 'Cycle revision must belong to its cycle'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS validate_training_cycle_revision_pointers_insert")
    op.execute("DROP TRIGGER IF EXISTS validate_training_cycle_revision_pointers")
    op.execute("DROP TRIGGER IF EXISTS prevent_training_cycle_weeks_update")
    op.execute("DROP TRIGGER IF EXISTS prevent_training_cycle_revisions_update")
    op.drop_index(op.f("ix_training_cycle_weeks_week_start"), table_name="training_cycle_weeks")
    op.drop_index(op.f("ix_training_cycle_weeks_owner_user_id"), table_name="training_cycle_weeks")
    op.drop_index(
        op.f("ix_training_cycle_weeks_training_plan_revision_id"), table_name="training_cycle_weeks"
    )
    op.drop_index(
        op.f("ix_training_cycle_weeks_cycle_revision_id"), table_name="training_cycle_weeks"
    )
    op.drop_table("training_cycle_weeks")
    op.drop_index(
        op.f("ix_training_cycle_revisions_parent_revision_id"),
        table_name="training_cycle_revisions",
    )
    op.drop_index(
        op.f("ix_training_cycle_revisions_cycle_id"), table_name="training_cycle_revisions"
    )
    op.drop_table("training_cycle_revisions")
    op.drop_index(op.f("ix_training_cycles_accepted_revision_id"), table_name="training_cycles")
    op.drop_index(op.f("ix_training_cycles_current_revision_id"), table_name="training_cycles")
    op.drop_index(op.f("ix_training_cycles_goal_id"), table_name="training_cycles")
    op.drop_index(op.f("ix_training_cycles_user_id"), table_name="training_cycles")
    op.drop_table("training_cycles")
    with op.batch_alter_table("athlete_goals") as batch:
        batch.drop_constraint("uq_athlete_goals_id_user_id", type_="unique")
