"""expand coach message execution and artifact lineage

Revision ID: 20260828_32
Revises: 20260826_31
Create Date: 2026-08-28 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_32"
down_revision: str | None = "20260826_31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _reject_conflict(kind: str, query: str) -> None:
    conflict_id = op.get_bind().exec_driver_sql(query).scalar()
    if conflict_id is not None:
        raise RuntimeError(f"coach lineage conflict: {kind} {conflict_id}")


def _validate_lineage() -> None:
    _reject_conflict(
        "run",
        """
        SELECT r.id
        FROM coach_assistant_runs r
        LEFT JOIN coach_conversations c ON c.id = r.conversation_id
        LEFT JOIN coach_messages u ON u.id = r.user_message_id
        LEFT JOIN coach_messages a ON a.id = r.assistant_message_id
        WHERE c.id IS NULL
           OR u.id IS NULL
           OR a.id IS NULL
           OR u.conversation_id != r.conversation_id
           OR a.conversation_id != r.conversation_id
           OR u.role != 'user'
           OR a.role != 'assistant'
        LIMIT 1
        """,
    )
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
        )
        LIMIT 1
        """,
    )
    _reject_conflict(
        "run workout",
        """
        SELECT r.id
        FROM coach_assistant_runs r
        JOIN coach_conversations c ON c.id = r.conversation_id
        LEFT JOIN workouts w ON w.id = r.workout_id
        WHERE r.workout_id IS NOT NULL
          AND (
              w.id IS NULL
              OR c.user_id != w.user_id
              OR w.originating_conversation_id IS NOT r.conversation_id
              OR w.originating_user_message_id IS NOT r.user_message_id
              OR w.originating_assistant_message_id IS NOT r.assistant_message_id
          )
        LIMIT 1
        """,
    )
    _reject_conflict(
        "workout run",
        """
        SELECT w.id
        FROM workouts w
        JOIN coach_assistant_runs r
          ON r.assistant_message_id = w.originating_assistant_message_id
        WHERE r.workout_id IS NOT w.id
           OR r.conversation_id IS NOT w.originating_conversation_id
           OR r.user_message_id IS NOT w.originating_user_message_id
        LIMIT 1
        """,
    )


def upgrade() -> None:
    _validate_lineage()
    connection = op.get_bind()

    op.add_column("coach_messages", sa.Column("request_id", sa.String(length=100)))
    op.add_column("coach_messages", sa.Column("prompt_template_version", sa.String(length=100)))
    op.add_column("coach_messages", sa.Column("operation_contract_version", sa.String(length=100)))
    op.add_column("coach_messages", sa.Column("failure_category", sa.String(length=100)))
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    with op.batch_alter_table("workouts") as batch_op:
        batch_op.add_column(sa.Column("source_assistant_message_id", sa.Integer()))
        batch_op.create_foreign_key(
            "fk_workouts_source_assistant_message",
            "coach_messages",
            ["source_assistant_message_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_workouts_source_assistant_message_id",
        "workouts",
        ["source_assistant_message_id"],
    )

    op.execute(
        """
        UPDATE coach_messages
        SET request_id = (
                SELECT r.request_id
                FROM coach_assistant_runs r
                WHERE r.assistant_message_id = coach_messages.id
            ),
            prompt_template_version = (
                SELECT wr.prompt_template_version
                FROM workouts w
                JOIN workout_revisions wr
                  ON wr.workout_id = w.id AND wr.revision_number = 1
                WHERE w.originating_assistant_message_id = coach_messages.id
                ORDER BY w.id
                LIMIT 1
            ),
            failure_category = CASE
                WHEN status IN ('failed', 'interrupted') THEN status
                ELSE NULL
            END
        WHERE role = 'assistant'
        """
    )
    op.execute(
        """
        UPDATE workouts
        SET source_assistant_message_id = originating_assistant_message_id
        WHERE originating_assistant_message_id IS NOT NULL
        """
    )
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    op.drop_index("ix_workouts_source_assistant_message_id", table_name="workouts")
    with op.batch_alter_table("workouts") as batch_op:
        batch_op.drop_constraint("fk_workouts_source_assistant_message", type_="foreignkey")
        batch_op.drop_column("source_assistant_message_id")
    op.drop_column("coach_messages", "failure_category")
    op.drop_column("coach_messages", "operation_contract_version")
    op.drop_column("coach_messages", "prompt_template_version")
    op.drop_column("coach_messages", "request_id")
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
