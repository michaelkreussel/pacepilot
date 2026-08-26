from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.user import utcnow

EXPERIENCE_LEVELS = ("novice", "intermediate", "advanced")
GOAL_EVENT_TYPES = ("general_fitness", "5k", "10k", "half_marathon", "marathon")
GOAL_STATUSES = ("active", "achieved", "archived")
ANCHOR_KINDS = ("race", "time_trial", "manual")


class AthletePlanningProfile(Base):
    __tablename__ = "athlete_planning_profiles"
    __table_args__ = (
        CheckConstraint(
            "experience_level IS NULL OR experience_level IN "
            "('novice', 'intermediate', 'advanced')",
            name="ck_athlete_planning_profiles_experience_level",
        ),
        CheckConstraint(
            "preferred_long_run_weekday IS NULL OR preferred_long_run_weekday BETWEEN 0 AND 6",
            name="ck_athlete_planning_profiles_long_run_weekday",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    experience_level: Mapped[str | None] = mapped_column(String(20))
    preferred_long_run_weekday: Mapped[int | None] = mapped_column(Integer)
    self_declared_reentry: Mapped[bool] = mapped_column(Boolean, default=False)
    constraint_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AthleteGoal(Base):
    __tablename__ = "athlete_goals"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('general_fitness', '5k', '10k', 'half_marathon', 'marathon')",
            name="ck_athlete_goals_event_type",
        ),
        CheckConstraint(
            "status IN ('active', 'achieved', 'archived')",
            name="ck_athlete_goals_status",
        ),
        Index("ix_athlete_goals_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    sport: Mapped[str] = mapped_column(String(30), default="running")
    event_type: Mapped[str] = mapped_column(String(30))
    event_name: Mapped[str | None] = mapped_column(String(200))
    target_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AthleteAvailability(Base):
    __tablename__ = "athlete_availability"
    __table_args__ = (
        UniqueConstraint("user_id", "weekday", name="uq_athlete_availability_user_weekday"),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_athlete_availability_weekday"),
        CheckConstraint(
            "(available = 0) OR (available_minutes IS NOT NULL AND available_minutes > 0)",
            name="ck_athlete_availability_minutes",
        ),
        CheckConstraint(
            "available_minutes IS NULL OR available_minutes BETWEEN 1 AND 1440",
            name="ck_athlete_availability_minutes_range",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)
    available: Mapped[bool] = mapped_column(Boolean, default=False)
    available_minutes: Mapped[int | None] = mapped_column(Integer)


class PerformanceAnchor(Base):
    __tablename__ = "performance_anchors"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('race', 'time_trial', 'manual')",
            name="ck_performance_anchors_kind",
        ),
        CheckConstraint("distance_m > 0", name="ck_performance_anchors_distance_positive"),
        CheckConstraint("duration_s > 0", name="ck_performance_anchors_duration_positive"),
        Index("ix_performance_anchors_user_achieved", "user_id", "achieved_on"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    distance_m: Mapped[float] = mapped_column(Float)
    duration_s: Mapped[float] = mapped_column(Float)
    achieved_on: Mapped[date] = mapped_column(Date)
    reliable: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TrainingPlan(Base):
    __tablename__ = "training_plans"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_training_plans_id_user_id"),
        UniqueConstraint("user_id", "week_start", name="uq_training_plans_user_week"),
        CheckConstraint("status IN ('active', 'archived')", name="ck_training_plans_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    week_start: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="active")
    current_revision_id: Mapped[int | None] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TrainingPlanRevision(Base):
    __tablename__ = "training_plan_revisions"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_training_plan_revisions_id_owner"),
        UniqueConstraint("plan_id", "revision_number", name="uq_training_plan_revisions_number"),
        UniqueConstraint(
            "plan_id", "input_fingerprint", name="uq_training_plan_revisions_fingerprint"
        ),
        CheckConstraint("revision_number >= 1", name="ck_training_plan_revisions_number_positive"),
        ForeignKeyConstraint(
            ["plan_id", "owner_user_id"],
            ["training_plans.id", "training_plans.user_id"],
            name="fk_training_plan_revisions_plan_owner",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(Integer, index=True)
    owner_user_id: Mapped[int] = mapped_column(Integer)
    revision_number: Mapped[int] = mapped_column(Integer)
    week_start: Mapped[date] = mapped_column(Date)
    week_end: Mapped[date] = mapped_column(Date)
    planner_version: Mapped[str] = mapped_column(String(100))
    knowledge_base_version: Mapped[str] = mapped_column(String(200))
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    generation_context_json: Mapped[dict[str, object]] = mapped_column(JSON)
    validation_report_json: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TrainingPlanWorkout(Base):
    __tablename__ = "training_plan_workouts"
    __table_args__ = (
        UniqueConstraint("plan_revision_id", "position", name="uq_training_plan_workouts_position"),
        UniqueConstraint(
            "plan_revision_id", "workout_id", name="uq_training_plan_workouts_workout"
        ),
        ForeignKeyConstraint(
            ["plan_revision_id", "owner_user_id"],
            ["training_plan_revisions.id", "training_plan_revisions.owner_user_id"],
            name="fk_training_plan_workouts_revision_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workout_id", "owner_user_id"],
            ["workouts.id", "workouts.user_id"],
            name="fk_training_plan_workouts_workout_owner",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_revision_id: Mapped[int] = mapped_column(Integer, index=True)
    workout_id: Mapped[int] = mapped_column(Integer, index=True)
    owner_user_id: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(30))
    scheduled_for: Mapped[date] = mapped_column(Date, index=True)


@event.listens_for(TrainingPlanRevision, "before_update")
@event.listens_for(TrainingPlanWorkout, "before_update")
def _prevent_plan_revision_update(
    _mapper: Any, _connection: Any, _target: TrainingPlanRevision | TrainingPlanWorkout
) -> None:
    raise ValueError("Training plan revisions and memberships are immutable")


event.listen(
    TrainingPlanRevision.__table__,
    "after_create",
    DDL(
        "CREATE TRIGGER prevent_training_plan_revisions_update "
        "BEFORE UPDATE ON training_plan_revisions "
        "BEGIN SELECT RAISE(ABORT, "
        "'Training plan revisions and memberships are immutable'); END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    TrainingPlanWorkout.__table__,
    "after_create",
    DDL(
        "CREATE TRIGGER prevent_training_plan_workouts_update "
        "BEFORE UPDATE ON training_plan_workouts "
        "BEGIN SELECT RAISE(ABORT, "
        "'Training plan revisions and memberships are immutable'); END"
    ).execute_if(dialect="sqlite"),
)


event.listen(
    TrainingPlan.__table__,
    "after_create",
    DDL(
        "CREATE TRIGGER validate_training_plan_current_revision "
        "BEFORE UPDATE OF current_revision_id ON training_plans "
        "WHEN NEW.current_revision_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM training_plan_revisions "
        "WHERE id = NEW.current_revision_id AND plan_id = NEW.id) "
        "BEGIN SELECT RAISE(ABORT, 'Current revision must belong to its plan'); END"
    ).execute_if(dialect="sqlite"),
)
