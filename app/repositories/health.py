from collections.abc import Iterable
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import DailyDataStatus, DailyHealth, SleepStage
from app.models.user import utcnow


def recent_health(session: Session, user_id: int, days: int = 14) -> list[DailyHealth]:
    rows = list(
        session.scalars(
            select(DailyHealth)
            .where(DailyHealth.user_id == user_id)
            .order_by(DailyHealth.day.desc())
            .limit(days)
        )
    )
    return list(reversed(rows))


def find_health_day(session: Session, user_id: int, day: date) -> DailyHealth | None:
    return session.scalar(
        select(DailyHealth)
        .options(selectinload(DailyHealth.sleep_stages))
        .where(DailyHealth.user_id == user_id, DailyHealth.day == day)
    )


def get_or_create_health_day(session: Session, user_id: int, day: date) -> DailyHealth:
    health = session.scalar(
        select(DailyHealth).where(DailyHealth.user_id == user_id, DailyHealth.day == day)
    )
    if health is None:
        health = DailyHealth(user_id=user_id, day=day)
        session.add(health)
    return health


def health_between(session: Session, user_id: int, start: date, end: date) -> list[DailyHealth]:
    return list(
        session.scalars(
            select(DailyHealth)
            .options(selectinload(DailyHealth.sleep_stages))
            .where(
                DailyHealth.user_id == user_id,
                DailyHealth.day >= start,
                DailyHealth.day <= end,
            )
            .order_by(DailyHealth.day)
        )
    )


def health_metrics_between(
    session: Session, user_id: int, start: date, end: date
) -> list[DailyHealth]:
    return list(
        session.scalars(
            select(DailyHealth)
            .where(
                DailyHealth.user_id == user_id,
                DailyHealth.day >= start,
                DailyHealth.day <= end,
            )
            .order_by(DailyHealth.day)
        )
    )


def latest_health_on_or_before(session: Session, user_id: int, day: date) -> DailyHealth | None:
    return session.scalar(
        select(DailyHealth)
        .where(DailyHealth.user_id == user_id, DailyHealth.day <= day)
        .order_by(DailyHealth.day.desc())
        .limit(1)
    )


def replace_sleep_stages(
    session: Session, health: DailyHealth, stages: Iterable[SleepStage]
) -> None:
    health.sleep_stages.clear()
    session.flush()
    health.sleep_stages = list(stages)


def set_daily_data_status(
    session: Session,
    user_id: int,
    day: date,
    resource: str,
    status: str,
    error: str | None = None,
) -> DailyDataStatus:
    result = session.scalar(
        select(DailyDataStatus).where(
            DailyDataStatus.user_id == user_id,
            DailyDataStatus.day == day,
            DailyDataStatus.resource == resource,
        )
    )
    if result is None:
        result = DailyDataStatus(user_id=user_id, day=day, resource=resource, status=status)
        session.add(result)
    result.status = status
    result.error = error
    result.synced_at = utcnow()
    return result
