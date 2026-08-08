from datetime import datetime

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, selectinload

from app.models import Activity, ActivityExerciseSet, ActivitySplit, ActivityZone


def activity_query(user_id: int) -> Select[tuple[Activity]]:
    return select(Activity).where(Activity.user_id == user_id).order_by(Activity.started_at.desc())


def list_activities(session: Session, user_id: int, limit: int = 100) -> list[Activity]:
    return list(session.scalars(activity_query(user_id).limit(limit)))


def find_activity(session: Session, user_id: int, activity_id: int) -> Activity | None:
    return session.scalar(
        select(Activity).where(Activity.user_id == user_id, Activity.id == activity_id)
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
