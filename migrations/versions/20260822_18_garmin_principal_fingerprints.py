"""add Garmin principal fingerprints

Revision ID: 20260822_18
Revises: 20260822_17
Create Date: 2026-08-22 21:00:00
"""

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_18"
down_revision: str | None = "20260822_17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table_name)}
    if column.name not in columns:
        op.add_column(table_name, column)


def upgrade() -> None:
    _add_column_if_missing(
        "garmin_accounts",
        sa.Column("principal_fingerprint", sa.String(length=64)),
    )
    _add_column_if_missing(
        "workout_garmin_remote_identities",
        sa.Column("principal_fingerprint", sa.String(length=64)),
    )

    connection = op.get_bind()
    for account_id, email in connection.execute(
        sa.text("SELECT id, email FROM garmin_accounts WHERE email IS NOT NULL")
    ):
        fingerprint = hashlib.sha256(str(email).strip().lower().encode()).hexdigest()
        connection.execute(
            sa.text(
                "UPDATE garmin_accounts SET principal_fingerprint = :fingerprint WHERE id = :id"
            ),
            {"fingerprint": fingerprint, "id": account_id},
        )
    connection.execute(
        sa.text(
            "UPDATE workout_garmin_remote_identities SET principal_fingerprint = "
            "(SELECT principal_fingerprint FROM garmin_accounts "
            "WHERE garmin_accounts.id = workout_garmin_remote_identities.garmin_account_id)"
        )
    )


def downgrade() -> None:
    op.drop_column("workout_garmin_remote_identities", "principal_fingerprint")
    op.drop_column("garmin_accounts", "principal_fingerprint")
