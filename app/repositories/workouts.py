from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Workout


def find_workout(session: Session, user_id: int, workout_id: int) -> Workout | None:
    return session.scalar(
        select(Workout)
        .options(selectinload(Workout.steps))
        .where(Workout.user_id == user_id, Workout.id == workout_id)
    )


def workouts_between(session: Session, user_id: int, start: date, end: date) -> list[Workout]:
    return list(
        session.scalars(
            select(Workout)
            .options(selectinload(Workout.steps))
            .where(
                Workout.user_id == user_id,
                Workout.scheduled_for >= start,
                Workout.scheduled_for <= end,
            )
            .order_by(Workout.scheduled_for, Workout.id)
        )
    )
