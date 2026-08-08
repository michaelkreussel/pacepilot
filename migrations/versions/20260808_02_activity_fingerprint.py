"""Add stable activity source fingerprint.

Revision ID: 20260808_02
Revises: 20260808_01
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_02"
down_revision: str | None = "20260808_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("activities", sa.Column("source_fingerprint", sa.String(64)))


def downgrade() -> None:
    op.drop_column("activities", "source_fingerprint")
