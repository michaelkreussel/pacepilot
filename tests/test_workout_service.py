from datetime import date
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import (
    GarminAccount,
    User,
    Workout,
    WorkoutGarminAttempt,
    WorkoutGarminBinding,
    WorkoutGarminOperation,
    WorkoutRevision,
)
from app.models.user import utcnow
from app.services.garmin.client import GarminUnavailableError
from app.services.garmin.workout_operations import GarminWorkoutOperations
from app.services.planning.validator import WorkoutInput, WorkoutValidationError
from app.services.planning.workout_definition import default_definition
from app.services.planning.workout_revision import (
    AcceptedWorkoutExecution,
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


def test_garmin_operations_own_durable_upload_and_outcome_mapping(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "garmin_call_delay_seconds", 0)

    class FakeGarmin:
        def upload_workout(self, _payload: dict[str, Any]) -> dict[str, str]:
            with session_factory() as observer:
                operation = observer.scalar(select(WorkoutGarminOperation))
                attempt = observer.scalar(select(WorkoutGarminAttempt))
                assert operation is not None and operation.status == "pending"
                assert attempt is not None and attempt.status == "pending"
            return {"workoutId": "remote-boundary"}

    with session_factory() as session:
        user = User(display_name="Athlete")
        session.add(user)
        session.flush()
        account = GarminAccount(user_id=user.id, connected_at=utcnow())
        session.add(account)
        session.commit()
        service = WorkoutService(session, user)
        workout = service.create(_input())
        service.confirm(workout.id, _accept_command(session, workout))
        assert workout.accepted_revision_id is not None
        revision = session.get(WorkoutRevision, workout.accepted_revision_id)
        binding = session.scalar(
            select(WorkoutGarminBinding).where(WorkoutGarminBinding.workout_id == workout.id)
        )
        assert revision is not None and binding is not None
        execution = AcceptedWorkoutExecution(
            workout_id=workout.id,
            revision_id=revision.id,
            revision_number=revision.revision_number,
            name=revision.name,
            sport=revision.sport,
            description=revision.description,
            definition_version=revision.definition_version,
            definition=revision.definition,
            scheduled_for=None,
            garmin_workout_id=None,
        )

        identity = GarminWorkoutOperations(
            session,
            account,
            connect_garmin=lambda *_args: FakeGarmin(),
        ).upload(workout, binding, revision, execution)

        assert identity is not None
        assert identity.garmin_workout_id == "remote-boundary"
        assert binding.active_remote_identity_id == identity.id
        assert binding.content_status == "synced"
        assert workout.garmin_workout_id == "remote-boundary"
        operation = session.scalar(select(WorkoutGarminOperation))
        assert operation is not None and operation.status == "succeeded"


def test_service_owns_manual_lifecycle_and_garmin_orchestration(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "garmin_call_delay_seconds", 0)

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
        operations = list(
            session.scalars(select(WorkoutGarminOperation).order_by(WorkoutGarminOperation.id))
        )
        assert [operation.operation_type for operation in operations] == [
            "upload",
            "schedule",
            "push",
        ]
        assert all(operation.status == "succeeded" for operation in operations)
        assert session.query(WorkoutGarminAttempt).count() == 3

        service.delete(workout.id)

        assert garmin.unscheduled == ["schedule-1"]
        assert garmin.deleted == ["remote-1"]
        deleted_workout = session.get(Workout, workout.id)
        assert deleted_workout is not None
        assert deleted_workout.deleted_at is not None


def test_ambiguous_upload_is_recorded_and_not_retried(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "garmin_call_delay_seconds", 0)

    class AmbiguousGarmin:
        uploads = 0

        def upload_workout(self, _payload: dict[str, Any]) -> dict[str, str]:
            self.uploads += 1
            with session_factory() as observer:
                operation = observer.scalar(select(WorkoutGarminOperation))
                attempt = observer.scalar(select(WorkoutGarminAttempt))
                assert operation is not None and operation.status == "pending"
                assert attempt is not None and attempt.status == "pending"
            raise TimeoutError("response lost")

    garmin = AmbiguousGarmin()
    with session_factory() as session:
        user = User(display_name="Athlete")
        session.add(user)
        session.flush()
        session.add(GarminAccount(user_id=user.id, connected_at=utcnow()))
        session.commit()
        service = WorkoutService(session, user, connect_garmin=lambda *_args: garmin)
        workout = service.create(_input())
        service.confirm(workout.id, _accept_command(session, workout))

        with pytest.raises(GarminUnavailableError):
            service.publish(workout.id)
        service.update(workout.id, _input("Edited after timeout"))
        with pytest.raises(GarminUnavailableError, match="automatische Wiederholung"):
            service.publish(workout.id)

        operation = session.scalar(select(WorkoutGarminOperation))
        assert operation is not None and operation.status == "unknown"
        assert operation.revision_id == workout.accepted_revision_id
        assert garmin.uploads == 1


def test_ambiguous_push_is_not_retried_after_edit(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "garmin_call_delay_seconds", 0)

    class AmbiguousPushGarmin:
        pushes = 0

        def upload_workout(self, _payload: dict[str, Any]) -> dict[str, str]:
            return {"workoutId": "remote-1"}

        def push_workout_to_device(self, _workout_id: str) -> None:
            self.pushes += 1
            raise TimeoutError("response lost")

    garmin = AmbiguousPushGarmin()
    with session_factory() as session:
        user = User(display_name="Athlete")
        session.add(user)
        session.flush()
        session.add(GarminAccount(user_id=user.id, connected_at=utcnow()))
        session.commit()
        service = WorkoutService(session, user, connect_garmin=lambda *_args: garmin)
        workout = service.create(_input())
        service.confirm(workout.id, _accept_command(session, workout))
        service.publish(workout.id)

        with pytest.raises(GarminUnavailableError):
            service.push(workout.id)
        service.update(workout.id, _input("Edited after push timeout"))
        with pytest.raises(GarminUnavailableError, match="automatische Wiederholung"):
            service.push(workout.id)

        assert garmin.pushes == 1


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
