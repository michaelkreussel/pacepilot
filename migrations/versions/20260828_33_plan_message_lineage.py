"""add source assistant messages to plan and cycle revisions

Revision ID: 20260828_33
Revises: 20260828_32
Create Date: 2026-08-28 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_33"
down_revision: str | None = "20260828_32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_plan_revision_triggers() -> None:
    op.execute(
        "CREATE TRIGGER prevent_training_plan_revisions_update "
        "BEFORE UPDATE OF id, plan_id, owner_user_id, revision_number, week_start, week_end, "
        "planner_version, knowledge_base_version, input_fingerprint, generation_context_json, "
        "validation_report_json, created_at ON training_plan_revisions "
        "BEGIN SELECT RAISE(ABORT, "
        "'Training plan revisions and memberships are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER prevent_training_plan_revision_source_update "
        "BEFORE UPDATE OF source_assistant_message_id ON training_plan_revisions "
        "WHEN NOT (OLD.source_assistant_message_id IS NOT NULL "
        "AND NEW.source_assistant_message_id IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM coach_messages WHERE id = OLD.source_assistant_message_id)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'Training plan revisions and memberships are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER validate_training_plan_revision_source "
        "BEFORE INSERT ON training_plan_revisions "
        "WHEN NEW.source_assistant_message_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM coach_messages m JOIN coach_conversations c "
        "ON c.id = m.conversation_id WHERE m.id = NEW.source_assistant_message_id "
        "AND m.role = 'assistant' AND c.user_id = NEW.owner_user_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'Plan source must be an assistant message owned by the artifact user'); END"
    )


def _create_cycle_revision_triggers() -> None:
    op.execute(
        "CREATE TRIGGER prevent_training_cycle_revisions_update "
        "BEFORE UPDATE OF id, cycle_id, owner_user_id, parent_revision_id, revision_number, "
        "event_type, start_date, target_date, planner_version, knowledge_base_version, "
        "input_fingerprint, confidence, phase_plan_json, assumptions_json, impact_json, "
        "validation_report_json, created_at ON training_cycle_revisions "
        "BEGIN SELECT RAISE(ABORT, "
        "'Training cycle revisions and memberships are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER prevent_training_cycle_revision_source_update "
        "BEFORE UPDATE OF source_assistant_message_id ON training_cycle_revisions "
        "WHEN NOT (OLD.source_assistant_message_id IS NOT NULL "
        "AND NEW.source_assistant_message_id IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM coach_messages WHERE id = OLD.source_assistant_message_id)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'Training cycle revisions and memberships are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER validate_training_cycle_revision_source "
        "BEFORE INSERT ON training_cycle_revisions "
        "WHEN NEW.source_assistant_message_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM coach_messages m JOIN coach_conversations c "
        "ON c.id = m.conversation_id WHERE m.id = NEW.source_assistant_message_id "
        "AND m.role = 'assistant' AND c.user_id = NEW.owner_user_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'Cycle source must be an assistant message owned by the artifact user'); END"
    )


def _create_plan_pointer_trigger() -> None:
    op.execute(
        "CREATE TRIGGER validate_training_plan_current_revision "
        "BEFORE UPDATE OF current_revision_id ON training_plans "
        "WHEN NEW.current_revision_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM training_plan_revisions "
        "WHERE id = NEW.current_revision_id AND plan_id = NEW.id) "
        "BEGIN SELECT RAISE(ABORT, 'Current revision must belong to its plan'); END"
    )


def _create_cycle_pointer_triggers() -> None:
    for name, action in (
        ("validate_training_cycle_revision_pointers", "UPDATE"),
        ("validate_training_cycle_revision_pointers_insert", "INSERT"),
    ):
        event = (
            "BEFORE UPDATE OF current_revision_id, accepted_revision_id ON training_cycles"
            if action == "UPDATE"
            else "BEFORE INSERT ON training_cycles"
        )
        op.execute(
            f"CREATE TRIGGER {name} {event} "
            "WHEN (NEW.current_revision_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM training_cycle_revisions WHERE id = NEW.current_revision_id "
            "AND cycle_id = NEW.id AND owner_user_id = NEW.user_id)) OR "
            "(NEW.accepted_revision_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM training_cycle_revisions WHERE id = NEW.accepted_revision_id "
            "AND cycle_id = NEW.id AND owner_user_id = NEW.user_id)) "
            "BEGIN SELECT RAISE(ABORT, 'Cycle revision must belong to its cycle'); END"
        )


def upgrade() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")

    op.execute("DROP TRIGGER IF EXISTS validate_training_plan_current_revision")
    op.execute("DROP TRIGGER IF EXISTS prevent_training_plan_revisions_update")
    with op.batch_alter_table("training_plan_revisions") as batch_op:
        batch_op.add_column(sa.Column("source_assistant_message_id", sa.Integer()))
        batch_op.create_foreign_key(
            "fk_training_plan_revisions_source_assistant_message",
            "coach_messages",
            ["source_assistant_message_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_training_plan_revisions_source_assistant_message_id",
        "training_plan_revisions",
        ["source_assistant_message_id"],
    )
    _create_plan_revision_triggers()
    _create_plan_pointer_trigger()

    op.execute("DROP TRIGGER IF EXISTS validate_training_cycle_revision_pointers")
    op.execute("DROP TRIGGER IF EXISTS validate_training_cycle_revision_pointers_insert")
    op.execute("DROP TRIGGER IF EXISTS prevent_training_cycle_revisions_update")
    with op.batch_alter_table("training_cycle_revisions") as batch_op:
        batch_op.add_column(sa.Column("source_assistant_message_id", sa.Integer()))
        batch_op.create_foreign_key(
            "fk_training_cycle_revisions_source_assistant_message",
            "coach_messages",
            ["source_assistant_message_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_training_cycle_revisions_source_assistant_message_id",
        "training_cycle_revisions",
        ["source_assistant_message_id"],
    )
    _create_cycle_revision_triggers()
    _create_cycle_pointer_triggers()

    connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")

    op.execute("DROP TRIGGER IF EXISTS validate_training_cycle_revision_pointers")
    op.execute("DROP TRIGGER IF EXISTS validate_training_cycle_revision_pointers_insert")
    op.execute("DROP TRIGGER IF EXISTS validate_training_cycle_revision_source")
    op.execute("DROP TRIGGER IF EXISTS prevent_training_cycle_revision_source_update")
    op.execute("DROP TRIGGER IF EXISTS prevent_training_cycle_revisions_update")
    op.drop_index(
        "ix_training_cycle_revisions_source_assistant_message_id",
        table_name="training_cycle_revisions",
    )
    with op.batch_alter_table("training_cycle_revisions") as batch_op:
        batch_op.drop_constraint(
            "fk_training_cycle_revisions_source_assistant_message", type_="foreignkey"
        )
        batch_op.drop_column("source_assistant_message_id")
    op.execute(
        "CREATE TRIGGER prevent_training_cycle_revisions_update BEFORE UPDATE ON "
        "training_cycle_revisions BEGIN SELECT RAISE(ABORT, "
        "'Training cycle revisions and memberships are immutable'); END"
    )
    _create_cycle_pointer_triggers()

    op.execute("DROP TRIGGER IF EXISTS validate_training_plan_current_revision")
    op.execute("DROP TRIGGER IF EXISTS validate_training_plan_revision_source")
    op.execute("DROP TRIGGER IF EXISTS prevent_training_plan_revision_source_update")
    op.execute("DROP TRIGGER IF EXISTS prevent_training_plan_revisions_update")
    op.drop_index(
        "ix_training_plan_revisions_source_assistant_message_id",
        table_name="training_plan_revisions",
    )
    with op.batch_alter_table("training_plan_revisions") as batch_op:
        batch_op.drop_constraint(
            "fk_training_plan_revisions_source_assistant_message", type_="foreignkey"
        )
        batch_op.drop_column("source_assistant_message_id")
    op.execute(
        "CREATE TRIGGER prevent_training_plan_revisions_update BEFORE UPDATE ON "
        "training_plan_revisions BEGIN SELECT RAISE(ABORT, "
        "'Training plan revisions and memberships are immutable'); END"
    )
    _create_plan_pointer_trigger()

    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
