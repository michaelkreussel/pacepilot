from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DailyFitness


def find_daily_fitness(session: Session, user_id: int, day: date) -> DailyFitness | None:
    return session.scalar(
        select(DailyFitness).where(DailyFitness.user_id == user_id, DailyFitness.day == day)
    )


def get_or_create_daily_fitness(session: Session, user_id: int, day: date) -> DailyFitness:
    fitness = find_daily_fitness(session, user_id, day)
    if fitness is None:
        fitness = DailyFitness(user_id=user_id, day=day)
        session.add(fitness)
    return fitness


def fitness_between(session: Session, user_id: int, start: date, end: date) -> list[DailyFitness]:
    return list(
        session.scalars(
            select(DailyFitness)
            .where(
                DailyFitness.user_id == user_id,
                DailyFitness.day >= start,
                DailyFitness.day <= end,
            )
            .order_by(DailyFitness.day)
        )
    )
