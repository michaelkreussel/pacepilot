"""add selective original activity FIT storage

Revision ID: 20260812_14
Revises: 20260812_13
Create Date: 2026-08-12 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_14"
down_revision: str | None = "20260812_13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("activities") as batch_op:
        batch_op.add_column(sa.Column("fit_file", sa.String(length=500)))
        batch_op.add_column(sa.Column("fit_import_status", sa.String(length=20)))
        batch_op.add_column(sa.Column("fit_synced_at", sa.DateTime()))


def downgrade() -> None:
    with op.batch_alter_table("activities") as batch_op:
        batch_op.drop_column("fit_synced_at")
        batch_op.drop_column("fit_import_status")
        batch_op.drop_column("fit_file")
