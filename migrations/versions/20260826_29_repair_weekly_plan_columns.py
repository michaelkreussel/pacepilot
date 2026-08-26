"""repair weekly plan columns on databases created by an earlier phase-11 build

Revision ID: 20260826_29
Revises: 20260826_28
Create Date: 2026-08-26 18:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260826_29"
down_revision: str | None = "20260826_28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_column_if_missing(table: str, column: sa.Column) -> bool:
    existing = {item["name"] for item in inspect(op.get_bind()).get_columns(table)}
    if column.name in existing:
        return False
    op.add_column(table, column)
    return True


def _create_index_if_missing(table: str, name: str, columns: list[str]) -> None:
    existing = {item["name"] for item in inspect(op.get_bind()).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns)


def _create_unique_index_if_missing(table: str, name: str, columns: list[str]) -> None:
    inspector = inspect(op.get_bind())
    indexes = {item["name"] for item in inspector.get_indexes(table)}
    constraints = {item["name"] for item in inspector.get_unique_constraints(table)}
    if name not in indexes and name not in constraints:
        op.create_index(name, table, columns, unique=True)


def upgrade() -> None:
    _add_column_if_missing(
        "training_plans",
        sa.Column("current_revision_id", sa.Integer(), nullable=True),
    )
    revision_owner_added = _add_column_if_missing(
        "training_plan_revisions",
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
    )
    workout_owner_added = _add_column_if_missing(
        "training_plan_workouts",
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
    )

    connection = op.get_bind()
    # Databases from the intermediate phase-11 build have cycle foreign keys
    # pointing at columns that were not present in the weekly tables yet.
    # Backfill while checks are disabled, then restore a valid parent key.
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    connection.execute(
        sa.text(
            "UPDATE training_plans SET current_revision_id = "
            "(SELECT MAX(id) FROM training_plan_revisions "
            "WHERE training_plan_revisions.plan_id = training_plans.id) "
            "WHERE current_revision_id IS NULL"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE training_plan_revisions SET owner_user_id = "
            "(SELECT user_id FROM training_plans "
            "WHERE training_plans.id = training_plan_revisions.plan_id) "
            "WHERE owner_user_id IS NULL"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE training_plan_workouts SET owner_user_id = "
            "(SELECT workouts.user_id FROM workouts "
            "WHERE workouts.id = training_plan_workouts.workout_id) "
            "WHERE owner_user_id IS NULL"
        )
    )

    del revision_owner_added, workout_owner_added

    _create_unique_index_if_missing(
        "training_plan_revisions",
        "uq_training_plan_revisions_id_owner",
        ["id", "owner_user_id"],
    )

    _create_index_if_missing(
        "training_plans", "ix_training_plans_current_revision_id", ["current_revision_id"]
    )
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    if "ix_training_plans_current_revision_id" in {
        item["name"] for item in inspect(op.get_bind()).get_indexes("training_plans")
    }:
        op.drop_index("ix_training_plans_current_revision_id", table_name="training_plans")
