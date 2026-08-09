"""persist user onboarding progress

Revision ID: 20260809_08
Revises: 20260809_07
Create Date: 2026-08-09 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_08"
down_revision: str | None = "20260809_07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column("onboarding_notice_acknowledged_at", sa.DateTime(), nullable=True)
        )
        batch_op.add_column(sa.Column("onboarding_completed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "onboarding_completed_version",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )

    # Existing accounts predate onboarding and must not be locked out after deployment.
    op.execute(
        sa.text(
            "UPDATE users SET onboarding_notice_acknowledged_at = CURRENT_TIMESTAMP, "
            "onboarding_completed_at = CURRENT_TIMESTAMP, onboarding_completed_version = 1"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("onboarding_completed_version")
        batch_op.drop_column("onboarding_completed_at")
        batch_op.drop_column("onboarding_notice_acknowledged_at")
