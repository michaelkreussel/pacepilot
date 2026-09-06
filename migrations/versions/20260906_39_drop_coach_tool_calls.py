"""drop coach tool-call telemetry

Revision ID: 20260906_39
Revises: 20260906_38
Create Date: 2026-09-06 21:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_39"
down_revision: str | None = "20260906_38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # All rows are disposable telemetry, including incomplete tool-call rows.
    op.drop_index(op.f("ix_coach_tool_calls_message_id"), table_name="coach_tool_calls")
    op.drop_table("coach_tool_calls")


def downgrade() -> None:
    op.create_table(
        "coach_tool_calls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("call_id", sa.String(length=100), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("input_summary", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(["message_id"], ["coach_messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", "call_id"),
    )
    op.create_index(
        op.f("ix_coach_tool_calls_message_id"),
        "coach_tool_calls",
        ["message_id"],
    )
