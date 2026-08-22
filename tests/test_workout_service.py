from datetime import date
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import GarminAccount, User, Workout, WorkoutRevision
from app.models.user import utcnow
from app.services.planning.validator import WorkoutInput, WorkoutValidationError
from app.services.planning.workout_definition import default_definition
from app.services.planning.workout_revision import (
    AcceptRevisionCommand,
    RevisionIdentity,
    ScheduleWorkoutCommand,
    default_context_fingerprint,
)
from app.services.planning.workout_service import (
    WorkoutNotFoundError,
    WorkoutService,
    WorkoutTransitionError,
)


def _input(name: str = "Easy Run") -> WorkoutInput:
    return WorkoutInput(
        name=name,
        sport="running",
        scheduled_for=date(2026, 8, 23),
        description="Locker",
        definition=default_definition(),
    )


def _accept_command(session: Session, workout: Workout) -> AcceptRevisionCommand:
    assert workout.current_revision_id is not None
    revision = session.get(WorkoutRevision, workout.current_revision_id)
    assert revision is not None
    return AcceptRevisionCommand(
        identity=RevisionIdentity(
            revision_id=revision.id,
            revision_number=revision.revision_number,
            content_hash=revision.content_hash,
            lock_version=workout.lock_version,
        ),
        context_fingerprint=default_context_fingerprint(revision.content_hash),
    )


def test_service_is_user_scoped(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        owner = User(display_name="Owner")
        other = User(display_name="Other")
        session.add_all([owner, other])
        session.flush()
        workout = Workout(
            user_id=other.id,
            name="Private workout",
            sport="running",
            status="pushed",
            garmin_workout_id="remote-private",
            definition=default_definition().model_dump(mode="json"),
        )
        session.add(workout)
        session.commit()

        def fail_connect(*_args: object) -> object:
            raise AssertionError("foreign workout contacted Garmin")

        service = WorkoutService(session, owner, connect_garmin=fail_connect)
        with pytest.raises(WorkoutNotFoundError) as exc_info:
            service.delete(workout.id)

        assert exc_info.value.code == "workout.not_found"
        assert session.get(Workout, workout.id) is workout


def test_service_validates_commands_before_persisting(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Athlete")
        session.add(user)
        session.commit()
        service = WorkoutService(session, user)

        with pytest.raises(WorkoutValidationError) as exc_info:
            service.create(_input(""))

        assert exc_info.value.code == "workout.name_required"
        assert str(exc_info.value) == "Bitte einen Namen angeben."
        assert session.scalar(select(Workout)) is None


def test_service_owns_manual_lifecycle_and_garmin_orchestration(
    session_factory: sessionmaker[Session],
) -> None:
    class FakeGarmin:
        uploads = 0
        schedules: list[tuple[str, str]] = []
        pushes: list[str] = []
        unscheduled: list[str] = []
        deleted: list[str] = []

        def upload_workout(self, _payload: dict[str, Any]) -> dict[str, str]:
            self.uploads += 1
            return {"workoutId": "remote-1"}

        def get_scheduled_workouts(self, _year: int, _month: int) -> dict[str, object]:
            return {
                "items": [
                    {"id": "schedule-1", "date": day, "workoutId": workout_id}
                    for workout_id, day in self.schedules
                ]
            }

        def schedule_workout(self, workout_id: str, day: str) -> None:
            self.schedules.append((workout_id, day))

        def push_workout_to_device(self, workout_id: str) -> None:
            self.pushes.append(workout_id)

        def unschedule_workout(self, scheduled_id: str) -> None:
            self.unscheduled.append(scheduled_id)

        def delete_workout(self, workout_id: str) -> None:
            self.deleted.append(workout_id)

    garmin = FakeGarmin()
    with session_factory() as session:
        user = User(display_name="Athlete")
        session.add(user)
        session.flush()
        session.add(GarminAccount(user_id=user.id, connected_at=utcnow()))
        session.commit()
        service = WorkoutService(session, user, connect_garmin=lambda *_args: garmin)

        workout = service.create(_input())
        service.update(workout.id, _input("Updated Easy Run"))
        service.confirm(workout.id, _accept_command(session, workout))
        assert workout.accepted_revision_id is not None
        service.schedule(
            workout.id,
            ScheduleWorkoutCommand(
                revision_id=workout.accepted_revision_id,
                scheduled_for=date(2026, 8, 23),
                expected_lock_version=workout.lock_version,
            ),
        )
        service.publish(workout.id)
        service.push(workout.id)

        assert workout.name == "Updated Easy Run"
        assert workout.status == "pushed"
        assert workout.garmin_workout_id == "remote-1"
        assert garmin.uploads == 1
        assert garmin.schedules == [("remote-1", "2026-08-23")]
        assert garmin.pushes == ["remote-1"]

        service.delete(workout.id)

        assert garmin.unscheduled == ["schedule-1"]
        assert garmin.deleted == ["remote-1"]
        deleted_workout = session.get(Workout, workout.id)
        assert deleted_workout is not None
        assert deleted_workout.deleted_at is not None


def test_service_transition_errors_have_stable_codes(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Athlete")
        session.add(user)
        session.commit()
        service = WorkoutService(session, user)
        workout = service.create(_input())

        with pytest.raises(WorkoutTransitionError) as exc_info:
            service.publish(workout.id)

        assert exc_info.value.code == "workout.confirmation_required"
        assert str(exc_info.value) == "Bitte den Entwurf vor der Übertragung bestätigen."
