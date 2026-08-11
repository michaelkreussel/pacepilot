"""normalize Garmin workout RPE to a 1-10 scale

Revision ID: 20260811_09
Revises: 20260809_08
Create Date: 2026-08-11 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_09"
down_revision: str | None = "20260809_08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE activities SET workout_rpe = ROUND(workout_rpe / 10.0) WHERE workout_rpe > 10"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE activities SET workout_rpe = workout_rpe * 10 WHERE workout_rpe IS NOT NULL"
        )
    )
