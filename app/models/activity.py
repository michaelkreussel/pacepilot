from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.user import utcnow

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.workout import Workout


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (UniqueConstraint("user_id", "garmin_activity_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    garmin_activity_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(300))
    activity_type: Mapped[str] = mapped_column(String(100), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    distance_m: Mapped[float | None] = mapped_column(Float)
    duration_s: Mapped[float | None] = mapped_column(Float)
    elapsed_duration_s: Mapped[float | None] = mapped_column(Float)
    moving_duration_s: Mapped[float | None] = mapped_column(Float)
    average_speed_mps: Mapped[float | None] = mapped_column(Float)
    max_speed_mps: Mapped[float | None] = mapped_column(Float)
    min_hr: Mapped[int | None] = mapped_column(Integer)
    average_hr: Mapped[int | None] = mapped_column(Integer)
    max_hr: Mapped[int | None] = mapped_column(Integer)
    calories: Mapped[int | None] = mapped_column(Integer)
    elevation_gain_m: Mapped[float | None] = mapped_column(Float)
    elevation_loss_m: Mapped[float | None] = mapped_column(Float)
    average_cadence: Mapped[float | None] = mapped_column(Float)
    max_cadence: Mapped[float | None] = mapped_column(Float)
    average_power_watts: Mapped[float | None] = mapped_column(Float)
    max_power_watts: Mapped[float | None] = mapped_column(Float)
    normalized_power_watts: Mapped[float | None] = mapped_column(Float)
    aerobic_training_effect: Mapped[float | None] = mapped_column(Float)
    anaerobic_training_effect: Mapped[float | None] = mapped_column(Float)
    training_effect_label: Mapped[str | None] = mapped_column(String(100))
    exercise_load: Mapped[float | None] = mapped_column(Float)
    vo2max: Mapped[float | None] = mapped_column(Float)
    stride_length_cm: Mapped[float | None] = mapped_column(Float)
    ground_contact_time_ms: Mapped[float | None] = mapped_column(Float)
    vertical_oscillation_cm: Mapped[float | None] = mapped_column(Float)
    vertical_ratio: Mapped[float | None] = mapped_column(Float)
    workout_rpe: Mapped[int | None] = mapped_column(Integer)
    workout_feel: Mapped[int | None] = mapped_column(Integer)
    moderate_intensity_minutes: Mapped[int | None] = mapped_column(Integer)
    vigorous_intensity_minutes: Mapped[int | None] = mapped_column(Integer)
    body_battery_change: Mapped[int | None] = mapped_column(Integer)
    associated_garmin_workout_id: Mapped[str | None] = mapped_column(String(100), index=True)
    workout_id: Mapped[int | None] = mapped_column(
        ForeignKey("workouts.id", ondelete="SET NULL"), index=True
    )
    raw_file: Mapped[str | None] = mapped_column(String(500))
    details_file: Mapped[str | None] = mapped_column(String(500))
    fit_file: Mapped[str | None] = mapped_column(String(500))
    fit_import_status: Mapped[str | None] = mapped_column(String(20))
    fit_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    details_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    splits_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    details_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped["User"] = relationship(back_populates="activities")
    workout: Mapped["Workout | None"] = relationship(back_populates="completed_activities")
    zones: Mapped[list["ActivityZone"]] = relationship(
        back_populates="activity", cascade="all, delete-orphan"
    )
    splits: Mapped[list["ActivitySplit"]] = relationship(
        back_populates="activity", cascade="all, delete-orphan", order_by="ActivitySplit.position"
    )
    exercise_sets: Mapped[list["ActivityExerciseSet"]] = relationship(
        back_populates="activity",
        cascade="all, delete-orphan",
        order_by="ActivityExerciseSet.position",
    )


class ActivityZone(Base):
    __tablename__ = "activity_zones"
    __table_args__ = (UniqueConstraint("activity_id", "zone_type", "zone_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"), index=True
    )
    zone_type: Mapped[str] = mapped_column(String(20))
    zone_number: Mapped[int] = mapped_column(Integer)
    low_boundary: Mapped[float | None] = mapped_column(Float)
    seconds: Mapped[float | None] = mapped_column(Float)

    activity: Mapped[Activity] = relationship(back_populates="zones")


class ActivitySplit(Base):
    __tablename__ = "activity_splits"
    __table_args__ = (UniqueConstraint("activity_id", "split_type", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"), index=True
    )
    split_type: Mapped[str] = mapped_column(String(30))
    position: Mapped[int] = mapped_column(Integer)
    intensity_type: Mapped[str | None] = mapped_column(String(30))
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_s: Mapped[float | None] = mapped_column(Float)
    elapsed_duration_s: Mapped[float | None] = mapped_column(Float)
    moving_duration_s: Mapped[float | None] = mapped_column(Float)
    distance_m: Mapped[float | None] = mapped_column(Float)
    elevation_gain_m: Mapped[float | None] = mapped_column(Float)
    elevation_loss_m: Mapped[float | None] = mapped_column(Float)
    average_hr: Mapped[int | None] = mapped_column(Integer)
    max_hr: Mapped[int | None] = mapped_column(Integer)
    average_speed_mps: Mapped[float | None] = mapped_column(Float)
    max_speed_mps: Mapped[float | None] = mapped_column(Float)
    average_cadence: Mapped[float | None] = mapped_column(Float)
    max_cadence: Mapped[float | None] = mapped_column(Float)
    average_power_watts: Mapped[float | None] = mapped_column(Float)
    max_power_watts: Mapped[float | None] = mapped_column(Float)
    normalized_power_watts: Mapped[float | None] = mapped_column(Float)
    stride_length_cm: Mapped[float | None] = mapped_column(Float)
    ground_contact_time_ms: Mapped[float | None] = mapped_column(Float)
    vertical_oscillation_cm: Mapped[float | None] = mapped_column(Float)
    vertical_ratio: Mapped[float | None] = mapped_column(Float)

    activity: Mapped[Activity] = relationship(back_populates="splits")


class ActivityExerciseSet(Base):
    __tablename__ = "activity_exercise_sets"
    __table_args__ = (UniqueConstraint("activity_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    set_type: Mapped[str | None] = mapped_column(String(30))
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_s: Mapped[float | None] = mapped_column(Float)
    repetitions: Mapped[int | None] = mapped_column(Integer)
    weight_kg: Mapped[float | None] = mapped_column(Float)
    exercise_category: Mapped[str | None] = mapped_column(String(100))
    exercise_name: Mapped[str | None] = mapped_column(String(150))
    workout_step_index: Mapped[int | None] = mapped_column(Integer)

    activity: Mapped[Activity] = relationship(back_populates="exercise_sets")
