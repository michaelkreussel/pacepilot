from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Activity,
    DailyFitness,
    PostSessionFeedback,
    PreSessionFeedback,
    User,
    Workout,
    WorkoutRevision,
    WorkoutValidationRun,
)
from app.models.user import utcnow
from app.services.analytics.subjective_feedback import effective_activity_feedback
from app.services.planning.feedback_service import FeedbackNotFoundError, FeedbackService
from app.services.planning.safety_triage import (
    IllnessSignal,
    PainInput,
    PostSessionFeedbackInput,
    PreSessionFeedbackInput,
    TriageOutcome,
    build_safety_context,
    triage_feedback,
)
from app.services.planning.validator import WorkoutInput
from app.services.planning.workout_definition import default_definition
from app.services.planning.workout_revision import (
    AcceptRevisionCommand,
    RevisionIdentity,
    ScheduleWorkoutCommand,
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


def _accept_command(service: WorkoutService, workout: Workout) -> AcceptRevisionCommand:
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


def test_new_safety_feedback_invalidates_acceptance_and_delete_does_not_reuse_cache(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        service = WorkoutService(session, user)
        workout = service.create(_workout_input())
        stale_command = _accept_command(service, workout)

        feedback = FeedbackService(session, user).record_pre_session(
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
        with pytest.raises(WorkoutConflictError) as stale:
            service.accept(workout.id, stale_command)
        assert stale.value.code == "workout.validation_context_stale"

        with pytest.raises(WorkoutTransitionError) as stopped:
            service.accept(workout.id, _accept_command(service, workout))
        assert stopped.value.code == "workout.validation_failed"
        with session_factory() as evidence_session:
            validation = evidence_session.scalar(
                select(WorkoutValidationRun)
                .where(WorkoutValidationRun.workout_id == workout.id)
                .order_by(WorkoutValidationRun.id.desc())
            )
            assert validation is not None
            assert validation.feedback_ids_json == [f"pre:{feedback.id}"]
            assert validation.report_json["outcome"] == "safety_stop"

        stopped_fingerprint = service.acceptance_context(workout.id).fingerprint
        FeedbackService(session, user).delete_pre_session(feedback.id)
        assert not any(
            f"pre:{feedback.id}" in run.feedback_ids_json
            for run in session.scalars(
                select(WorkoutValidationRun).where(WorkoutValidationRun.workout_id == workout.id)
            )
        )
        allowed_context = service.acceptance_context(workout.id)
        assert allowed_context.fingerprint != stopped_fingerprint
        assert allowed_context.report.outcome == TriageOutcome.ALLOW
        service.accept(workout.id, _accept_command(service, workout))
        assert workout.approval_status == "accepted"


def test_same_day_safety_stop_blocks_delayed_garmin_sync(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        service = WorkoutService(session, user)
        workout = service.create(_workout_input(date.today()))
        service.accept(workout.id, _accept_command(service, workout))
        service.schedule(
            workout.id,
            ScheduleWorkoutCommand(
                revision_id=workout.accepted_revision_id or 0,
                expected_lock_version=workout.lock_version,
                scheduled_for=date.today(),
            ),
        )
        FeedbackService(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(
                motivation=5,
                fatigue=1,
                leg_freshness=5,
                soreness=0,
                illness_signal=IllnessSignal.FEVER,
            ),
        )

        with pytest.raises(WorkoutTransitionError) as stopped:
            service.publish(workout.id)
        assert stopped.value.code == "workout.validation_failed"
        assert "Sicherheitshinweis" in str(stopped.value)


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
        FeedbackService(session, user).record_pre_session(
            signal_workout.id,
            PreSessionFeedbackInput(
                motivation=3,
                fatigue=3,
                leg_freshness=3,
                soreness=0,
                illness_signal=IllnessSignal.FEVER,
            ),
        )
        assert future.current_revision_id is not None
        revision = session.get(WorkoutRevision, future.current_revision_id)
        assert revision is not None

        acceptance = build_safety_context(session, user.id, future, revision, mode="acceptance")
        sync = build_safety_context(session, user.id, future, revision, mode="sync")
        assert acceptance.report.outcome == TriageOutcome.SAFETY_STOP
        assert sync.report.outcome == TriageOutcome.ALLOW


def test_feedback_expires_after_versioned_freshness_window(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Runner")
        session.add(user)
        session.flush()
        service = WorkoutService(session, user)
        workout = service.create(_workout_input(date.today() + timedelta(days=30)))
        feedback = FeedbackService(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(
                motivation=3,
                fatigue=3,
                leg_freshness=3,
                soreness=0,
                illness_signal=IllnessSignal.FEVER,
            ),
        )
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
        FeedbackService(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(
                motivation=5,
                fatigue=1,
                leg_freshness=5,
                soreness=0,
                illness_signal=IllnessSignal.CARDIOPULMONARY_WARNING,
            ),
        )

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
        FeedbackService(session, user).record_post_session(
            activity.id, PostSessionFeedbackInput(session_rpe=9, overall_feel=2)
        )

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
        service = FeedbackService(session, user)
        feedback = service.record_post_session(
            activity.id,
            PostSessionFeedbackInput(
                completion_percent=80,
                session_rpe=8,
                overall_feel=3,
                stopped_reason="Zeitbudget",
                notes="Bewusst verkürzt",
            ),
        )

        session.delete(activity)
        session.commit()
        session.refresh(feedback)
        assert feedback.activity_id is None

        payload = service.export_data()
        exported = payload["post_session_feedback"]
        assert isinstance(exported, list)
        assert exported[0]["activity_id"] is None
        assert exported[0]["notes"] == "Bewusst verkürzt"

        service.delete_post_session(feedback.id)
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
            FeedbackService(session, owner).delete_pre_session(feedback.id)


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
        service = FeedbackService(session, user)
        service.record_post_session(
            garmin.id, PostSessionFeedbackInput(session_rpe=3, overall_feel=2)
        )
        service.record_post_session(
            manual.id, PostSessionFeedbackInput(session_rpe=7, overall_feel=3)
        )

        feedback = effective_activity_feedback(session, user.id, [garmin, manual])

        assert (feedback[garmin.id].effort, feedback[garmin.id].effort_source) == (8, "garmin")
        assert (feedback[garmin.id].feel, feedback[garmin.id].feel_source) == (4, "garmin")
        assert (feedback[manual.id].effort, feedback[manual.id].effort_source) == (7, "manual")
        assert (feedback[manual.id].feel, feedback[manual.id].feel_source) == (3, "manual")


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
    assert "Entwurf bestätigen" not in stopped.text

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
