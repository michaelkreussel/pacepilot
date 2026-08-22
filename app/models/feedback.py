from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.user import utcnow

ILLNESS_SIGNALS = (
    "none",
    "mild_upper_respiratory",
    "fever",
    "systemic",
    "cardiopulmonary_warning",
    "unknown",
)


class PreSessionFeedback(Base):
    __tablename__ = "pre_session_feedback"
    __table_args__ = (
        CheckConstraint("motivation BETWEEN 1 AND 5", name="ck_pre_feedback_motivation"),
        CheckConstraint("fatigue BETWEEN 1 AND 5", name="ck_pre_feedback_fatigue"),
        CheckConstraint("leg_freshness BETWEEN 1 AND 5", name="ck_pre_feedback_leg_freshness"),
        CheckConstraint("soreness BETWEEN 0 AND 10", name="ck_pre_feedback_soreness"),
        CheckConstraint(
            "sleep_quality IS NULL OR sleep_quality BETWEEN 1 AND 5",
            name="ck_pre_feedback_sleep_quality",
        ),
        CheckConstraint(
            "workout_id IS NULL OR (workout_user_id IS NOT NULL AND workout_user_id = user_id)",
            name="ck_pre_feedback_workout_owner",
        ),
        ForeignKeyConstraint(
            ["workout_id", "workout_user_id"],
            ["workouts.id", "workouts.user_id"],
            name="fk_pre_feedback_workout_owner",
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "pain_severity IS NULL OR pain_severity BETWEEN 0 AND 10",
            name="ck_pre_feedback_pain_severity",
        ),
        CheckConstraint(
            "available_minutes IS NULL OR available_minutes BETWEEN 0 AND 1440",
            name="ck_pre_feedback_available_minutes",
        ),
        CheckConstraint(
            "illness_signal IN ('none', 'mild_upper_respiratory', 'fever', 'systemic', "
            "'cardiopulmonary_warning', 'unknown')",
            name="ck_pre_feedback_illness_signal",
        ),
        Index("ix_pre_feedback_user_recorded", "user_id", "recorded_at"),
        Index("ix_pre_feedback_workout_recorded", "workout_id", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    workout_id: Mapped[int | None] = mapped_column(
        ForeignKey("workouts.id", ondelete="SET NULL"), index=True
    )
    workout_user_id: Mapped[int | None] = mapped_column(Integer)
    motivation: Mapped[int] = mapped_column(Integer)
    fatigue: Mapped[int] = mapped_column(Integer)
    leg_freshness: Mapped[int] = mapped_column(Integer)
    soreness: Mapped[int] = mapped_column(Integer)
    sleep_quality: Mapped[int | None] = mapped_column(Integer)
    pain_present: Mapped[bool] = mapped_column(Boolean, default=False)
    pain_location: Mapped[str | None] = mapped_column(String(100))
    pain_severity: Mapped[int | None] = mapped_column(Integer)
    pain_alters_gait: Mapped[bool | None] = mapped_column(Boolean)
    pain_worsens_with_activity: Mapped[bool | None] = mapped_column(Boolean)
    illness_signal: Mapped[str] = mapped_column(String(40), default="none")
    available_minutes: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(30), default="explicit_form")
    content_hash: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PostSessionFeedback(Base):
    __tablename__ = "post_session_feedback"
    __table_args__ = (
        CheckConstraint("completion_percent BETWEEN 0 AND 100", name="ck_post_feedback_completion"),
        CheckConstraint("session_rpe BETWEEN 0 AND 10", name="ck_post_feedback_rpe"),
        CheckConstraint("overall_feel BETWEEN 1 AND 5", name="ck_post_feedback_feel"),
        CheckConstraint(
            "pain_severity IS NULL OR pain_severity BETWEEN 0 AND 10",
            name="ck_post_feedback_pain_severity",
        ),
        CheckConstraint(
            "workout_id IS NULL OR (workout_user_id IS NOT NULL AND workout_user_id = user_id)",
            name="ck_post_feedback_workout_owner",
        ),
        CheckConstraint(
            "activity_id IS NULL OR (activity_user_id IS NOT NULL AND activity_user_id = user_id)",
            name="ck_post_feedback_activity_owner",
        ),
        ForeignKeyConstraint(
            ["workout_id", "workout_user_id"],
            ["workouts.id", "workouts.user_id"],
            name="fk_post_feedback_workout_owner",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["activity_id", "activity_user_id"],
            ["activities.id", "activities.user_id"],
            name="fk_post_feedback_activity_owner",
            ondelete="SET NULL",
        ),
        Index("ix_post_feedback_user_recorded", "user_id", "recorded_at"),
        Index("ix_post_feedback_activity_recorded", "activity_id", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    workout_id: Mapped[int | None] = mapped_column(
        ForeignKey("workouts.id", ondelete="SET NULL"), index=True
    )
    workout_user_id: Mapped[int | None] = mapped_column(Integer)
    activity_id: Mapped[int | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), index=True
    )
    activity_user_id: Mapped[int | None] = mapped_column(Integer)
    completion_percent: Mapped[int] = mapped_column(Integer)
    session_rpe: Mapped[float] = mapped_column(Float)
    overall_feel: Mapped[int] = mapped_column(Integer)
    pain_present: Mapped[bool] = mapped_column(Boolean, default=False)
    pain_location: Mapped[str | None] = mapped_column(String(100))
    pain_severity: Mapped[int | None] = mapped_column(Integer)
    pain_alters_gait: Mapped[bool | None] = mapped_column(Boolean)
    pain_worsens_with_activity: Mapped[bool | None] = mapped_column(Boolean)
    stopped_reason: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(30), default="explicit_form")
    content_hash: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
