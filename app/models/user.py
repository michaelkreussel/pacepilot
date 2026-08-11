from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.activity import Activity
    from app.models.coach import CoachConversation
    from app.models.health import DailyHealth
    from app.models.workout import Workout


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100), default="Athlet")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    onboarding_notice_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    onboarding_completed_version: Mapped[int] = mapped_column(Integer, default=0)

    garmin_account: Mapped["GarminAccount | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    activities: Mapped[list["Activity"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    health_days: Mapped[list["DailyHealth"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    workouts: Mapped[list["Workout"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    coach_conversations: Mapped[list["CoachConversation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    oauth_identities: Mapped[list["OAuthIdentity"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class OAuthIdentity(Base):
    __tablename__ = "oauth_identities"
    __table_args__ = (UniqueConstraint("provider", "subject"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(30))
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    username: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="oauth_identities")


class GarminAccount(Base):
    __tablename__ = "garmin_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    email: Mapped[str | None] = mapped_column(String(320))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    rate_limit_until: Mapped[datetime | None] = mapped_column(DateTime)
    sync_status: Mapped[str] = mapped_column(String(30), default="not_connected")
    sync_error: Mapped[str | None] = mapped_column(String(1000))

    user: Mapped[User] = relationship(back_populates="garmin_account")
    devices: Mapped[list["GarminDevice"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class GarminDevice(Base):
    __tablename__ = "garmin_devices"
    __table_args__ = (UniqueConstraint("account_id", "garmin_device_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("garmin_accounts.id", ondelete="CASCADE"))
    garmin_device_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    model: Mapped[str | None] = mapped_column(String(200))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    account: Mapped[GarminAccount] = relationship(back_populates="devices")
