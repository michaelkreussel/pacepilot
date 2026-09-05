from dataclasses import asdict
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import (
    Activity,
    DailyFitness,
    GarminAccount,
    PostSessionFeedback,
    PreSessionFeedback,
    User,
    Workout,
    WorkoutEvent,
    WorkoutGarminOperation,
    WorkoutRevision,
)
from app.models.user import utcnow
from app.routes import workouts as workouts_module
from app.services.analytics.athlete_data import AthleteDataService
from app.services.analytics.subjective_feedback import effective_activity_feedback
from app.services.garmin import workout_operations as workout_operations_module
from app.services.garmin.client import GarminUnavailableError
from app.services.planning.feedback_service import (
    FeedbackCommands,
    FeedbackNotFoundError,
    FeedbackQueries,
)
from app.services.planning.safety_triage import (
    IllnessSignal,
    PainInput,
    PostSessionFeedbackInput,
    PreSessionFeedbackInput,
    TriageOutcome,
    build_safety_context,
    triage_feedback,
)
from app.services.planning.validator import WorkoutInput, WorkoutValidationError
from app.services.planning.workout_definition import default_definition
from app.services.planning.workout_revision import (
    AcceptRevisionCommand,
    RevisionIdentity,
    ScheduleWorkoutCommand,
    UnscheduleWorkoutCommand,
)
from app.services.planning.workout_service import (
    WorkoutConflictError,
    WorkoutService,
    WorkoutTransitionError,
)


def _workout_input(day: date | None = None) -> WorkoutInput:
    return WorkoutInput(
        name="Lockerer Lauf",
        sport="running",
        scheduled_for=day or date.today(),
        description="Ruhig",
        definition=default_definition(),
    )


def _accept_command(
    service: WorkoutService,
    workout: Workout,
    *,
    acknowledge_elevated_warning: bool = False,
) -> AcceptRevisionCommand:
    service.session.refresh(workout)
    assert workout.current_revision_id is not None
    revision = service.session.get(WorkoutRevision, workout.current_revision_id)
    assert revision is not None
    return AcceptRevisionCommand(
        identity=RevisionIdentity(
            revision_id=revision.id,
            revision_number=revision.revision_number,
            content_hash=revision.content_hash,
            lock_version=workout.lock_version,
        ),
        context_fingerprint=service.acceptance_context(workout.id).fingerprint,
        acknowledge_elevated_warning=acknowledge_elevated_warning,
    )


def _pre_row(**values: object) -> PreSessionFeedback:
    defaults: dict[str, object] = {
        "id": 1,
        "user_id": 1,
        "workout_id": 1,
        "motivation": 3,
        "fatigue": 2,
        "leg_freshness": 4,
        "soreness": 1,
        "sleep_quality": 4,
        "pain_present": False,
        "illness_signal": "none",
        "source": "explicit_form",
        "content_hash": "a" * 64,
    }
    defaults.update(values)
    return PreSessionFeedback(**defaults)


def _post_row(**values: object) -> PostSessionFeedback:
    defaults: dict[str, object] = {
        "id": 1,
        "user_id": 1,
        "completion_percent": 100,
        "session_rpe": 4.0,
        "overall_feel": 4,
        "pain_present": False,
        "source": "explicit_form",
        "content_hash": "b" * 64,
    }
    defaults.update(values)
    return PostSessionFeedback(**defaults)


def test_triage_covers_allow_warn_clarify_and_safety_stop() -> None:
    assert triage_feedback([_pre_row()], []).outcome == TriageOutcome.ALLOW
    assert triage_feedback([_pre_row(fatigue=5)], []).outcome == TriageOutcome.WARN
    clarify = triage_feedback(
        [
            _pre_row(
                pain_present=True,
                pain_location="Knie",
                pain_severity=None,
                pain_alters_gait=None,
            )
        ],
        [],
    )
    assert clarify.outcome == TriageOutcome.CLARIFY
    assert clarify.issues[0].code == "safety.pain_unclear"

    stop = triage_feedback(
        [
            _pre_row(
                motivation=5,
                fatigue=1,
                leg_freshness=5,
                pain_present=True,
                pain_location="Knie",
                pain_severity=3,
                pain_alters_gait=True,
            )
        ],
        [],
    )
    assert stop.outcome == TriageOutcome.SAFETY_STOP
    assert not stop.valid


@pytest.mark.parametrize(
    ("signal", "code"),
    [
        ("fever", "safety.fever_or_systemic_illness"),
        ("systemic", "safety.fever_or_systemic_illness"),
        ("cardiopulmonary_warning", "safety.cardiopulmonary_warning"),
    ],
)
def test_red_flags_stop_without_diagnostic_copy(signal: str, code: str) -> None:
    report = triage_feedback([_pre_row(illness_signal=signal)], [])

    assert report.outcome == TriageOutcome.SAFETY_STOP
    assert code in {issue.code for issue in report.issues}
    copy = " ".join(issue.message.lower() for issue in report.issues)
    words = set(copy.replace(".", "").replace(",", "").split())
    assert (
        not {
            "diagnose",
            "diagnosis",
            "grippe",
            "herzinfarkt",
            "myokarditis",
            "lungenentzündung",
        }
        & words
    )


def test_post_session_pain_and_difficult_session_are_deterministic() -> None:
    report = triage_feedback(
        [],
        [
            _post_row(
                completion_percent=50,
                session_rpe=9,
                overall_feel=1,
                pain_present=True,
                pain_location="Achillessehne",
                pain_severity=5,
                pain_alters_gait=False,
                pain_worsens_with_activity=True,
            )
        ],
    )

    assert report.outcome == TriageOutcome.WARN
    assert {issue.code for issue in report.issues} == {
        "recovery.difficult_session",
        "safety.pain_warning",
    }


def test_feedback_record_commands_are_transaction_neutral(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        workout = WorkoutService(session, user).create(_workout_input())
        activity = Activity(
            user_id=user.id,
            garmin_activity_id="transaction-neutral",
            name="Run",
            activity_type="running",
            started_at=utcnow(),
        )
        session.add(activity)
        session.commit()
        user_id = user.id
        workout_id = workout.id
        activity_id = activity.id

        commands = FeedbackCommands(session, user)
        commands.record_pre_session(
            workout_id,
            PreSessionFeedbackInput(fatigue=3),
        )
        commands.record_post_session(
            activity_id,
            PostSessionFeedbackInput(session_rpe=5),
        )

        queries = FeedbackQueries(session, user)
        assert len(queries.pre_session_for_workout(workout_id)) == 1
        assert len(queries.post_session_for_activity(activity_id)) == 1
        session.rollback()

    with session_factory() as session:
        user = session.get(User, user_id)
        assert user is not None
        queries = FeedbackQueries(session, user)
        assert queries.pre_session_for_workout(workout_id) == []
        assert queries.post_session_for_activity(activity_id) == []


def test_feedback_delete_commands_return_ids_without_committing(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        workout = WorkoutService(session, user).create(_workout_input())
        activity = Activity(
            user_id=user.id,
            garmin_activity_id="rollback-delete",
            name="Run",
            activity_type="running",
            started_at=utcnow(),
        )
        session.add(activity)
        session.commit()
        commands = FeedbackCommands(session, user)
        pre = commands.record_pre_session(workout.id, PreSessionFeedbackInput(fatigue=3))
        post = commands.record_post_session(
            activity.id,
            PostSessionFeedbackInput(session_rpe=5),
        )
        session.commit()

        assert commands.delete_pre_session(pre.id) == workout.id
        assert commands.delete_post_session(post.id) == activity.id
        queries = FeedbackQueries(session, user)
        assert queries.all_pre_session() == []
        assert queries.all_post_session() == []
        session.rollback()

        assert [item.id for item in queries.all_pre_session()] == [pre.id]
        assert [item.id for item in queries.all_post_session()] == [post.id]


def test_elevated_same_day_accept_requires_fresh_acknowledgement(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        service = WorkoutService(session, user)
        workout = service.create(_workout_input())
        stale_command = _accept_command(service, workout)

        FeedbackCommands(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(
                motivation=5,
                fatigue=1,
                leg_freshness=5,
                soreness=0,
                pain=PainInput(
                    present=True,
                    location="Knie",
                    severity=3,
                    alters_gait=True,
                ),
            ),
        )
        session.commit()
        with pytest.raises(WorkoutConflictError) as stale:
            service.accept(workout.id, stale_command)
        assert stale.value.code == "workout.validation_context_stale"

        with pytest.raises(WorkoutTransitionError) as stopped:
            service.accept(workout.id, _accept_command(service, workout))
        assert stopped.value.code == "workout.training_fit_acknowledgement_required"
        session.refresh(workout)
        assert workout.accepted_revision_id is None
        assert (
            session.scalar(
                select(WorkoutEvent).where(
                    WorkoutEvent.workout_id == workout.id,
                    WorkoutEvent.action == "accept",
                )
            )
            is None
        )

        service.accept(
            workout.id,
            _accept_command(service, workout, acknowledge_elevated_warning=True),
        )
        event = session.scalar(
            select(WorkoutEvent).where(
                WorkoutEvent.workout_id == workout.id,
                WorkoutEvent.action == "accept",
            )
        )
        assert event is not None
        authorization = event.safe_metadata_json["training_fit_authorization"]
        assert isinstance(authorization, dict)
        assert authorization == {
            "policy_version": authorization["policy_version"],
            "assessment_fingerprint": authorization["assessment_fingerprint"],
            "effective_date": date.today().isoformat(),
            "acknowledged_by_user_id": user.id,
            "acknowledged_at": authorization["acknowledged_at"],
            "authorized_revision_id": workout.accepted_revision_id,
            "local_date": date.today().isoformat(),
        }
        assert len(authorization["assessment_fingerprint"]) == 64
        assert workout.approval_status == "accepted"


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("acknowledged_by_user_id", -1),
        ("authorized_revision_id", -1),
        ("effective_date", "2026-01-01"),
        ("policy_version", "training-fit-stale"),
        ("assessment_fingerprint", "0" * 64),
        ("local_date", "2026-01-01"),
        ("acknowledged_at", "not-a-date"),
    ],
)
def test_mismatched_acknowledgement_cannot_authorize_schedule(
    session_factory: sessionmaker[Session], field: str, wrong_value: object
) -> None:
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        service = WorkoutService(session, user)
        workout = service.create(_workout_input())
        FeedbackCommands(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(illness_signal=IllnessSignal.FEVER),
        )
        session.commit()
        service.accept(
            workout.id,
            _accept_command(service, workout, acknowledge_elevated_warning=True),
        )
        event = session.scalar(
            select(WorkoutEvent).where(
                WorkoutEvent.workout_id == workout.id,
                WorkoutEvent.action == "accept",
            )
        )
        assert event is not None
        metadata = dict(event.safe_metadata_json)
        raw_authorization = metadata["training_fit_authorization"]
        assert isinstance(raw_authorization, dict)
        authorization = dict(raw_authorization)
        authorization[field] = wrong_value
        event.safe_metadata_json = {**metadata, "training_fit_authorization": authorization}
        session.commit()

        with pytest.raises(WorkoutTransitionError) as required:
            service.schedule(
                workout.id,
                ScheduleWorkoutCommand(
                    revision_id=workout.accepted_revision_id or 0,
                    expected_lock_version=workout.lock_version,
                    scheduled_for=date.today(),
                ),
            )

        assert required.value.code == "workout.training_fit_acknowledgement_required"
        session.refresh(workout)
        assert workout.scheduled_for is None


def test_matching_acknowledgement_is_reused_until_feedback_changes(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        service = WorkoutService(session, user)
        workout = service.create(_workout_input())
        FeedbackCommands(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(illness_signal=IllnessSignal.FEVER),
        )
        session.commit()
        service.accept(
            workout.id,
            _accept_command(service, workout, acknowledge_elevated_warning=True),
        )

        service.schedule(
            workout.id,
            ScheduleWorkoutCommand(
                revision_id=workout.accepted_revision_id or 0,
                expected_lock_version=workout.lock_version,
                scheduled_for=date.today(),
            ),
        )
        schedule_event = session.scalar(
            select(WorkoutEvent).where(
                WorkoutEvent.workout_id == workout.id,
                WorkoutEvent.action == "schedule",
            )
        )
        assert schedule_event is not None
        assert "training_fit_authorization_event_id" in schedule_event.safe_metadata_json

        service.unschedule(
            workout.id,
            UnscheduleWorkoutCommand(
                revision_id=workout.accepted_revision_id or 0,
                expected_lock_version=workout.lock_version,
            ),
        )
        FeedbackCommands(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(fatigue=5),
        )
        session.commit()

        with pytest.raises(WorkoutTransitionError) as required:
            service.schedule(
                workout.id,
                ScheduleWorkoutCommand(
                    revision_id=workout.accepted_revision_id or 0,
                    expected_lock_version=workout.lock_version,
                    scheduled_for=date.today(),
                ),
            )
        assert required.value.code == "workout.training_fit_acknowledgement_required"

        service.schedule(
            workout.id,
            ScheduleWorkoutCommand(
                revision_id=workout.accepted_revision_id or 0,
                expected_lock_version=workout.lock_version,
                scheduled_for=date.today(),
                acknowledge_elevated_warning=True,
            ),
        )
        assert workout.scheduled_for == date.today()


def test_replacing_a_revision_uses_the_existing_same_day_schedule_for_authorization(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        service = WorkoutService(session, user)
        workout = service.create(_workout_input())
        service.accept(workout.id, _accept_command(service, workout))
        accepted_revision_id = workout.accepted_revision_id
        assert accepted_revision_id is not None
        service.schedule(
            workout.id,
            ScheduleWorkoutCommand(
                revision_id=accepted_revision_id,
                expected_lock_version=workout.lock_version,
                scheduled_for=date.today(),
            ),
        )
        service.update(workout.id, _workout_input(date.today() + timedelta(days=1)))
        FeedbackCommands(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(illness_signal=IllnessSignal.FEVER),
        )
        session.commit()

        with pytest.raises(WorkoutTransitionError) as required:
            service.accept(workout.id, _accept_command(service, workout))

        assert required.value.code == "workout.training_fit_acknowledgement_required"
        session.refresh(workout)
        assert workout.accepted_revision_id == accepted_revision_id

        service.accept(
            workout.id,
            _accept_command(service, workout, acknowledge_elevated_warning=True),
        )
        assert workout.accepted_revision_id == workout.current_revision_id
        assert workout.scheduled_for == date.today()


def test_newly_elevated_delayed_publish_requires_acknowledgement(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "garmin_call_delay_seconds", 0)

    class FakeGarmin:
        uploads = 0
        pushes = 0

        def upload_workout(self, _payload: dict[str, object]) -> dict[str, str]:
            self.uploads += 1
            return {"workoutId": "remote-1"}

        def push_workout_to_device(self, _workout_id: str) -> None:
            self.pushes += 1

    garmin = FakeGarmin()
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        session.add(GarminAccount(user_id=user.id, connected_at=utcnow()))
        session.commit()
        service = WorkoutService(session, user, connect_garmin=lambda *_args: garmin)
        workout = service.create(_workout_input(date.today()))
        service.accept(workout.id, _accept_command(service, workout))
        FeedbackCommands(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(
                motivation=5,
                fatigue=1,
                leg_freshness=5,
                soreness=0,
                illness_signal=IllnessSignal.FEVER,
            ),
        )
        session.commit()

        with pytest.raises(WorkoutTransitionError) as required:
            service.publish(workout.id)
        assert required.value.code == "workout.training_fit_acknowledgement_required"
        assert garmin.uploads == 0
        assert session.scalar(select(WorkoutGarminOperation)) is None

        service.publish(workout.id, acknowledge_elevated_warning=True)

        operation = session.scalar(select(WorkoutGarminOperation))
        revision = session.get(WorkoutRevision, workout.accepted_revision_id)
        assert operation is not None and operation.status == "succeeded"
        assert revision is not None and revision.validation_report_json is not None
        assert revision.validation_report_json["valid"] is True
        assert operation.training_fit_policy_version
        assert len(operation.training_fit_assessment_fingerprint or "") == 64
        assert operation.training_fit_effective_date == date.today()
        assert operation.training_fit_acknowledged_by_user_id == user.id
        assert operation.training_fit_acknowledged_at is not None
        assert operation.training_fit_authorized_revision_id == revision.id
        assert workout.status == "published"
        assert garmin.uploads == 1

        service.push(workout.id)

        push_operation = session.scalar(
            select(WorkoutGarminOperation).where(WorkoutGarminOperation.operation_type == "push")
        )
        assert push_operation is not None and push_operation.status == "succeeded"
        assert (
            push_operation.training_fit_assessment_fingerprint
            == operation.training_fit_assessment_fingerprint
        )
        assert push_operation.training_fit_acknowledged_at == operation.training_fit_acknowledged_at
        assert workout.status == "pushed"
        assert garmin.pushes == 1


def test_ambiguous_acknowledged_push_retains_authorization_without_claiming_success(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "garmin_call_delay_seconds", 0)

    class AmbiguousPushGarmin:
        pushes = 0

        def upload_workout(self, _payload: dict[str, object]) -> dict[str, str]:
            return {"workoutId": "remote-1"}

        def push_workout_to_device(self, _workout_id: str) -> None:
            self.pushes += 1
            raise TimeoutError("response lost")

    garmin = AmbiguousPushGarmin()
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        session.add(GarminAccount(user_id=user.id, connected_at=utcnow()))
        session.commit()
        service = WorkoutService(session, user, connect_garmin=lambda *_args: garmin)
        workout = service.create(_workout_input(date.today()))
        service.accept(workout.id, _accept_command(service, workout))
        service.publish(workout.id)
        FeedbackCommands(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(illness_signal=IllnessSignal.FEVER),
        )
        session.commit()

        with pytest.raises(WorkoutTransitionError) as required:
            service.push(workout.id)
        assert required.value.code == "workout.training_fit_acknowledgement_required"
        assert garmin.pushes == 0

        with pytest.raises(GarminUnavailableError):
            service.push(workout.id, acknowledge_elevated_warning=True)

        operation = session.scalar(
            select(WorkoutGarminOperation).where(WorkoutGarminOperation.operation_type == "push")
        )
        assert operation is not None and operation.status == "unknown"
        assert operation.training_fit_effective_date == date.today()
        assert operation.training_fit_acknowledged_by_user_id == user.id
        assert operation.training_fit_acknowledged_at is not None
        assert operation.training_fit_authorized_revision_id == workout.accepted_revision_id
        assert len(operation.attempts) == 1
        assert operation.attempts[0].status == "unknown"
        assert (
            session.scalar(
                select(WorkoutEvent).where(
                    WorkoutEvent.workout_id == workout.id,
                    WorkoutEvent.action == "push",
                )
            )
            is None
        )
        session.refresh(workout)
        assert workout.status == "published"
        assert garmin.pushes == 1


def test_ambiguous_acknowledged_publish_retains_authorization_without_claiming_success(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "garmin_call_delay_seconds", 0)

    class AmbiguousGarmin:
        uploads = 0

        def upload_workout(self, _payload: dict[str, object]) -> dict[str, str]:
            self.uploads += 1
            raise TimeoutError("response lost")

    garmin = AmbiguousGarmin()
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        session.add(GarminAccount(user_id=user.id, connected_at=utcnow()))
        session.commit()
        service = WorkoutService(session, user, connect_garmin=lambda *_args: garmin)
        workout = service.create(_workout_input(date.today()))
        service.accept(workout.id, _accept_command(service, workout))
        FeedbackCommands(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(illness_signal=IllnessSignal.FEVER),
        )
        session.commit()

        with pytest.raises(GarminUnavailableError):
            service.publish(workout.id, acknowledge_elevated_warning=True)

        operation = session.scalar(
            select(WorkoutGarminOperation).where(WorkoutGarminOperation.operation_type == "upload")
        )
        assert operation is not None and operation.status == "unknown"
        assert operation.training_fit_effective_date == date.today()
        assert operation.training_fit_acknowledged_by_user_id == user.id
        assert operation.training_fit_acknowledged_at is not None
        assert operation.training_fit_authorized_revision_id == workout.accepted_revision_id
        assert len(operation.attempts) == 1
        assert operation.attempts[0].status == "unknown"
        assert (
            session.scalar(
                select(WorkoutEvent).where(
                    WorkoutEvent.workout_id == workout.id,
                    WorkoutEvent.action == "publish",
                )
            )
            is None
        )
        session.refresh(workout)
        assert workout.status == "confirmed"
        assert garmin.uploads == 1


def test_failed_acknowledged_publish_retains_authorization_without_claiming_success(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "garmin_call_delay_seconds", 0)

    def fail_upload(*_args: object) -> None:
        raise WorkoutValidationError("Garmin-Inhalt ungültig.", code="garmin.validation_failed")

    monkeypatch.setattr(workout_operations_module, "upload_workout", fail_upload)

    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        session.add(GarminAccount(user_id=user.id, connected_at=utcnow()))
        session.commit()
        service = WorkoutService(session, user, connect_garmin=lambda *_args: object())
        workout = service.create(_workout_input(date.today()))
        service.accept(workout.id, _accept_command(service, workout))
        FeedbackCommands(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(illness_signal=IllnessSignal.FEVER),
        )
        session.commit()

        with pytest.raises(WorkoutValidationError):
            service.publish(workout.id, acknowledge_elevated_warning=True)

        operation = session.scalar(select(WorkoutGarminOperation))
        assert operation is not None and operation.status == "failed_final"
        assert operation.training_fit_acknowledged_by_user_id == user.id
        assert operation.training_fit_authorized_revision_id == workout.accepted_revision_id
        assert len(operation.attempts) == 1
        assert operation.attempts[0].status == "failed"
        assert (
            session.scalar(
                select(WorkoutEvent).where(
                    WorkoutEvent.workout_id == workout.id,
                    WorkoutEvent.action == "publish",
                )
            )
            is None
        )
        session.refresh(workout)
        assert workout.status == "confirmed"


def test_delayed_publish_form_submits_elevated_acknowledgement(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.get("/")
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        service = WorkoutService(session, user)
        workout = service.create(_workout_input(date.today()))
        service.accept(workout.id, _accept_command(service, workout))
        account = session.scalar(select(GarminAccount).where(GarminAccount.user_id == user.id))
        assert account is not None
        account.connected_at = utcnow()
        FeedbackCommands(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(illness_signal=IllnessSignal.FEVER),
        )
        session.commit()
        workout_id = workout.id

    class FakeGarmin:
        uploads = 0
        pushes = 0

        def upload_workout(self, _payload: dict[str, object]) -> dict[str, str]:
            self.uploads += 1
            return {"workoutId": "remote-1"}

        def push_workout_to_device(self, _workout_id: str) -> None:
            self.pushes += 1

    garmin = FakeGarmin()
    monkeypatch.setattr(
        workouts_module,
        "connect_garmin_account",
        lambda *_args: garmin,
    )

    detail = client.get(f"/workouts/{workout_id}")
    assert f'action="/workouts/{workout_id}/publish"' in detail.text
    assert 'name="acknowledge_elevated_warning"' in detail.text
    assert "An Garmin übertragen" in detail.text

    blocked = client.post(f"/workouts/{workout_id}/publish", follow_redirects=False)
    assert blocked.status_code == 303
    assert "error=" in blocked.headers["location"]
    assert garmin.uploads == 0

    allowed = client.post(
        f"/workouts/{workout_id}/publish",
        data={"acknowledge_elevated_warning": "yes"},
        follow_redirects=False,
    )
    assert allowed.status_code == 303
    assert "error=" not in allowed.headers["location"]
    assert garmin.uploads == 1

    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        FeedbackCommands(session, user).record_pre_session(
            workout_id,
            PreSessionFeedbackInput(
                fatigue=5,
                illness_signal=IllnessSignal.FEVER,
            ),
        )
        session.commit()

    detail = client.get(f"/workouts/{workout_id}")
    push_form = detail.text.split(f'action="/workouts/{workout_id}/push"', maxsplit=1)[1].split(
        "</form>", maxsplit=1
    )[0]
    assert 'name="acknowledge_elevated_warning"' in push_form
    blocked = client.post(f"/workouts/{workout_id}/push", follow_redirects=False)
    assert "error=" in blocked.headers["location"]
    assert garmin.pushes == 0

    allowed = client.post(
        f"/workouts/{workout_id}/push",
        data={"acknowledge_elevated_warning": "yes"},
        follow_redirects=False,
    )
    assert allowed.status_code == 303
    assert "error=" not in allowed.headers["location"]
    assert garmin.pushes == 1


def test_future_sync_does_not_require_unrelated_daily_feedback(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        service = WorkoutService(session, user)
        future = service.create(_workout_input(date.today() + timedelta(days=2)))
        signal_workout = service.create(_workout_input(date.today()))
        FeedbackCommands(session, user).record_pre_session(
            signal_workout.id,
            PreSessionFeedbackInput(
                motivation=3,
                fatigue=3,
                leg_freshness=3,
                soreness=0,
                illness_signal=IllnessSignal.FEVER,
            ),
        )
        session.commit()
        assert future.current_revision_id is not None
        revision = session.get(WorkoutRevision, future.current_revision_id)
        assert revision is not None

        acceptance = build_safety_context(session, user.id, future, revision, mode="acceptance")
        sync = build_safety_context(session, user.id, future, revision, mode="sync")
        assert acceptance.report.outcome == TriageOutcome.SAFETY_STOP
        assert sync.report.outcome == TriageOutcome.ALLOW
        service.accept(future.id, _accept_command(service, future))
        service.schedule(
            future.id,
            ScheduleWorkoutCommand(
                revision_id=future.accepted_revision_id or 0,
                expected_lock_version=future.lock_version,
                scheduled_for=date.today() + timedelta(days=2),
            ),
        )
        assert future.scheduled_for == date.today() + timedelta(days=2)


def test_feedback_expires_after_versioned_freshness_window(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        service = WorkoutService(session, user)
        workout = service.create(_workout_input(date.today() + timedelta(days=30)))
        feedback = FeedbackCommands(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(
                motivation=3,
                fatigue=3,
                leg_freshness=3,
                soreness=0,
                illness_signal=IllnessSignal.FEVER,
            ),
        )
        session.commit()
        feedback.recorded_at = utcnow() - timedelta(days=8)
        session.commit()

        context = service.acceptance_context(workout.id)
        assert context.report.outcome == TriageOutcome.ALLOW
        assert context.feedback_ids == ()


def test_positive_wearable_readiness_cannot_override_safety_stop(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        session.add(
            DailyFitness(
                user_id=user.id,
                day=date.today(),
                garmin_training_readiness_score=100,
                garmin_training_readiness_level="prime",
            )
        )
        service = WorkoutService(session, user)
        workout = service.create(_workout_input())
        FeedbackCommands(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(
                motivation=5,
                fatigue=1,
                leg_freshness=5,
                soreness=0,
                illness_signal=IllnessSignal.CARDIOPULMONARY_WARNING,
            ),
        )
        session.commit()

        assert service.acceptance_context(workout.id).report.outcome == TriageOutcome.SAFETY_STOP


def test_effective_activity_feedback_drives_safety_context(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        workout_service = WorkoutService(session, user)
        workout = workout_service.create(_workout_input())
        activity = Activity(
            user_id=user.id,
            garmin_activity_id="effective-feedback",
            name="Run",
            activity_type="running",
            started_at=utcnow(),
        )
        session.add(activity)
        session.commit()
        FeedbackCommands(session, user).record_post_session(
            activity.id, PostSessionFeedbackInput(session_rpe=9, overall_feel=2)
        )
        session.commit()

        manual_context = workout_service.acceptance_context(workout.id)
        assert manual_context.report.outcome == TriageOutcome.WARN

        activity.workout_rpe = 4
        activity.workout_feel = 4
        session.commit()
        garmin_override = workout_service.acceptance_context(workout.id)
        assert garmin_override.report.outcome == TriageOutcome.ALLOW
        assert garmin_override.fingerprint != manual_context.fingerprint

        activity.workout_rpe = 8
        session.commit()
        garmin_warning = workout_service.acceptance_context(workout.id)
        assert garmin_warning.report.outcome == TriageOutcome.WARN
        assert garmin_warning.fingerprint != garmin_override.fingerprint


def test_post_feedback_export_survives_garmin_activity_deletion_and_can_be_deleted(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        activity = Activity(
            user_id=user.id,
            garmin_activity_id="run-1",
            name="Run",
            activity_type="running",
            started_at=utcnow(),
        )
        session.add(activity)
        session.commit()
        commands = FeedbackCommands(session, user)
        feedback = commands.record_post_session(
            activity.id,
            PostSessionFeedbackInput(
                completion_percent=80,
                session_rpe=8,
                overall_feel=3,
                stopped_reason="Zeitbudget",
                notes="Bewusst verkürzt",
            ),
        )
        session.commit()

        session.delete(activity)
        session.commit()
        session.refresh(feedback)
        assert feedback.activity_id is None

        payload = FeedbackQueries(session, user).export_data()
        exported = payload["post_session_feedback"]
        assert isinstance(exported, list)
        assert exported[0]["activity_id"] is None
        assert exported[0]["notes"] == "Bewusst verkürzt"

        commands.delete_post_session(feedback.id)
        session.commit()
        assert session.get(PostSessionFeedback, feedback.id) is None


def test_feedback_service_is_user_scoped(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        owner = User(display_name="Owner")
        other = User(display_name="Other")
        session.add_all([owner, other])
        session.flush()
        feedback = _pre_row(id=None, user_id=other.id, workout_id=None)
        session.add(feedback)
        session.commit()

        with pytest.raises(FeedbackNotFoundError):
            FeedbackCommands(session, owner).delete_pre_session(feedback.id)


def test_effective_activity_feedback_prefers_garmin_and_falls_back_per_field(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        garmin = Activity(
            user_id=user.id,
            garmin_activity_id="garmin",
            name="Garmin Run",
            activity_type="running",
            started_at=utcnow(),
            workout_rpe=8,
            workout_feel=4,
        )
        manual = Activity(
            user_id=user.id,
            garmin_activity_id="manual",
            name="Manual Run",
            activity_type="running",
            started_at=utcnow(),
        )
        session.add_all([garmin, manual])
        session.commit()
        commands = FeedbackCommands(session, user)
        commands.record_post_session(
            garmin.id, PostSessionFeedbackInput(session_rpe=3, overall_feel=2)
        )
        session.commit()
        commands.record_post_session(
            manual.id, PostSessionFeedbackInput(session_rpe=7, overall_feel=3)
        )
        session.commit()

        feedback = effective_activity_feedback(session, user.id, [garmin, manual])

        assert (feedback[garmin.id].effort, feedback[garmin.id].effort_source) == (8, "garmin")
        assert (feedback[garmin.id].feel, feedback[garmin.id].feel_source) == (4, "garmin")
        assert (feedback[manual.id].effort, feedback[manual.id].effort_source) == (7, "manual")
        assert (feedback[manual.id].feel, feedback[manual.id].feel_source) == (3, "manual")

        context = AthleteDataService(session, user.id).get_subjective_context()
        context_feedback = {item.activity_id: item for item in context.recent_activity_feedback}
        assert (context_feedback[garmin.id].effort, context_feedback[garmin.id].effort_source) == (
            8,
            "garmin",
        )
        post_details = [asdict(item) for item in context.recent_post_session_feedback]
        assert all(
            "session_rpe" not in item and "overall_feel" not in item for item in post_details
        )


def test_german_feedback_forms_routes_and_export(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    created = client.post(
        "/workouts",
        data={
            "name": "Testlauf",
            "sport": "running",
            "scheduled_for": date.today().isoformat(),
            "description": "Locker",
            "definition": default_definition().model_dump_json(),
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    location = created.headers["location"]
    detail = client.get(location)
    assert ">Sicherheit<" in detail.text
    assert "Sicherheitsangaben öffnen" in detail.text
    assert "Was soll heute berücksichtigt" not in detail.text
    assert "Motivation (1–5)" not in detail.text

    recorded = client.post(
        f"{location}/feedback/pre-session",
        data={
            "illness_signal": "none",
            "pain_present": "yes",
            "pain_location": "Knie",
            "pain_severity": "3",
            "pain_alters_gait": "yes",
            "pain_worsens_with_activity": "no",
        },
        follow_redirects=False,
    )
    assert recorded.status_code == 303
    stopped = client.get(location)
    assert "Sicherheitsstopp" in stopped.text
    assert 'name="acknowledge_elevated_warning"' in stopped.text
    assert "Trotz erhöhtem Gesundheitsrisiko mit exakt Revision" in stopped.text
    assert "Entwurf bestätigen" in stopped.text

    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        activity = Activity(
            user_id=user.id,
            garmin_activity_id="route-run",
            name="Abendlauf",
            activity_type="running",
            started_at=utcnow(),
        )
        session.add(activity)
        session.commit()
        activity_id = activity.id
    activity_page = client.get(f"/activities/{activity_id}")
    assert "Wie lief dein Training?" in activity_page.text
    assert "Wie anstrengend war es?" in activity_page.text
    assert "Abgeschlossen (%)" not in activity_page.text
    assert 'pt-4" open>' in activity_page.text
    post = client.post(
        f"/activities/{activity_id}/feedback/post-session",
        data={
            "session_rpe": "6",
            "overall_feel": "3",
        },
        follow_redirects=False,
    )
    assert post.status_code == 303
    activity_page = client.get(f"/activities/{activity_id}")
    assert "😐 Okay" in activity_page.text

    with session_factory() as session:
        activity = session.get(Activity, activity_id)
        assert activity is not None
        activity.workout_rpe = 5
        activity.workout_feel = 4
        session.commit()
    activity_page = client.get(f"/activities/{activity_id}")
    assert "🙂 Gut" in activity_page.text
    assert activity_page.text.count(">Garmin<") >= 2

    with session_factory() as session:
        activity = session.get(Activity, activity_id)
        assert activity is not None
        activity.workout_rpe = None
        session.commit()
    activity_page = client.get(f"/activities/{activity_id}")
    assert "6.0" in activity_page.text
    assert "🙂 Gut" in activity_page.text
    assert ">Manuell<" in activity_page.text
    assert ">Garmin<" in activity_page.text

    dashboard = client.get("/")
    assert "Wie geht es dir heute?" not in dashboard.text
    assert client.post("/feedback/daily").status_code == 404

    exported = client.get("/settings/feedback/export")
    assert exported.status_code == 200
    assert "pacepilot-subjektives-feedback.json" in exported.headers["content-disposition"]
    assert exported.headers["cache-control"] == "private, no-store, max-age=0"
    assert len(exported.json()["pre_session_feedback"]) == 1
    assert len(exported.json()["post_session_feedback"]) == 1
