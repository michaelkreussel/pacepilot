"""enforce feedback link ownership

Revision ID: 20260822_23
Revises: 20260822_22
Create Date: 2026-08-22 23:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_23"
down_revision: str | None = "20260822_22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("uq_activities_id_user_id", "activities", ["id", "user_id"], unique=True)
    op.add_column("pre_session_feedback", sa.Column("workout_user_id", sa.Integer()))
    op.execute(
        "UPDATE pre_session_feedback SET workout_user_id = user_id WHERE workout_id IS NOT NULL"
    )
    with op.batch_alter_table("pre_session_feedback") as batch_op:
        batch_op.create_check_constraint(
            "ck_pre_feedback_workout_owner",
            "workout_id IS NULL OR (workout_user_id IS NOT NULL AND workout_user_id = user_id)",
        )
        batch_op.create_foreign_key(
            "fk_pre_feedback_workout_owner",
            "workouts",
            ["workout_id", "workout_user_id"],
            ["id", "user_id"],
            ondelete="SET NULL",
        )

    op.add_column("post_session_feedback", sa.Column("workout_user_id", sa.Integer()))
    op.add_column("post_session_feedback", sa.Column("activity_user_id", sa.Integer()))
    op.execute(
        "UPDATE post_session_feedback SET workout_user_id = user_id WHERE workout_id IS NOT NULL"
    )
    op.execute(
        "UPDATE post_session_feedback SET activity_user_id = user_id WHERE activity_id IS NOT NULL"
    )
    with op.batch_alter_table("post_session_feedback") as batch_op:
        batch_op.create_check_constraint(
            "ck_post_feedback_workout_owner",
            "workout_id IS NULL OR (workout_user_id IS NOT NULL AND workout_user_id = user_id)",
        )
        batch_op.create_check_constraint(
            "ck_post_feedback_activity_owner",
            "activity_id IS NULL OR (activity_user_id IS NOT NULL AND activity_user_id = user_id)",
        )
        batch_op.create_foreign_key(
            "fk_post_feedback_workout_owner",
            "workouts",
            ["workout_id", "workout_user_id"],
            ["id", "user_id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_post_feedback_activity_owner",
            "activities",
            ["activity_id", "activity_user_id"],
            ["id", "user_id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("post_session_feedback") as batch_op:
        batch_op.drop_constraint("fk_post_feedback_activity_owner", type_="foreignkey")
        batch_op.drop_constraint("fk_post_feedback_workout_owner", type_="foreignkey")
        batch_op.drop_constraint("ck_post_feedback_activity_owner", type_="check")
        batch_op.drop_constraint("ck_post_feedback_workout_owner", type_="check")
        batch_op.drop_column("activity_user_id")
        batch_op.drop_column("workout_user_id")
    with op.batch_alter_table("pre_session_feedback") as batch_op:
        batch_op.drop_constraint("fk_pre_feedback_workout_owner", type_="foreignkey")
        batch_op.drop_constraint("ck_pre_feedback_workout_owner", type_="check")
        batch_op.drop_column("workout_user_id")
    op.drop_index("uq_activities_id_user_id", table_name="activities")
