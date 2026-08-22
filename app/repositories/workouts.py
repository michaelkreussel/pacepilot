from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Workout, WorkoutRevision
from app.services.planning.workout_views import CalendarWorkout


def find_workout(session: Session, user_id: int, workout_id: int) -> Workout | None:
    return session.scalar(
        select(Workout).where(
            Workout.user_id == user_id,
            Workout.id == workout_id,
            Workout.deleted_at.is_(None),
        )
    )


def workouts_between(
    session: Session, user_id: int, start: date, end: date
) -> list[CalendarWorkout]:
    rows = session.execute(
        select(Workout, WorkoutRevision)
        .join(WorkoutRevision, WorkoutRevision.id == Workout.accepted_revision_id)
        .where(
            Workout.user_id == user_id,
            Workout.deleted_at.is_(None),
            Workout.local_schedule_status == "scheduled",
            Workout.scheduled_for >= start,
            Workout.scheduled_for <= end,
        )
        .order_by(Workout.scheduled_for, Workout.id)
    )
    output: list[CalendarWorkout] = []
    for workout, revision in rows:
        if workout.scheduled_for is None:
            continue
        output.append(
            CalendarWorkout(
                id=workout.id,
                name=revision.name,
                sport=revision.sport,
                description=revision.description,
                scheduled_for=workout.scheduled_for,
                status="accepted",
                step_count=revision.step_count,
                revision_number=revision.revision_number,
                source_type=revision.source_type,
                has_unaccepted_changes=workout.current_revision_id != revision.id,
                definition=revision.definition_model,
            )
        )
    return output


def next_scheduled_workout(session: Session, user_id: int, start: date) -> CalendarWorkout | None:
    workouts = workouts_between(session, user_id, start, date.max)
    return workouts[0] if workouts else None
