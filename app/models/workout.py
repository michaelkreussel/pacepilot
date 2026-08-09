from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.user import utcnow

if TYPE_CHECKING:
    from app.models.activity import Activity
    from app.models.user import User


class Workout(Base):
    __tablename__ = "workouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    sport: Mapped[str] = mapped_column(String(30), default="running")
    scheduled_for: Mapped[date | None] = mapped_column(Date, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    garmin_workout_id: Mapped[str | None] = mapped_column(String(100))
    definition_version: Mapped[int] = mapped_column(Integer, default=1)
    definition: Mapped[dict[str, object]] = mapped_column(JSON, default=lambda: {"blocks": []})
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship(back_populates="workouts")
    steps: Mapped[list["WorkoutStep"]] = relationship(
        back_populates="workout", cascade="all, delete-orphan", order_by="WorkoutStep.position"
    )
    completed_activities: Mapped[list["Activity"]] = relationship(back_populates="workout")

    @property
    def definition_model(self):
        from app.services.planning.workout_definition import parse_definition

        return parse_definition(self.definition)

    @property
    def step_count(self) -> int:
        from app.services.planning.workout_definition import workout_metrics

        return workout_metrics(self.definition_model).step_count


class WorkoutStep(Base):
    __tablename__ = "workout_steps"
    __table_args__ = (UniqueConstraint("workout_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workout_id: Mapped[int] = mapped_column(
        ForeignKey("workouts.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    step_type: Mapped[str] = mapped_column(String(30))
    duration_type: Mapped[str] = mapped_column(String(30))
    duration_value: Mapped[float | None] = mapped_column(Float)
    target_type: Mapped[str] = mapped_column(String(30), default="no_target")
    target_min: Mapped[float | None] = mapped_column(Float)
    target_max: Mapped[float | None] = mapped_column(Float)
    repeat_count: Mapped[int | None] = mapped_column(Integer)

    workout: Mapped[Workout] = relationship(back_populates="steps")
