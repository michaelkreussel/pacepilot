"""persist Garmin rate-limit cooldown

Revision ID: 20260809_07
Revises: 20260809_06
Create Date: 2026-08-09 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_07"
down_revision: str | None = "20260809_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("garmin_accounts") as batch_op:
        batch_op.add_column(sa.Column("rate_limit_until", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("garmin_accounts") as batch_op:
        batch_op.drop_column("rate_limit_until")
