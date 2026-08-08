from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.user import utcnow

if TYPE_CHECKING:
    from app.models.user import User


class DailyFitness(Base):
    __tablename__ = "daily_fitness"
    __table_args__ = (UniqueConstraint("user_id", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    vo2max: Mapped[float | None] = mapped_column(Float)
    training_status: Mapped[str | None] = mapped_column(String(50))
    training_load: Mapped[float | None] = mapped_column(Float)
    acute_load: Mapped[float | None] = mapped_column(Float)
    chronic_load: Mapped[float | None] = mapped_column(Float)
    load_ratio: Mapped[float | None] = mapped_column(Float)
    garmin_training_readiness_score: Mapped[int | None] = mapped_column(Integer)
    garmin_training_readiness_level: Mapped[str | None] = mapped_column(String(50))
    recovery_time_minutes: Mapped[int | None] = mapped_column(Integer)
    endurance_score: Mapped[float | None] = mapped_column(Float)
    hill_score: Mapped[float | None] = mapped_column(Float)
    fitness_age: Mapped[float | None] = mapped_column(Float)
    lactate_threshold_hr: Mapped[int | None] = mapped_column(Integer)
    lactate_threshold_speed_mps: Mapped[float | None] = mapped_column(Float)
    running_ftp_watts: Mapped[float | None] = mapped_column(Float)
    cycling_ftp_watts: Mapped[float | None] = mapped_column(Float)
    race_prediction_5k_seconds: Mapped[int | None] = mapped_column(Integer)
    race_prediction_10k_seconds: Mapped[int | None] = mapped_column(Integer)
    race_prediction_half_seconds: Mapped[int | None] = mapped_column(Integer)
    race_prediction_marathon_seconds: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship()
