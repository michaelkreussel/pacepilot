"""repair constraints missing from legacy databases

Revision ID: 20260808_05
Revises: 20260808_04
Create Date: 2026-08-08 21:15:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_05"
down_revision: str | None = "20260808_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_workout_step_position_constraint() -> bool:
    constraints = sa.inspect(op.get_bind()).get_unique_constraints("workout_steps")
    return any(
        set(constraint.get("column_names") or ()) == {"workout_id", "position"}
        for constraint in constraints
    )


def upgrade() -> None:
    if _has_workout_step_position_constraint():
        return
    with op.batch_alter_table("workout_steps") as batch_op:
        batch_op.create_unique_constraint(
            "uq_workout_steps_workout_id_position", ["workout_id", "position"]
        )


def downgrade() -> None:
    constraints = sa.inspect(op.get_bind()).get_unique_constraints("workout_steps")
    if not any(
        constraint.get("name") == "uq_workout_steps_workout_id_position"
        for constraint in constraints
    ):
        return
    with op.batch_alter_table("workout_steps") as batch_op:
        batch_op.drop_constraint("uq_workout_steps_workout_id_position", type_="unique")
