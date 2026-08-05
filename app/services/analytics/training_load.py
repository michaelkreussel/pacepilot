from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.repositories.activities import activities_since


@dataclass(frozen=True)
class WeeklyLoad:
    distance_km: float
    duration_hours: float
    activity_count: int


def calculate_weekly_load(session: Session, user_id: int) -> WeeklyLoad:
    since = (datetime.now(UTC) - timedelta(days=7)).replace(tzinfo=None)
    activities = activities_since(session, user_id, since)
    return WeeklyLoad(
        distance_km=round(sum(item.distance_m or 0 for item in activities) / 1000, 1),
        duration_hours=round(sum(item.duration_s or 0 for item in activities) / 3600, 1),
        activity_count=len(activities),
    )
