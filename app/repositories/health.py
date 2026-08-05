from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DailyHealth


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
