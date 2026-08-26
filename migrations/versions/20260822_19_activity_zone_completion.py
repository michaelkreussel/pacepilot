"""track activity zone completion

Revision ID: 20260822_19
Revises: 20260822_18
Create Date: 2026-08-22 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_19"
down_revision: str | None = "20260822_18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "activities",
        sa.Column("zones_complete", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.execute(
        sa.text(
            "UPDATE activities SET zones_complete = 1 "
            "WHERE (average_hr IS NULL OR EXISTS ("
            "SELECT 1 FROM activity_zones "
            "WHERE activity_zones.activity_id = activities.id "
            "AND activity_zones.zone_type = 'heart_rate')) "
            "AND (average_power_watts IS NULL OR EXISTS ("
            "SELECT 1 FROM activity_zones "
            "WHERE activity_zones.activity_id = activities.id "
            "AND activity_zones.zone_type = 'power'))"
        )
    )


def downgrade() -> None:
    op.drop_column("activities", "zones_complete")
