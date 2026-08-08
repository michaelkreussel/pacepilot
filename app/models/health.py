from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.user import utcnow

if TYPE_CHECKING:
    from app.models.user import User


class DailyHealth(Base):
    __tablename__ = "daily_health"
    __table_args__ = (UniqueConstraint("user_id", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    sleep_seconds: Mapped[int | None] = mapped_column(Integer)
    sleep_score: Mapped[int | None] = mapped_column(Integer)
    sleep_score_duration: Mapped[int | None] = mapped_column(Integer)
    sleep_score_stress: Mapped[int | None] = mapped_column(Integer)
    sleep_score_awake_count: Mapped[int | None] = mapped_column(Integer)
    sleep_score_rem_percentage: Mapped[int | None] = mapped_column(Integer)
    sleep_score_restlessness: Mapped[int | None] = mapped_column(Integer)
    sleep_score_light_percentage: Mapped[int | None] = mapped_column(Integer)
    sleep_score_deep_percentage: Mapped[int | None] = mapped_column(Integer)
    sleep_start_at: Mapped[datetime | None] = mapped_column(DateTime)
    sleep_end_at: Mapped[datetime | None] = mapped_column(DateTime)
    deep_sleep_seconds: Mapped[int | None] = mapped_column(Integer)
    light_sleep_seconds: Mapped[int | None] = mapped_column(Integer)
    rem_sleep_seconds: Mapped[int | None] = mapped_column(Integer)
    awake_sleep_seconds: Mapped[int | None] = mapped_column(Integer)
    nap_seconds: Mapped[int | None] = mapped_column(Integer)
    sleep_need_seconds: Mapped[int | None] = mapped_column(Integer)
    sleep_need_baseline_seconds: Mapped[int | None] = mapped_column(Integer)
    next_sleep_need_seconds: Mapped[int | None] = mapped_column(Integer)
    sleep_average_hr: Mapped[float | None] = mapped_column(Float)
    sleep_average_stress: Mapped[float | None] = mapped_column(Float)
    sleep_body_battery_change: Mapped[int | None] = mapped_column(Integer)
    resting_hr: Mapped[int | None] = mapped_column(Integer)
    min_hr: Mapped[int | None] = mapped_column(Integer)
    max_hr: Mapped[int | None] = mapped_column(Integer)
    hrv_average: Mapped[float | None] = mapped_column(Float)
    hrv_weekly_average: Mapped[float | None] = mapped_column(Float)
    hrv_five_min_high: Mapped[float | None] = mapped_column(Float)
    hrv_status: Mapped[str | None] = mapped_column(String(30))
    hrv_baseline_low: Mapped[float | None] = mapped_column(Float)
    hrv_baseline_balanced_low: Mapped[float | None] = mapped_column(Float)
    hrv_baseline_balanced_high: Mapped[float | None] = mapped_column(Float)
    steps: Mapped[int | None] = mapped_column(Integer)
    distance_m: Mapped[float | None] = mapped_column(Float)
    total_calories: Mapped[int | None] = mapped_column(Integer)
    active_calories: Mapped[int | None] = mapped_column(Integer)
    stress_average: Mapped[int | None] = mapped_column(Integer)
    stress_max: Mapped[int | None] = mapped_column(Integer)
    body_battery_high: Mapped[int | None] = mapped_column(Integer)
    body_battery_low: Mapped[int | None] = mapped_column(Integer)
    body_battery_charged: Mapped[int | None] = mapped_column(Integer)
    body_battery_drained: Mapped[int | None] = mapped_column(Integer)
    waking_respiration_average: Mapped[float | None] = mapped_column(Float)
    sleep_respiration_average: Mapped[float | None] = mapped_column(Float)
    spo2_average: Mapped[float | None] = mapped_column(Float)
    sleep_spo2_average: Mapped[float | None] = mapped_column(Float)
    spo2_lowest: Mapped[float | None] = mapped_column(Float)
    moderate_intensity_minutes: Mapped[int | None] = mapped_column(Integer)
    vigorous_intensity_minutes: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship(back_populates="health_days")
    sleep_stages: Mapped[list["SleepStage"]] = relationship(
        back_populates="health_day",
        cascade="all, delete-orphan",
        order_by="SleepStage.position",
    )


class SleepStage(Base):
    __tablename__ = "sleep_stages"
    __table_args__ = (UniqueConstraint("daily_health_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    daily_health_id: Mapped[int] = mapped_column(
        ForeignKey("daily_health.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime)
    ended_at: Mapped[datetime] = mapped_column(DateTime)

    health_day: Mapped[DailyHealth] = relationship(back_populates="sleep_stages")
