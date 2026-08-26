"""store Garmin heart rate zones

Revision ID: 20260822_21
Revises: 20260822_20
Create Date: 2026-08-22 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_21"
down_revision: str | None = "20260822_20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("garmin_accounts", sa.Column("heart_rate_zone_profiles", sa.JSON()))
    op.add_column("garmin_accounts", sa.Column("heart_rate_zones_synced_at", sa.DateTime()))


def downgrade() -> None:
    op.drop_column("garmin_accounts", "heart_rate_zones_synced_at")
    op.drop_column("garmin_accounts", "heart_rate_zone_profiles")
