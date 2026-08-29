"""store Coach planning result artifacts

Revision ID: 20260829_36
Revises: 20260828_35
Create Date: 2026-08-29 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260829_36"
down_revision: str | None = "20260828_35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "coach_messages",
        sa.Column(
            "artifacts_json",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("coach_messages", "artifacts_json")
