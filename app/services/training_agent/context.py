from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.repositories.activities import list_activities
from app.repositories.health import recent_health
from app.repositories.workouts import workouts_between
from app.services.analytics.training_load import calculate_weekly_load
from app.services.training_agent.backend import SnapshotRow, TrainingSnapshot


def build_training_snapshot(session: Session, user_id: int) -> TrainingSnapshot:
    today = date.today()
    load = calculate_weekly_load(session, user_id)
    activities: list[SnapshotRow] = [
        {
            "name": activity.name,
            "type": activity.activity_type,
            "started_at": activity.started_at.isoformat(timespec="minutes"),
            "distance_km": (
                round(activity.distance_m / 1000, 2) if activity.distance_m is not None else None
            ),
            "duration_minutes": (
                round(activity.duration_s / 60) if activity.duration_s is not None else None
            ),
            "average_hr": activity.average_hr,
            "max_hr": activity.max_hr,
        }
        for activity in list_activities(session, user_id, limit=10)
    ]
    health: list[SnapshotRow] = [
        {
            "day": item.day.isoformat(),
            "sleep_minutes": (
                round(item.sleep_seconds / 60) if item.sleep_seconds is not None else None
            ),
            "sleep_score": item.sleep_score,
            "resting_hr": item.resting_hr,
            "hrv_average": item.hrv_average,
            "stress_average": item.stress_average,
            "body_battery_high": item.body_battery_high,
        }
        for item in recent_health(session, user_id, days=7)
    ]
    workouts: list[SnapshotRow] = []
    for workout in workouts_between(session, user_id, today, today + timedelta(days=14)):
        duration_seconds = sum(
            (step.duration_value or 0) * (step.repeat_count or 1)
            for step in workout.steps
            if step.duration_type == "time"
        )
        distance_m = sum(
            (step.duration_value or 0) * (step.repeat_count or 1)
            for step in workout.steps
            if step.duration_type == "distance"
        )
        workouts.append(
            {
                "name": workout.name,
                "sport": workout.sport,
                "scheduled_for": (
                    workout.scheduled_for.isoformat() if workout.scheduled_for else None
                ),
                "status": workout.status,
                "duration_minutes": round(duration_seconds / 60) if duration_seconds else None,
                "distance_km": round(distance_m / 1000, 2) if distance_m else None,
            }
        )

    return TrainingSnapshot(
        as_of=today.isoformat(),
        weekly_load={
            "distance_km": load.distance_km,
            "duration_hours": load.duration_hours,
            "activity_count": load.activity_count,
        },
        recent_activities=tuple(activities),
        recent_health=tuple(health),
        upcoming_workouts=tuple(workouts),
    )
