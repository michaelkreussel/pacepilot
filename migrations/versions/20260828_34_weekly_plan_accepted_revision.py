"""add accepted revision identity to weekly plans

Revision ID: 20260828_34
Revises: 20260828_33
Create Date: 2026-08-28 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_34"
down_revision: str | None = "20260828_33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_pointer_triggers() -> None:
    for name, event in (
        (
            "validate_training_plan_revision_pointers",
            "BEFORE UPDATE OF current_revision_id, accepted_revision_id ON training_plans",
        ),
        ("validate_training_plan_revision_pointers_insert", "BEFORE INSERT ON training_plans"),
    ):
        op.execute(
            f"CREATE TRIGGER {name} {event} "
            "WHEN (NEW.current_revision_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM training_plan_revisions WHERE id = NEW.current_revision_id "
            "AND plan_id = NEW.id AND owner_user_id = NEW.user_id)) OR "
            "(NEW.accepted_revision_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM training_plan_revisions WHERE id = NEW.accepted_revision_id "
            "AND plan_id = NEW.id AND owner_user_id = NEW.user_id)) "
            "BEGIN SELECT RAISE(ABORT, "
            "'Plan revision must belong to its plan and user'); END"
        )


def _create_legacy_current_pointer_trigger() -> None:
    op.execute(
        "CREATE TRIGGER validate_training_plan_current_revision "
        "BEFORE UPDATE OF current_revision_id ON training_plans "
        "WHEN NEW.current_revision_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM training_plan_revisions "
        "WHERE id = NEW.current_revision_id AND plan_id = NEW.id) "
        "BEGIN SELECT RAISE(ABORT, 'Current revision must belong to its plan'); END"
    )


def upgrade() -> None:
    connection = op.get_bind()
    conflict = connection.exec_driver_sql(
        "SELECT p.id FROM training_plans p "
        "WHERE p.current_revision_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM training_plan_revisions r WHERE r.id = p.current_revision_id "
        "AND r.plan_id = p.id AND r.owner_user_id = p.user_id) LIMIT 1"
    ).first()
    if conflict is not None:
        raise RuntimeError(f"weekly plan revision pointer conflict for training plan {conflict.id}")

    op.execute("DROP TRIGGER IF EXISTS validate_training_plan_current_revision")
    op.add_column("training_plans", sa.Column("accepted_revision_id", sa.Integer()))
    op.create_index(
        "ix_training_plans_accepted_revision_id",
        "training_plans",
        ["accepted_revision_id"],
    )
    _create_pointer_triggers()


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS validate_training_plan_revision_pointers")
    op.execute("DROP TRIGGER IF EXISTS validate_training_plan_revision_pointers_insert")
    op.drop_index("ix_training_plans_accepted_revision_id", table_name="training_plans")
    op.drop_column("training_plans", "accepted_revision_id")
    _create_legacy_current_pointer_trigger()
