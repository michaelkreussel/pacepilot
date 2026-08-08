from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.user import utcnow


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(30), default="running")
    stage: Mapped[str | None] = mapped_column(String(50))
    message: Mapped[str | None] = mapped_column(String(500))
    current_item: Mapped[int] = mapped_column(Integer, default=0)
    total_items: Mapped[int] = mapped_column(Integer, default=0)
    activities_synced: Mapped[int] = mapped_column(Integer, default=0)
    health_days_synced: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(1000))


class GarminSyncState(Base):
    __tablename__ = "garmin_sync_states"
    __table_args__ = (UniqueConstraint("user_id", "resource"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    resource: Mapped[str] = mapped_column(String(50))
    oldest_synced_date: Mapped[date | None] = mapped_column(Date)
    newest_synced_date: Mapped[date | None] = mapped_column(Date)
    backfill_cursor_date: Mapped[date | None] = mapped_column(Date)
    cursor: Mapped[str | None] = mapped_column(String(200))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    backfill_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(String(1000))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DailyDataStatus(Base):
    __tablename__ = "daily_data_statuses"
    __table_args__ = (UniqueConstraint("user_id", "day", "resource"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    resource: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30))
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    error: Mapped[str | None] = mapped_column(String(1000))
