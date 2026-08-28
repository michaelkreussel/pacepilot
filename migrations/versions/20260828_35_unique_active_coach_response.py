"""enforce one active coach response per conversation

Revision ID: 20260828_35
Revises: 20260828_34
Create Date: 2026-08-28 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_35"
down_revision: str | None = "20260828_34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE coach_messages AS duplicate
        SET status = 'interrupted',
            failure_category = 'interrupted',
            completed_at = COALESCE(
                duplicate.completed_at,
                (
                    SELECT kept.created_at
                    FROM coach_messages AS kept
                    WHERE kept.id = (
                        SELECT MAX(active.id)
                        FROM coach_messages AS active
                        WHERE active.conversation_id = duplicate.conversation_id
                          AND active.role = 'assistant'
                          AND active.status = 'streaming'
                    )
                )
            )
        WHERE duplicate.role = 'assistant'
          AND duplicate.status = 'streaming'
          AND duplicate.id < (
              SELECT MAX(active.id)
              FROM coach_messages AS active
              WHERE active.conversation_id = duplicate.conversation_id
                AND active.role = 'assistant'
                AND active.status = 'streaming'
          )
        """
    )
    op.create_index(
        "uq_coach_messages_active_assistant_per_conversation",
        "coach_messages",
        ["conversation_id"],
        unique=True,
        sqlite_where=sa.text("role = 'assistant' AND status = 'streaming'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_coach_messages_active_assistant_per_conversation",
        table_name="coach_messages",
    )
