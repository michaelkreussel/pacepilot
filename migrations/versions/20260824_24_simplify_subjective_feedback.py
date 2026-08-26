"""simplify subjective feedback

Revision ID: 20260824_24
Revises: 20260822_23
Create Date: 2026-08-24 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_24"
down_revision: str | None = "20260822_23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("pre_session_feedback") as batch_op:
        for column in ("motivation", "fatigue", "leg_freshness", "soreness"):
            batch_op.alter_column(column, existing_type=sa.Integer(), nullable=True)
    with op.batch_alter_table("post_session_feedback") as batch_op:
        batch_op.alter_column("completion_percent", existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column("session_rpe", existing_type=sa.Float(), nullable=True)
        batch_op.alter_column("overall_feel", existing_type=sa.Integer(), nullable=True)
    op.execute(
        "UPDATE activities SET workout_feel = CASE workout_feel "
        "WHEN 0 THEN 1 WHEN 25 THEN 2 WHEN 50 THEN 3 WHEN 75 THEN 4 WHEN 100 THEN 5 END "
        "WHERE workout_feel IN (0, 25, 50, 75, 100)"
    )


def downgrade() -> None:
    connection = op.get_bind()
    sparse_rows = connection.scalar(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM pre_session_feedback WHERE motivation IS NULL "
            "OR fatigue IS NULL OR leg_freshness IS NULL OR soreness IS NULL) + "
            "(SELECT COUNT(*) FROM post_session_feedback WHERE completion_percent IS NULL "
            "OR session_rpe IS NULL OR overall_feel IS NULL)"
        )
    )
    if sparse_rows:
        raise RuntimeError("Cannot downgrade while simplified feedback rows exist")
    with op.batch_alter_table("post_session_feedback") as batch_op:
        batch_op.alter_column("overall_feel", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("session_rpe", existing_type=sa.Float(), nullable=False)
        batch_op.alter_column("completion_percent", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table("pre_session_feedback") as batch_op:
        for column in ("soreness", "leg_freshness", "fatigue", "motivation"):
            batch_op.alter_column(column, existing_type=sa.Integer(), nullable=False)
