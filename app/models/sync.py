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
    activities_processed: Mapped[int] = mapped_column(Integer, default=0)
    activities_total: Mapped[int] = mapped_column(Integer, default=0)
    days_completed: Mapped[int] = mapped_column(Integer, default=0)
    days_total: Mapped[int] = mapped_column(Integer, default=0)
    operations_completed: Mapped[int] = mapped_column(Integer, default=0)
    operations_total: Mapped[int] = mapped_column(Integer, default=0)
    current_day: Mapped[date | None] = mapped_column(Date)
    current_operation: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(String(1000))


class SyncEvent(Base):
    __tablename__ = "sync_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    sync_run_id: Mapped[int] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(10), default="info")
    category: Mapped[str] = mapped_column(String(30), default="sync")
    status: Mapped[str] = mapped_column(String(20), default="info")
    resource: Mapped[str | None] = mapped_column(String(50))
    day: Mapped[date | None] = mapped_column(Date)
    operation: Mapped[str | None] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(String(500))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    record_count: Mapped[int | None] = mapped_column(Integer)


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
