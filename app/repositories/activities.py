from datetime import datetime

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models import Activity


def activity_query(user_id: int) -> Select[tuple[Activity]]:
    return select(Activity).where(Activity.user_id == user_id).order_by(Activity.started_at.desc())


def list_activities(session: Session, user_id: int, limit: int = 100) -> list[Activity]:
    return list(session.scalars(activity_query(user_id).limit(limit)))


def find_activity(session: Session, user_id: int, activity_id: int) -> Activity | None:
    return session.scalar(
        select(Activity).where(Activity.user_id == user_id, Activity.id == activity_id)
    )


def activities_since(session: Session, user_id: int, since: datetime) -> list[Activity]:
    return list(session.scalars(activity_query(user_id).where(Activity.started_at >= since)))
