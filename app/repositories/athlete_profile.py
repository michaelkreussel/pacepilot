from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    AthleteAvailability,
    AthleteGoal,
    AthleteManualAnchor,
    AthleteProfile,
)
from app.services.planning.athlete_profile import AthleteProfileInput


def get_athlete_profile(session: Session, user_id: int) -> AthleteProfile | None:
    return session.get(AthleteProfile, user_id)


def get_athlete_goal(session: Session, user_id: int) -> AthleteGoal | None:
    return session.get(AthleteGoal, user_id)


def get_athlete_availability(session: Session, user_id: int) -> list[AthleteAvailability]:
    return list(
        session.scalars(
            select(AthleteAvailability)
            .where(AthleteAvailability.user_id == user_id)
            .order_by(AthleteAvailability.weekday)
        )
    )


def get_manual_anchors(session: Session, user_id: int) -> list[AthleteManualAnchor]:
    return list(
        session.scalars(
            select(AthleteManualAnchor)
            .where(AthleteManualAnchor.user_id == user_id)
            .order_by(AthleteManualAnchor.sport, AthleteManualAnchor.metric)
        )
    )


def save_athlete_profile(session: Session, user_id: int, data: AthleteProfileInput) -> None:
    profile = session.get(AthleteProfile, user_id)
    if profile is None:
        profile = AthleteProfile(user_id=user_id)
        session.add(profile)
    profile.primary_sport = data.primary_sport
    profile.experience_level = data.experience_level
    profile.experience_years = data.experience_years
    profile.constraint_note = data.constraint_note or None
    profile.constraint_until = data.constraint_until

    goal = session.get(AthleteGoal, user_id)
    if data.goal is None:
        if goal is not None:
            session.delete(goal)
    else:
        if goal is None:
            goal = AthleteGoal(
                user_id=user_id,
                sport=data.goal.sport,
                target_date=data.goal.target_date,
            )
            session.add(goal)
        goal.sport = data.goal.sport
        goal.event_name = data.goal.event_name or None
        goal.target_date = data.goal.target_date
        goal.distance_m = data.goal.distance_m
        goal.target_duration_s = data.goal.target_duration_s

    session.execute(delete(AthleteAvailability).where(AthleteAvailability.user_id == user_id))
    session.add_all(
        AthleteAvailability(
            user_id=user_id,
            weekday=item.weekday,
            max_duration_minutes=item.max_duration_minutes,
        )
        for item in data.availability
    )
    session.execute(delete(AthleteManualAnchor).where(AthleteManualAnchor.user_id == user_id))
    session.add_all(
        AthleteManualAnchor(
            user_id=user_id,
            sport=item.sport,
            metric=item.metric,
            value=item.value,
            observed_on=item.observed_on,
            method=item.method,
        )
        for item in data.anchors
    )
