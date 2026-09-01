"""add durable Garmin training-fit authorization

Revision ID: 20260901_37
Revises: 20260829_36
Create Date: 2026-09-01 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_37"
down_revision: str | None = "20260829_36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workout_garmin_operations",
        sa.Column("training_fit_policy_version", sa.String(length=100)),
    )
    op.add_column(
        "workout_garmin_operations",
        sa.Column("training_fit_assessment_fingerprint", sa.String(length=64)),
    )
    op.add_column("workout_garmin_operations", sa.Column("training_fit_effective_date", sa.Date()))
    op.add_column(
        "workout_garmin_operations",
        sa.Column("training_fit_acknowledged_by_user_id", sa.Integer()),
    )
    op.add_column(
        "workout_garmin_operations", sa.Column("training_fit_acknowledged_at", sa.DateTime())
    )
    op.add_column(
        "workout_garmin_operations",
        sa.Column("training_fit_authorized_revision_id", sa.Integer()),
    )
    authorization_valid = (
        "(NEW.training_fit_policy_version IS NULL AND "
        "NEW.training_fit_assessment_fingerprint IS NULL AND "
        "NEW.training_fit_effective_date IS NULL AND "
        "NEW.training_fit_acknowledged_by_user_id IS NULL AND "
        "NEW.training_fit_acknowledged_at IS NULL AND "
        "NEW.training_fit_authorized_revision_id IS NULL) OR "
        "(NEW.training_fit_policy_version IS NOT NULL AND "
        "length(NEW.training_fit_policy_version) > 0 AND "
        "NEW.training_fit_assessment_fingerprint IS NOT NULL AND "
        "length(NEW.training_fit_assessment_fingerprint) = 64 AND "
        "NEW.training_fit_effective_date IS NOT NULL AND "
        "NEW.training_fit_acknowledged_by_user_id IS NOT NULL AND "
        "NEW.training_fit_acknowledged_at IS NOT NULL AND "
        "NEW.training_fit_authorized_revision_id = NEW.revision_id AND "
        "EXISTS (SELECT 1 FROM workouts WHERE id = NEW.workout_id "
        "AND user_id = NEW.training_fit_acknowledged_by_user_id))"
    )
    op.execute(
        "CREATE TRIGGER validate_workout_garmin_operation_training_fit_insert "
        "BEFORE INSERT ON workout_garmin_operations "
        f"WHEN NOT ({authorization_valid}) "
        "BEGIN SELECT RAISE(ABORT, 'Invalid Garmin training-fit authorization'); END"
    )
    op.execute(
        "CREATE TRIGGER validate_workout_garmin_operation_training_fit_update "
        "BEFORE UPDATE OF workout_id, revision_id, training_fit_policy_version, "
        "training_fit_assessment_fingerprint, training_fit_effective_date, "
        "training_fit_acknowledged_by_user_id, training_fit_acknowledged_at, "
        "training_fit_authorized_revision_id ON workout_garmin_operations "
        f"WHEN NOT ({authorization_valid}) "
        "BEGIN SELECT RAISE(ABORT, 'Invalid Garmin training-fit authorization'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS validate_workout_garmin_operation_training_fit_update")
    op.execute("DROP TRIGGER IF EXISTS validate_workout_garmin_operation_training_fit_insert")
    op.drop_column("workout_garmin_operations", "training_fit_authorized_revision_id")
    op.drop_column("workout_garmin_operations", "training_fit_acknowledged_at")
    op.drop_column("workout_garmin_operations", "training_fit_acknowledged_by_user_id")
    op.drop_column("workout_garmin_operations", "training_fit_effective_date")
    op.drop_column("workout_garmin_operations", "training_fit_assessment_fingerprint")
    op.drop_column("workout_garmin_operations", "training_fit_policy_version")
