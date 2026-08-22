"""add idempotent Garmin workout operations

Revision ID: 20260822_17
Revises: 20260822_16
Create Date: 2026-08-22 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_17"
down_revision: str | None = "20260822_16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workout_garmin_operations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workout_id", sa.Integer(), nullable=False),
        sa.Column("binding_id", sa.Integer(), nullable=False),
        sa.Column("operation_type", sa.String(length=30), nullable=False),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("remote_identity_id", sa.Integer()),
        sa.Column("scheduled_for", sa.Date()),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("remote_reference", sa.String(length=200)),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("error_code", sa.String(length=100)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "operation_type IN ('upload', 'update', 'schedule', 'unschedule', 'push', 'delete')",
            name="ck_workout_garmin_operations_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'retryable', 'unknown', 'failed_final')",
            name="ck_workout_garmin_operations_status",
        ),
        sa.CheckConstraint(
            "(operation_type = 'upload' AND remote_identity_id IS NULL) OR "
            "(operation_type <> 'upload' AND remote_identity_id IS NOT NULL)",
            name="ck_workout_garmin_operations_identity_required",
        ),
        sa.CheckConstraint("length(idempotency_key) > 0", name="ck_workout_garmin_operations_key"),
        sa.ForeignKeyConstraint(
            ["binding_id", "workout_id"],
            ["workout_garmin_bindings.id", "workout_garmin_bindings.workout_id"],
            name="fk_workout_garmin_operations_binding_same_workout",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id", "workout_id"],
            ["workout_revisions.id", "workout_revisions.workout_id"],
            name="fk_workout_garmin_operations_revision_same_workout",
        ),
        sa.ForeignKeyConstraint(
            ["remote_identity_id", "binding_id"],
            ["workout_garmin_remote_identities.id", "workout_garmin_remote_identities.binding_id"],
            name="fk_workout_garmin_operations_identity_same_binding",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_workout_garmin_operations_key"),
    )
    for column in ("workout_id", "binding_id", "revision_id", "remote_identity_id", "status"):
        op.create_index(
            f"ix_workout_garmin_operations_{column}", "workout_garmin_operations", [column]
        )
    op.create_index(
        "ix_workout_garmin_operations_binding_status",
        "workout_garmin_operations",
        ["binding_id", "status"],
    )

    op.create_table(
        "workout_garmin_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("attempt_kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("error_code", sa.String(length=100)),
        sa.Column("error_message", sa.String(length=1000)),
        sa.CheckConstraint(
            "attempt_kind IN ('execute', 'reconcile')",
            name="ck_workout_garmin_attempts_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'retryable', 'unknown', 'failed')",
            name="ck_workout_garmin_attempts_status",
        ),
        sa.CheckConstraint("attempt_number >= 1", name="ck_workout_garmin_attempts_number"),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["workout_garmin_operations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id", "attempt_number", name="uq_workout_garmin_attempts_number"
        ),
    )
    op.create_index(
        "ix_workout_garmin_attempts_operation_id", "workout_garmin_attempts", ["operation_id"]
    )
    op.create_index(
        "ix_workout_garmin_attempts_operation_status",
        "workout_garmin_attempts",
        ["operation_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("workout_garmin_attempts")
    op.drop_table("workout_garmin_operations")
