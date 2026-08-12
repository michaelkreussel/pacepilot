from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.user import utcnow


class AthleteProfile(Base):
    __tablename__ = "athlete_profiles"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    primary_sport: Mapped[str | None] = mapped_column(String(30))
    experience_level: Mapped[str | None] = mapped_column(String(20))
    experience_years: Mapped[int | None] = mapped_column(Integer)
    constraint_note: Mapped[str | None] = mapped_column(Text)
    constraint_until: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AthleteGoal(Base):
    __tablename__ = "athlete_goals"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    sport: Mapped[str] = mapped_column(String(30))
    event_name: Mapped[str | None] = mapped_column(String(200))
    target_date: Mapped[date] = mapped_column(Date)
    distance_m: Mapped[float | None] = mapped_column(Float)
    target_duration_s: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AthleteAvailability(Base):
    __tablename__ = "athlete_availability"
    __table_args__ = (
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="ck_athlete_availability_weekday"),
        CheckConstraint(
            "max_duration_minutes >= 15 AND max_duration_minutes <= 1440",
            name="ck_athlete_availability_duration",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    weekday: Mapped[int] = mapped_column(Integer, primary_key=True)
    max_duration_minutes: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AthleteManualAnchor(Base):
    __tablename__ = "athlete_manual_anchors"
    __table_args__ = (CheckConstraint("value > 0", name="ck_athlete_manual_anchor_value"),)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    sport: Mapped[str] = mapped_column(String(30), primary_key=True)
    metric: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[float] = mapped_column(Float)
    observed_on: Mapped[date] = mapped_column(Date)
    method: Mapped[str] = mapped_column(String(20), default="manual")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
