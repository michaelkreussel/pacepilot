"""add durable coach assistant runs

Revision ID: 20260824_25
Revises: 20260824_24
Create Date: 2026-08-24 21:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_25"
down_revision: str | None = "20260824_24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coach_assistant_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("user_message_id", sa.Integer(), nullable=False),
        sa.Column("assistant_message_id", sa.Integer(), nullable=False),
        sa.Column("workout_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=True),
        sa.Column("request_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
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
    op.create_index(
        op.f("ix_coach_assistant_runs_assistant_message_id"),
        "coach_assistant_runs",
        ["assistant_message_id"],
    )
    op.create_index(
        op.f("ix_coach_assistant_runs_conversation_id"),
        "coach_assistant_runs",
        ["conversation_id"],
    )
    op.create_index(
        op.f("ix_coach_assistant_runs_user_message_id"),
        "coach_assistant_runs",
        ["user_message_id"],
    )
    op.create_index(
        op.f("ix_coach_assistant_runs_workout_id"),
        "coach_assistant_runs",
        ["workout_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_coach_assistant_runs_workout_id"), table_name="coach_assistant_runs")
    op.drop_index(
        op.f("ix_coach_assistant_runs_user_message_id"),
        table_name="coach_assistant_runs",
    )
    op.drop_index(
        op.f("ix_coach_assistant_runs_conversation_id"),
        table_name="coach_assistant_runs",
    )
    op.drop_index(
        op.f("ix_coach_assistant_runs_assistant_message_id"),
        table_name="coach_assistant_runs",
    )
    op.drop_table("coach_assistant_runs")
