from datetime import date, datetime, time, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Activity, ActivityExerciseSet, ActivitySplit, ActivityZone


def activity_query(user_id: int) -> Select[tuple[Activity]]:
    return (
        select(Activity)
        .where(Activity.user_id == user_id)
        .order_by(Activity.started_at.desc(), Activity.id.desc())
    )


def list_activities(session: Session, user_id: int, limit: int = 100) -> list[Activity]:
    return list(session.scalars(activity_query(user_id).limit(limit)))


def list_activities_on_or_before(
    session: Session, user_id: int, through: datetime, limit: int = 100
) -> list[Activity]:
    return list(
        session.scalars(activity_query(user_id).where(Activity.started_at <= through).limit(limit))
    )


def list_activities_filtered(
    session: Session,
    user_id: int,
    *,
    start: date | None = None,
    end: date | None = None,
    sport: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Activity]:
    query = _filtered_activity_query(user_id, start=start, end=end, sport=sport)
    return list(session.scalars(query.offset(offset).limit(limit)))


def count_activities_filtered(
    session: Session,
    user_id: int,
    *,
    start: date | None = None,
    end: date | None = None,
    sport: str | None = None,
) -> int:
    query = _filtered_activity_query(user_id, start=start, end=end, sport=sport)
    return session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0


def list_activity_types(session: Session, user_id: int) -> list[str]:
    return list(
        session.scalars(
            select(Activity.activity_type)
            .where(Activity.user_id == user_id)
            .distinct()
            .order_by(Activity.activity_type)
        )
    )


def _filtered_activity_query(
    user_id: int,
    *,
    start: date | None = None,
    end: date | None = None,
    sport: str | None = None,
) -> Select[tuple[Activity]]:
    query = activity_query(user_id)
    if start is not None:
        query = query.where(Activity.started_at >= datetime.combine(start, time.min))
    if end is not None:
        query = query.where(
            Activity.started_at < datetime.combine(end + timedelta(days=1), time.min)
        )
    if sport is not None:
        query = query.where(Activity.activity_type == sport)
    return query


def find_activity(session: Session, user_id: int, activity_id: int) -> Activity | None:
    return session.scalar(
        select(Activity)
        .options(selectinload(Activity.zones))
        .where(Activity.user_id == user_id, Activity.id == activity_id)
    )


def find_activity_by_garmin_id(
    session: Session, user_id: int, garmin_activity_id: str
) -> Activity | None:
    return session.scalar(
        select(Activity).where(
            Activity.user_id == user_id,
            Activity.garmin_activity_id == garmin_activity_id,
        )
    )


def find_activity_with_history(session: Session, user_id: int, activity_id: int) -> Activity | None:
    return session.scalar(
        select(Activity)
        .options(
            selectinload(Activity.zones),
            selectinload(Activity.splits),
            selectinload(Activity.exercise_sets),
        )
        .where(Activity.user_id == user_id, Activity.id == activity_id)
    )


def get_or_create_activity(
    session: Session,
    user_id: int,
    garmin_activity_id: str,
    *,
    name: str,
    activity_type: str,
    started_at: datetime,
) -> Activity:
    activity = find_activity_by_garmin_id(session, user_id, garmin_activity_id)
    if activity is None:
        activity = Activity(
            user_id=user_id,
            garmin_activity_id=garmin_activity_id,
            name=name,
            activity_type=activity_type,
            started_at=started_at,
        )
        session.add(activity)
    return activity


def replace_activity_zones(session: Session, activity: Activity, zones: list[ActivityZone]) -> None:
    activity.zones.clear()
    session.flush()
    activity.zones = zones


def replace_activity_splits(
    session: Session, activity: Activity, splits: list[ActivitySplit]
) -> None:
    activity.splits.clear()
    session.flush()
    activity.splits = splits


def replace_activity_exercise_sets(
    session: Session, activity: Activity, exercise_sets: list[ActivityExerciseSet]
) -> None:
    activity.exercise_sets.clear()
    session.flush()
    activity.exercise_sets = exercise_sets


def activities_since(session: Session, user_id: int, since: datetime) -> list[Activity]:
    return list(session.scalars(activity_query(user_id).where(Activity.started_at >= since)))


def activities_between(
    session: Session,
    user_id: int,
    start: datetime,
    end: datetime,
    *,
    include_zones: bool = False,
) -> list[Activity]:
    query = select(Activity).where(
        Activity.user_id == user_id,
        Activity.started_at >= start,
        Activity.started_at < end,
    )
    if include_zones:
        query = query.options(selectinload(Activity.zones))
    return list(session.scalars(query.order_by(Activity.started_at, Activity.id)))
