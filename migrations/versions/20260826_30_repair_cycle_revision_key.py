"""repair the composite key used by historical cycle revisions

Revision ID: 20260826_30
Revises: 20260826_29
Create Date: 2026-08-26 18:45:00
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

revision: str = "20260826_30"
down_revision: str | None = "20260826_29"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    inspector = inspect(connection)
    indexes = {item["name"] for item in inspector.get_indexes("training_cycle_revisions")}
    constraints = {
        item["name"] for item in inspector.get_unique_constraints("training_cycle_revisions")
    }
    if (
        "uq_training_cycle_revisions_id_cycle_owner" not in indexes
        and "uq_training_cycle_revisions_id_cycle_owner" not in constraints
    ):
        op.create_index(
            "uq_training_cycle_revisions_id_cycle_owner",
            "training_cycle_revisions",
            ["id", "cycle_id", "owner_user_id"],
            unique=True,
        )
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_training_cycle_revisions_id_cycle_owner")
