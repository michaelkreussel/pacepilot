"""drop coach assistant runs and redundant workout origins

Revision ID: 20260906_40
Revises: 20260906_39
Create Date: 2026-09-06 22:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_40"
down_revision: str | None = "20260906_39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WORKOUT_REFERENCING_TRIGGERS = (
    "validate_workout_garmin_operation_training_fit_insert",
    "validate_workout_garmin_operation_training_fit_update",
)


def _reject_conflict(kind: str, query: str) -> None:
    conflict_id = op.get_bind().exec_driver_sql(query).scalar()
    if conflict_id is not None:
        raise RuntimeError(f"coach provenance conflict: {kind} {conflict_id}")


def _validate_provenance() -> None:
    _reject_conflict(
        "workout origin",
        """
        SELECT w.id
        FROM workouts w
        LEFT JOIN coach_conversations c ON c.id = w.originating_conversation_id
        LEFT JOIN coach_messages u ON u.id = w.originating_user_message_id
        LEFT JOIN coach_messages a ON a.id = w.originating_assistant_message_id
        WHERE (
            w.originating_conversation_id IS NOT NULL
            OR w.originating_user_message_id IS NOT NULL
            OR w.originating_assistant_message_id IS NOT NULL
        ) AND (
            c.id IS NULL
            OR u.id IS NULL
            OR a.id IS NULL
            OR c.user_id != w.user_id
            OR u.conversation_id != c.id
            OR a.conversation_id != c.id
            OR u.role != 'user'
            OR a.role != 'assistant'
            OR w.source_assistant_message_id IS NOT w.originating_assistant_message_id
        )
        LIMIT 1
        """,
    )
    _reject_conflict(
        "workout source",
        """
        SELECT w.id
        FROM workouts w
        LEFT JOIN coach_messages m ON m.id = w.source_assistant_message_id
        LEFT JOIN coach_conversations c ON c.id = m.conversation_id
        WHERE w.source_assistant_message_id IS NOT NULL
          AND (
              m.id IS NULL
              OR m.role != 'assistant'
              OR c.id IS NULL
              OR c.user_id != w.user_id
          )
        LIMIT 1
        """,
    )
    _reject_conflict(
        "assistant run",
        """
        SELECT r.id
        FROM coach_assistant_runs r
        LEFT JOIN coach_conversations c ON c.id = r.conversation_id
        LEFT JOIN coach_messages u ON u.id = r.user_message_id
        LEFT JOIN coach_messages a ON a.id = r.assistant_message_id
        LEFT JOIN workouts w ON w.id = r.workout_id
        WHERE c.id IS NULL
           OR u.id IS NULL
           OR a.id IS NULL
           OR u.conversation_id != c.id
           OR a.conversation_id != c.id
           OR u.role != 'user'
           OR a.role != 'assistant'
           OR (r.model_id IS NOT NULL AND r.model_id IS NOT a.model_id)
           OR (r.request_id IS NOT NULL AND r.request_id IS NOT a.request_id)
           OR (
               r.workout_id IS NOT NULL
               AND (
                   w.id IS NULL
                   OR w.user_id != c.user_id
                   OR w.source_assistant_message_id IS NOT r.assistant_message_id
               )
           )
        LIMIT 1
        """,
    )


def _drop_workout_referencing_triggers() -> list[str]:
    connection = op.get_bind()
    trigger_sql = []
    for name in _WORKOUT_REFERENCING_TRIGGERS:
        sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (name,)
        ).scalar_one()
        trigger_sql.append(sql)
        connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
    return trigger_sql


def _restore_triggers(trigger_sql: list[str]) -> None:
    connection = op.get_bind()
    for sql in trigger_sql:
        connection.exec_driver_sql(sql)


def upgrade() -> None:
    _validate_provenance()
    connection = op.get_bind()
    trigger_sql = _drop_workout_referencing_triggers()
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table("workouts", recreate="always") as batch_op:
            batch_op.drop_constraint(
                "fk_workouts_originating_assistant_message", type_="foreignkey"
            )
            batch_op.drop_constraint("fk_workouts_originating_user_message", type_="foreignkey")
            batch_op.drop_constraint("fk_workouts_originating_conversation", type_="foreignkey")
            batch_op.drop_column("originating_assistant_message_id")
            batch_op.drop_column("originating_user_message_id")
            batch_op.drop_column("originating_conversation_id")
    finally:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    _restore_triggers(trigger_sql)

    for column in ("workout_id", "user_message_id", "conversation_id", "assistant_message_id"):
        op.drop_index(f"ix_coach_assistant_runs_{column}", table_name="coach_assistant_runs")
    op.drop_table("coach_assistant_runs")


def downgrade() -> None:
    connection = op.get_bind()
    trigger_sql = _drop_workout_referencing_triggers()
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table("workouts", recreate="always") as batch_op:
            batch_op.add_column(sa.Column("originating_conversation_id", sa.Integer()))
            batch_op.add_column(sa.Column("originating_user_message_id", sa.Integer()))
            batch_op.add_column(sa.Column("originating_assistant_message_id", sa.Integer()))
            batch_op.create_foreign_key(
                "fk_workouts_originating_conversation",
                "coach_conversations",
                ["originating_conversation_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch_op.create_foreign_key(
                "fk_workouts_originating_user_message",
                "coach_messages",
                ["originating_user_message_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch_op.create_foreign_key(
                "fk_workouts_originating_assistant_message",
                "coach_messages",
                ["originating_assistant_message_id"],
                ["id"],
                ondelete="SET NULL",
            )
    finally:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    _restore_triggers(trigger_sql)

    op.create_table(
        "coach_assistant_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("user_message_id", sa.Integer(), nullable=False),
        sa.Column("assistant_message_id", sa.Integer(), nullable=False),
        sa.Column("workout_id", sa.Integer()),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("model_id", sa.String(length=200)),
        sa.Column("request_id", sa.String(length=100)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.ForeignKeyConstraint(
            ["assistant_message_id"], ["coach_messages.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["coach_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_message_id"], ["coach_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workout_id"], ["workouts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("assistant_message_id"),
        sa.UniqueConstraint("workout_id"),
    )
    for column in ("assistant_message_id", "conversation_id", "user_message_id", "workout_id"):
        op.create_index(f"ix_coach_assistant_runs_{column}", "coach_assistant_runs", [column])
