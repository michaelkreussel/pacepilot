"""complete activity zone detection

Revision ID: 20260822_20
Revises: 20260822_19
Create Date: 2026-08-22 22:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_20"
down_revision: str | None = "20260822_19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE activities SET zones_complete = 0 "
            "WHERE ((min_hr IS NOT NULL OR max_hr IS NOT NULL) AND NOT EXISTS ("
            "SELECT 1 FROM activity_zones "
            "WHERE activity_zones.activity_id = activities.id "
            "AND activity_zones.zone_type = 'heart_rate')) "
            "OR ((max_power_watts IS NOT NULL OR normalized_power_watts IS NOT NULL) "
            "AND NOT EXISTS (SELECT 1 FROM activity_zones "
            "WHERE activity_zones.activity_id = activities.id "
            "AND activity_zones.zone_type = 'power'))"
        )
    )


def downgrade() -> None:
    pass
