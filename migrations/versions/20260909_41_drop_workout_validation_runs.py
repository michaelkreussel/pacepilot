"""drop contextual workout validation runs

Revision ID: 20260909_41
Revises: 20260906_40
Create Date: 2026-09-09 21:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260909_41"
down_revision: str | None = "20260906_40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("workout_validation_runs")


def downgrade() -> None:
    op.create_table(
        "workout_validation_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workout_id", sa.Integer(), nullable=False),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("validation_kind", sa.String(length=30), nullable=False),
        sa.Column("rule_set_version", sa.String(length=100), nullable=False),
        sa.Column("context_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("feedback_ids_json", sa.JSON(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime()),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["revision_id", "workout_id"],
            ["workout_revisions.id", "workout_revisions.workout_id"],
            name="fk_workout_validation_runs_revision_same_workout",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workout_validation_runs_workout_id", "workout_validation_runs", ["workout_id"]
    )
    op.create_index(
        "ix_workout_validation_runs_revision_id", "workout_validation_runs", ["revision_id"]
    )
    op.create_index(
        "ix_workout_validation_runs_revision_kind_evaluated",
        "workout_validation_runs",
        ["revision_id", "validation_kind", "evaluated_at"],
    )
    op.create_index(
        "ix_workout_validation_runs_context_fingerprint",
        "workout_validation_runs",
        ["context_fingerprint"],
    )
