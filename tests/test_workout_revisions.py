from datetime import date
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    GarminAccount,
    User,
    Workout,
    WorkoutEvent,
    WorkoutGarminBinding,
    WorkoutRevision,
)
from app.models.user import utcnow
from app.services.planning.validator import WorkoutInput
from app.services.planning.workout_definition import default_definition
from app.services.planning.workout_revision import (
    AcceptRevisionCommand,
    RevisionIdentity,
    ScheduleWorkoutCommand,
    UnscheduleWorkoutCommand,
    default_context_fingerprint,
)
from app.services.planning.workout_service import (
    WorkoutConflictError,
    WorkoutService,
    WorkoutTransitionError,
)
from app.services.planning.workout_views import WorkoutDetailView, WorkoutRevisionView


def _input(name: str, suggested_for: date = date(2026, 8, 23)) -> WorkoutInput:
    return WorkoutInput(
        name=name,
        sport="running",
        scheduled_for=suggested_for,
        description="Locker",
        definition=default_definition(),
    )


def _accept_command(session: Session, workout_id: int) -> AcceptRevisionCommand:
    service_workout = session.get(Workout, workout_id)
    assert service_workout is not None
    assert service_workout.current_revision_id is not None
    revision = session.get(WorkoutRevision, service_workout.current_revision_id)
    assert revision is not None
    return AcceptRevisionCommand(
        identity=RevisionIdentity(
            revision_id=revision.id,
            revision_number=revision.revision_number,
            content_hash=revision.content_hash,
            lock_version=service_workout.lock_version,
        ),
        context_fingerprint=default_context_fingerprint(revision.content_hash),
    )


def _service(
    session: Session, *, connect_garmin=lambda *_args: None
) -> tuple[WorkoutService, User]:
    user = User(display_name="Athlete")
    session.add(user)
    session.flush()
    return WorkoutService(session, user, connect_garmin=connect_garmin), user


def test_accept_rejects_stale_revision(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        service, _user = _service(session)
        workout = service.create(_input("Revision 1"))
        stale_command = _accept_command(session, workout.id)
        service.update(workout.id, _input("Revision 2"))

        with pytest.raises(WorkoutConflictError) as exc_info:
            service.accept(workout.id, stale_command)

        session.refresh(workout)
        assert exc_info.value.code == "workout.revision_stale"
        assert workout.accepted_revision_id is None
        assert workout.approval_status == "draft"


def test_revision_diff_covers_all_acceptance_content() -> None:
    definition = default_definition().model_dump(mode="json")
    accepted = WorkoutRevisionView(
        id=1,
        revision_number=1,
        content_hash="a" * 64,
        name="Accepted",
        sport="running",
        suggested_for=date(2026, 8, 23),
        description="Accepted description",
        definition_version=1,
        definition=definition,
        purpose="Base",
        guidance={"cue": "Easy"},
        load_estimate={"score": 1},
        source_type="manual",
    )
    candidate = WorkoutRevisionView(
        id=2,
        revision_number=2,
        content_hash="b" * 64,
        name="Candidate",
        sport="cycling",
        suggested_for=date(2026, 8, 24),
        description="Candidate description",
        definition_version=2,
        definition={"blocks": []},
        purpose="Build",
        guidance={"cue": "Hard"},
        load_estimate={"score": 2},
        source_type="manual",
    )
    detail = WorkoutDetailView(
        id=1,
        current=candidate,
        accepted=accepted,
        approval_status="proposed",
        local_schedule_status="scheduled",
        scheduled_for=date(2026, 8, 23),
        lock_version=2,
        source_type="manual",
        garmin_content_status="not_requested",
        garmin_calendar_status="not_requested",
        garmin_device_status="not_requested",
        garmin_workout_id=None,
    )

    assert detail.change_labels == (
        "Name",
        "Sportart",
        "Datum",
        "Beschreibung",
        "Formatversion",
        "Ablauf",
        "Zweck",
        "Coaching-Hinweise",
        "Belastung",
    )


def test_edit_after_acceptance_keeps_previous_execution(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        service, _user = _service(session)
        workout = service.create(_input("Accepted name"))
        service.accept(workout.id, _accept_command(session, workout.id))
        assert workout.accepted_revision_id is not None
        accepted_revision_id = workout.accepted_revision_id
        service.schedule(
            workout.id,
            ScheduleWorkoutCommand(
                revision_id=accepted_revision_id,
                scheduled_for=date(2026, 8, 23),
                expected_lock_version=workout.lock_version,
            ),
        )

        service.update(workout.id, _input("Candidate name", date(2026, 8, 24)))

        assert workout.current_revision_id != accepted_revision_id
        assert workout.accepted_revision_id == accepted_revision_id
        assert workout.materialized_revision_id == accepted_revision_id
        assert workout.name == "Accepted name"
        assert workout.scheduled_for == date(2026, 8, 23)
        assert workout.approval_status == "proposed"


def test_garmin_uses_accepted_revision(session_factory: sessionmaker[Session]) -> None:
    class FakeGarmin:
        payloads: list[dict[str, Any]] = []

        def upload_workout(self, payload: dict[str, Any]) -> dict[str, str]:
            self.payloads.append(payload)
            return {"workoutId": "remote-1"}

    garmin = FakeGarmin()
    with session_factory() as session:
        service, user = _service(session, connect_garmin=lambda *_args: garmin)
        session.add(GarminAccount(user_id=user.id, connected_at=utcnow()))
        session.commit()
        workout = service.create(_input("Accepted name"))
        service.accept(workout.id, _accept_command(session, workout.id))
        service.update(workout.id, _input("Unaccepted candidate"))

        service.publish(workout.id)

        assert [payload["workoutName"] for payload in garmin.payloads] == ["Accepted name"]


def test_revision_is_immutable(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        service, _user = _service(session)
        workout = service.create(_input("Immutable"))
        assert workout.current_revision_id is not None
        revision = session.get(WorkoutRevision, workout.current_revision_id)
        assert revision is not None
        revision.name = "Mutation"

        with pytest.raises(ValueError, match="immutable"):
            session.commit()
        session.rollback()


def test_revision_is_immutable_for_direct_sql(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        service, _user = _service(session)
        workout = service.create(_input("Immutable"))
        assert workout.current_revision_id is not None

        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(
                update(WorkoutRevision)
                .where(WorkoutRevision.id == workout.current_revision_id)
                .values(name="Mutation")
            )
            session.commit()
        session.rollback()

        revision = session.get(WorkoutRevision, workout.current_revision_id)
        assert revision is not None
        assert revision.name == "Immutable"


def test_unknown_garmin_state_blocks_mutations(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        service, _user = _service(session)
        workout = service.create(_input("Unknown state"))
        service.accept(workout.id, _accept_command(session, workout.id))
        binding = session.scalar(
            select(WorkoutGarminBinding).where(WorkoutGarminBinding.workout_id == workout.id)
        )
        assert binding is not None
        binding.device_status = "unknown"
        session.commit()

        with pytest.raises(WorkoutTransitionError) as publish_error:
            service.publish(workout.id)
        assert publish_error.value.code == "garmin.state_unknown"

        with pytest.raises(WorkoutTransitionError) as delete_error:
            service.delete(workout.id)
        assert delete_error.value.code == "garmin.state_unknown"

        assert workout.accepted_revision_id is not None
        with pytest.raises(WorkoutTransitionError) as schedule_error:
            service.schedule(
                workout.id,
                ScheduleWorkoutCommand(
                    revision_id=workout.accepted_revision_id,
                    scheduled_for=date(2026, 8, 24),
                    expected_lock_version=workout.lock_version,
                ),
            )
        assert schedule_error.value.code == "garmin.state_unknown"

        service.update(workout.id, _input("Candidate"))
        with pytest.raises(WorkoutTransitionError) as accept_error:
            service.accept(workout.id, _accept_command(session, workout.id))
        assert accept_error.value.code == "garmin.state_unknown"


def test_accepting_new_revision_resets_remote_and_device_state(
    session_factory: sessionmaker[Session],
) -> None:
    class FakeGarmin:
        def upload_workout(self, _payload: dict[str, Any]) -> dict[str, str]:
            return {"workoutId": "remote-1"}

        def push_workout_to_device(self, _workout_id: str) -> None:
            return None

    with session_factory() as session:
        service, user = _service(session, connect_garmin=lambda *_args: FakeGarmin())
        session.add(GarminAccount(user_id=user.id, connected_at=utcnow()))
        session.commit()
        workout = service.create(_input("Accepted"))
        service.accept(workout.id, _accept_command(session, workout.id))
        service.publish(workout.id)
        service.push(workout.id)

        service.update(workout.id, _input("New candidate"))
        service.accept(workout.id, _accept_command(session, workout.id))

        binding = session.scalar(
            select(WorkoutGarminBinding).where(WorkoutGarminBinding.workout_id == workout.id)
        )
        assert binding is not None
        assert binding.content_status == "pending"
        assert binding.device_status == "not_requested"


def test_reschedule_and_unschedule_replace_remote_calendar_entry(
    session_factory: sessionmaker[Session],
) -> None:
    class FakeGarmin:
        scheduled: dict[str, tuple[str, str]] = {}
        unscheduled: list[str] = []

        def upload_workout(self, _payload: dict[str, Any]) -> dict[str, str]:
            return {"workoutId": "remote-1"}

        def get_scheduled_workouts(self, _year: int, _month: int) -> dict[str, object]:
            return {
                "items": [
                    {"id": key, "workoutId": workout_id, "date": day}
                    for key, (workout_id, day) in self.scheduled.items()
                ]
            }

        def schedule_workout(self, workout_id: str, day: str) -> None:
            key = f"schedule-{len(self.scheduled) + 1}"
            self.scheduled[key] = (workout_id, day)

        def unschedule_workout(self, scheduled_id: str) -> None:
            self.unscheduled.append(scheduled_id)
            self.scheduled.pop(scheduled_id)

    garmin = FakeGarmin()
    with session_factory() as session:
        service, user = _service(session, connect_garmin=lambda *_args: garmin)
        session.add(GarminAccount(user_id=user.id, connected_at=utcnow()))
        session.commit()
        workout = service.create(_input("Calendar"))
        service.accept(workout.id, _accept_command(session, workout.id))
        assert workout.accepted_revision_id is not None
        revision_id = workout.accepted_revision_id
        service.schedule(
            workout.id,
            ScheduleWorkoutCommand(
                revision_id=revision_id,
                scheduled_for=date(2026, 8, 23),
                expected_lock_version=workout.lock_version,
            ),
        )
        service.publish(workout.id)

        service.schedule(
            workout.id,
            ScheduleWorkoutCommand(
                revision_id=revision_id,
                scheduled_for=date(2026, 8, 24),
                expected_lock_version=workout.lock_version,
            ),
        )
        service.publish(workout.id)
        assert garmin.unscheduled == ["schedule-1"]
        assert garmin.scheduled == {"schedule-1": ("remote-1", "2026-08-24")}

        service.unschedule(
            workout.id,
            UnscheduleWorkoutCommand(
                revision_id=revision_id,
                expected_lock_version=workout.lock_version,
            ),
        )
        service.publish(workout.id)

        assert garmin.unscheduled == ["schedule-1", "schedule-1"]
        assert garmin.scheduled == {}
        assert workout.scheduled_for is None
        assert workout.local_schedule_status == "cancelled"
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkoutEvent)
                .where(WorkoutEvent.workout_id == workout.id, WorkoutEvent.action == "unschedule")
            )
            == 1
        )


def test_delete_uses_remote_date_while_reschedule_is_pending(
    session_factory: sessionmaker[Session],
) -> None:
    class FakeGarmin:
        unscheduled: list[str] = []
        deleted: list[str] = []

        def upload_workout(self, _payload: dict[str, Any]) -> dict[str, str]:
            return {"workoutId": "remote-1"}

        def get_scheduled_workouts(self, _year: int, _month: int) -> dict[str, object]:
            return {"items": [{"id": "old-entry", "workoutId": "remote-1", "date": "2026-08-23"}]}

        def schedule_workout(self, _workout_id: str, _day: str) -> None:
            return None

        def unschedule_workout(self, scheduled_id: str) -> None:
            self.unscheduled.append(scheduled_id)

        def delete_workout(self, workout_id: str) -> None:
            self.deleted.append(workout_id)

    garmin = FakeGarmin()
    with session_factory() as session:
        service, user = _service(session, connect_garmin=lambda *_args: garmin)
        session.add(GarminAccount(user_id=user.id, connected_at=utcnow()))
        session.commit()
        workout = service.create(_input("Delete pending reschedule"))
        service.accept(workout.id, _accept_command(session, workout.id))
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
        service.schedule(
            workout.id,
            ScheduleWorkoutCommand(
                revision_id=workout.accepted_revision_id,
                scheduled_for=date(2026, 8, 24),
                expected_lock_version=workout.lock_version,
            ),
        )

        service.delete(workout.id)

        assert garmin.unscheduled == ["old-entry"]
        assert garmin.deleted == ["remote-1"]


def test_edit_creates_next_revision(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        service, _user = _service(session)
        workout = service.create(_input("Revision 1"))
        first_revision_id = workout.current_revision_id

        service.update(workout.id, _input("Revision 2"))

        revisions = list(
            session.scalars(
                select(WorkoutRevision)
                .where(WorkoutRevision.workout_id == workout.id)
                .order_by(WorkoutRevision.revision_number)
            )
        )
        assert [revision.revision_number for revision in revisions] == [1, 2]
        assert revisions[1].parent_revision_id == first_revision_id
        assert revisions[0].name == "Revision 1"
        assert revisions[1].name == "Revision 2"


def test_changed_context_creates_validation_run(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        service, _user = _service(session)
        workout = service.create(_input("Context"))
        assert workout.current_revision_id is not None
        revision_id = workout.current_revision_id
        command = _accept_command(session, workout.id)

        service.validate_revision_context(
            workout.id,
            revision_id,
            command.context_fingerprint,
        )
        assert len(workout.revisions[0].validation_runs) == 1
        service.validate_revision_context(workout.id, revision_id, "a" * 64)
        session.expire_all()
        revision = session.get(WorkoutRevision, revision_id)
        assert revision is not None
        assert len(revision.validation_runs) == 2


def test_delete_tombstones_workout(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        service, _user = _service(session)
        workout = service.create(_input("Tombstone"))
        assert workout.current_revision_id is not None
        revision_id = workout.current_revision_id

        service.delete(workout.id)

        assert workout.deleted_at is not None
        assert session.get(WorkoutRevision, revision_id) is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkoutEvent)
                .where(
                    WorkoutEvent.workout_id == workout.id,
                    WorkoutEvent.action == "delete",
                )
            )
            == 1
        )
