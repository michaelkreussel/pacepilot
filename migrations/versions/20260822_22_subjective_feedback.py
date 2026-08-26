"""add subjective session feedback

Revision ID: 20260822_22
Revises: 20260822_21
Create Date: 2026-08-22 23:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_22"
down_revision: str | None = "20260822_21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pre_session_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "workout_id",
            sa.Integer(),
            sa.ForeignKey("workouts.id", ondelete="SET NULL"),
        ),
        sa.Column("motivation", sa.Integer(), nullable=False),
        sa.Column("fatigue", sa.Integer(), nullable=False),
        sa.Column("leg_freshness", sa.Integer(), nullable=False),
        sa.Column("soreness", sa.Integer(), nullable=False),
        sa.Column("sleep_quality", sa.Integer()),
        sa.Column("pain_present", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pain_location", sa.String(length=100)),
        sa.Column("pain_severity", sa.Integer()),
        sa.Column("pain_alters_gait", sa.Boolean()),
        sa.Column("pain_worsens_with_activity", sa.Boolean()),
        sa.Column("illness_signal", sa.String(length=40), nullable=False),
        sa.Column("available_minutes", sa.Integer()),
        sa.Column("notes", sa.Text()),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("motivation BETWEEN 1 AND 5", name="ck_pre_feedback_motivation"),
        sa.CheckConstraint("fatigue BETWEEN 1 AND 5", name="ck_pre_feedback_fatigue"),
        sa.CheckConstraint("leg_freshness BETWEEN 1 AND 5", name="ck_pre_feedback_leg_freshness"),
        sa.CheckConstraint("soreness BETWEEN 0 AND 10", name="ck_pre_feedback_soreness"),
        sa.CheckConstraint(
            "sleep_quality IS NULL OR sleep_quality BETWEEN 1 AND 5",
            name="ck_pre_feedback_sleep_quality",
        ),
        sa.CheckConstraint(
            "pain_severity IS NULL OR pain_severity BETWEEN 0 AND 10",
            name="ck_pre_feedback_pain_severity",
        ),
        sa.CheckConstraint(
            "available_minutes IS NULL OR available_minutes BETWEEN 0 AND 1440",
            name="ck_pre_feedback_available_minutes",
        ),
        sa.CheckConstraint(
            "illness_signal IN ('none', 'mild_upper_respiratory', 'fever', 'systemic', "
            "'cardiopulmonary_warning', 'unknown')",
            name="ck_pre_feedback_illness_signal",
        ),
    )
    op.create_index(
        "ix_pre_feedback_user_recorded",
        "pre_session_feedback",
        ["user_id", "recorded_at"],
    )
    op.create_index(
        "ix_pre_feedback_workout_recorded",
        "pre_session_feedback",
        ["workout_id", "recorded_at"],
    )
    op.create_index("ix_pre_session_feedback_workout_id", "pre_session_feedback", ["workout_id"])

    op.create_table(
        "post_session_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "workout_id",
            sa.Integer(),
            sa.ForeignKey("workouts.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "activity_id",
            sa.Integer(),
            sa.ForeignKey("activities.id", ondelete="SET NULL"),
        ),
        sa.Column("completion_percent", sa.Integer(), nullable=False),
        sa.Column("session_rpe", sa.Float(), nullable=False),
        sa.Column("overall_feel", sa.Integer(), nullable=False),
        sa.Column("pain_present", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pain_location", sa.String(length=100)),
        sa.Column("pain_severity", sa.Integer()),
        sa.Column("pain_alters_gait", sa.Boolean()),
        sa.Column("pain_worsens_with_activity", sa.Boolean()),
        sa.Column("stopped_reason", sa.String(length=500)),
        sa.Column("notes", sa.Text()),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "completion_percent BETWEEN 0 AND 100", name="ck_post_feedback_completion"
        ),
        sa.CheckConstraint("session_rpe BETWEEN 0 AND 10", name="ck_post_feedback_rpe"),
        sa.CheckConstraint("overall_feel BETWEEN 1 AND 5", name="ck_post_feedback_feel"),
        sa.CheckConstraint(
            "pain_severity IS NULL OR pain_severity BETWEEN 0 AND 10",
            name="ck_post_feedback_pain_severity",
        ),
    )
    op.create_index(
        "ix_post_feedback_user_recorded",
        "post_session_feedback",
        ["user_id", "recorded_at"],
    )
    op.create_index(
        "ix_post_feedback_activity_recorded",
        "post_session_feedback",
        ["activity_id", "recorded_at"],
    )
    op.create_index(
        "ix_post_session_feedback_activity_id", "post_session_feedback", ["activity_id"]
    )
    op.create_index("ix_post_session_feedback_workout_id", "post_session_feedback", ["workout_id"])


def downgrade() -> None:
    op.drop_table("post_session_feedback")
    op.drop_table("pre_session_feedback")
