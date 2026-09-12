from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    Activity,
    AthleteAvailability,
    AthleteGoal,
    AthletePlanningProfile,
    PreSessionFeedback,
    User,
    Workout,
    WorkoutRevision,
)
from app.services.garmin.workout_export import compile_workout_with_report
from app.services.planning.daily_adaptation import DailyAdaptationClass, DailyAdaptationService
from app.services.planning.daily_recommendation import recommend_today, save_recommendation
from app.services.planning.workout_proposals import RunningProposalService
from app.services.planning.workout_revision import (
    AcceptRevisionCommand,
    RejectRevisionCommand,
    RevisionIdentity,
)
from app.services.planning.workout_service import WorkoutService, WorkoutTransitionError
from app.services.planning.workout_templates import TemplateParameters

DAY = date(2026, 9, 11)


def runner(session, minutes=25, frequency=2):
    user = User(display_name="Runner")
    session.add(user)
    session.flush()
    for week in range(8):
        for index in range(frequency):
            age = week * 7 + (7 if index == 0 else 2 + index)
            session.add(
                Activity(
                    user_id=user.id,
                    garmin_activity_id=f"{user.id}-{age}",
                    name="Lauf",
                    activity_type="running",
                    started_at=datetime.combine(DAY - timedelta(days=age), datetime.min.time()),
                    duration_s=minutes * 60,
                    distance_m=minutes * 160,
                    workout_rpe=3,
                )
            )
    session.flush()
    return user


def test_dose_uses_running_history_not_free_time(session_factory):
    with session_factory() as session:
        low = runner(session)
        high = runner(session, 55, 4)
        first = recommend_today(session, low, as_of=DAY, available_minutes=120)
        second = recommend_today(session, high, as_of=DAY, available_minutes=120)
        assert first.state == second.state == "workout"
        assert first.duration_seconds == 25 * 60
        assert second.duration_seconds > first.duration_seconds
        assert second.duration_seconds <= 55 * 60
        assert (
            first.context_fingerprint
            == recommend_today(session, low, as_of=DAY, available_minutes=120).context_fingerprint
        )
        assert session.scalar(select(func.count()).select_from(Workout)) == 0


def test_save_is_idempotent_and_unscheduled(session_factory):
    with session_factory() as session:
        user = runner(session)
        preview = recommend_today(session, user, as_of=DAY)
        first = save_recommendation(
            session, user, as_of=DAY, expected_fingerprint=preview.context_fingerprint
        )
        second = save_recommendation(
            session, user, as_of=DAY, expected_fingerprint=preview.context_fingerprint
        )
        assert isinstance(first, Workout) and isinstance(second, Workout)
        assert first.id == second.id
        assert first.accepted_revision_id is None
        assert first.scheduled_for is None
        assert first.source_assistant_message_id is None


def test_completed_run_and_zero_time_take_precedence(session_factory):
    with session_factory() as session:
        user = runner(session)
        assert recommend_today(session, user, as_of=DAY, available_minutes=0).state == "rest"
        session.add(
            Activity(
                user_id=user.id,
                garmin_activity_id="today",
                name="Schon erledigt",
                activity_type="running",
                started_at=datetime.combine(DAY, datetime.min.time()),
                duration_s=1500,
            )
        )
        session.flush()
        assert recommend_today(session, user, as_of=DAY, available_minutes=0).state == "completed"


def test_today_routes_without_provider_and_stale_save(client, session_factory, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "llm_api_key", "")
    page = client.get("/coach")
    assert page.status_code == 200
    assert "Heute" in page.text and "Als Vorschlag speichern" in page.text
    preview = client.get("/coach/today").json()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
    stale = client.post("/coach/today/save", data={"context_fingerprint": "0" * 64})
    assert stale.status_code == 409
    result = client.post(
        "/coach/today/save",
        data={"context_fingerprint": preview["context_fingerprint"]},
        follow_redirects=False,
    )

    assert result.status_code == 303
    repeated = client.post(
        "/coach/today/save",
        data={"context_fingerprint": preview["context_fingerprint"]},
        follow_redirects=False,
    )
    assert repeated.headers["location"] == result.headers["location"]
    client.headers.pop("X-CSRF-Token")
    assert (
        client.post(
            "/coach/today/save", data={"context_fingerprint": preview["context_fingerprint"]}
        ).status_code
        == 403
    )


def accepted_generated(
    session, user, *, day=DAY, template="threshold_cruise", repetitions=5, work=6
):
    data, metadata = RunningProposalService(session, user, as_of=day).build_candidate(
        template_id=template,
        suggested_for=day,
        available_minutes=90,
        edit_source="generator",
        parameters=TemplateParameters(repetitions=repetitions, work_minutes=work),
    )
    workout = WorkoutService(session, user).create_proposal(
        data,
        metadata,
        idempotency_key=f"test-{user.id}-{day}-{template}",
        request_fingerprint=f"test-{day}",
    )
    # Historical fixture: accepted content must remain distinct from future proposals.
    workout.accepted_revision_id = workout.current_revision_id
    workout.approval_status = "accepted"
    workout.scheduled_for = day
    workout.local_schedule_status = "scheduled"
    session.flush()
    return workout


def test_threshold_dose_maintains_comparable_success_and_compiles(session_factory):
    with session_factory() as session:
        low, high = runner(session, 40, 3), runner(session, 60, 5)
        for user in (low, high):
            session.add(AthleteGoal(user_id=user.id, event_type="10k"))
        old = accepted_generated(session, high, day=DAY - timedelta(days=10))
        session.add(
            Activity(
                user_id=high.id,
                garmin_activity_id="quality-complete",
                name="Intervalle",
                activity_type="running",
                started_at=datetime.combine(DAY - timedelta(days=10), datetime.min.time()),
                duration_s=3900,
                workout_id=old.id,
                workout_rpe=7,
                workout_feel=4,
            )
        )
        session.flush()
        a = recommend_today(session, low, as_of=DAY, available_minutes=90)
        b = recommend_today(session, high, as_of=DAY, available_minutes=90)
        assert a.template_id == b.template_id == "threshold_cruise"
        assert a.work_seconds == 15 * 60
        assert b.work_seconds == 30 * 60
        assert a.duration_seconds == 46 * 60 and b.duration_seconds == 65 * 60
        for result in (a, b):
            workout = save_recommendation(
                session,
                low if result is a else high,
                as_of=DAY,
                available_minutes=90,
                expected_fingerprint=result.context_fingerprint,
            )
            assert isinstance(workout, Workout)
            revision = session.get(WorkoutRevision, workout.current_revision_id)
            assert revision.definition_model == result.definition
            assert compile_workout_with_report(revision).payload["workoutSegments"]


@pytest.mark.parametrize(
    "sport,rpe,minutes,age",
    [("cycling", 8, 70, 0), ("cycling", 8, 70, 1), ("running", 8, 30, 1), ("running", 3, 90, 1)],
)
def test_recent_demand_changes_new_and_scheduled_work(session_factory, sport, rpe, minutes, age):
    with session_factory() as session:
        user = runner(session, 50, 4)
        session.add(AthleteGoal(user_id=user.id, event_type="10k"))
        session.flush()
        assert (
            recommend_today(session, user, as_of=DAY, available_minutes=80).template_id
            == "threshold_cruise"
        )
        activity = session.scalar(
            select(Activity).where(Activity.user_id == user.id).order_by(Activity.started_at.desc())
        )
        activity.started_at = datetime.combine(DAY - timedelta(days=age), datetime.min.time())
        activity.activity_type, activity.workout_rpe, activity.duration_s = sport, rpe, minutes * 60
        session.flush()
        result = recommend_today(session, user, as_of=DAY, available_minutes=80)
        assert result.template_id == "easy_run"
        workout = accepted_generated(session, user)
        revision = session.get(WorkoutRevision, workout.accepted_revision_id)
        result = recommend_today(session, user, as_of=DAY)
        assert result.state == "scheduled" and result.definition == revision.definition_model
        preview = DailyAdaptationService(session, user, as_of=DAY).assess_today(workout.id)
        recommended = [c for c in preview.assessment.candidates if c.recommended]
        assert len(recommended) == 1
        assert recommended[0].adaptation_class == DailyAdaptationClass.REPLACE_WITH_EASY
        assert any(
            (DAY - timedelta(days=age)).isoformat() in c for c in recommended[0].reason_codes
        )


def test_explicit_unavailability_short_budget_and_long_history_bound(session_factory):
    with session_factory() as session:
        user = runner(session, 40, 3)
        session.add(
            AthletePlanningProfile(user_id=user.id, preferred_long_run_weekday=DAY.weekday())
        )
        session.flush()
        result = recommend_today(session, user, as_of=DAY, available_minutes=120)
        assert result.template_id == "easy_run" and result.duration_seconds == 2400
        session.add(AthleteGoal(user_id=user.id, event_type="10k"))
        session.flush()
        assert (
            recommend_today(session, user, as_of=DAY, available_minutes=30).template_id
            == "easy_run"
        )
        assert recommend_today(session, user, as_of=DAY, available_minutes=15).state == "rest"
        session.add(AthleteAvailability(user_id=user.id, weekday=DAY.weekday(), available=False))
        session.flush()
        assert recommend_today(session, user, as_of=DAY, available_minutes=120).state == "rest"


@pytest.mark.parametrize("illness", ["fever", "systemic"])
def test_serious_feedback_yields_rest(session_factory, illness):
    with session_factory() as session:
        user = runner(session)
        session.add(
            PreSessionFeedback(
                user_id=user.id,
                illness_signal=illness,
                source="workout_safety",
                content_hash="f" * 64,
                recorded_at=datetime.combine(DAY, datetime.min.time()),
            )
        )
        session.flush()
        assert recommend_today(session, user, as_of=DAY).state == "rest"


def test_corroborated_recovery_and_reentry(session_factory):
    from tests.test_training_fit import _seed_health

    with session_factory() as session:
        user = runner(session, 50, 3)
        session.add(AthletePlanningProfile(user_id=user.id, self_declared_reentry=True))
        session.flush()
        preview = recommend_today(session, user, as_of=DAY, available_minutes=120)
        assert preview.template_id == "easy_run" and preview.duration_seconds <= 1800
        _seed_health(session, user.id, DAY, resting_hr=80, hrv=20)
        assert recommend_today(session, user, as_of=DAY).state == "rest"


def test_user_isolation_rejection_and_accepted_revision_preserved(session_factory):
    with session_factory() as session:
        user, other = runner(session), runner(session)
        preview = recommend_today(session, user, as_of=DAY)
        assert not isinstance(
            save_recommendation(
                session, other, as_of=DAY, expected_fingerprint=preview.context_fingerprint
            ),
            Workout,
        )
        workout = save_recommendation(
            session, user, as_of=DAY, expected_fingerprint=preview.context_fingerprint
        )
        assert isinstance(workout, Workout)
        revision = session.get(WorkoutRevision, workout.current_revision_id)
        identity = RevisionIdentity(
            revision.id, revision.revision_number, revision.content_hash, workout.lock_version
        )
        service = WorkoutService(session, user)
        service.reject(workout.id, RejectRevisionCommand(identity))
        with pytest.raises(WorkoutTransitionError, match="abgelehnt"):
            save_recommendation(
                session, user, as_of=DAY, expected_fingerprint=preview.context_fingerprint
            )


def test_save_reuses_accepted_proposal_without_overwriting_it(session_factory):
    with session_factory() as session:
        user = runner(session)
        preview = recommend_today(session, user, as_of=DAY)
        workout = save_recommendation(
            session, user, as_of=DAY, expected_fingerprint=preview.context_fingerprint
        )
        assert isinstance(workout, Workout)
        revision = session.get(WorkoutRevision, workout.current_revision_id)
        service = WorkoutService(session, user)
        service.accept(
            workout.id,
            AcceptRevisionCommand(
                RevisionIdentity(
                    revision.id,
                    revision.revision_number,
                    revision.content_hash,
                    workout.lock_version,
                ),
                service.acceptance_context(workout.id).fingerprint,
            ),
        )
        again = save_recommendation(
            session, user, as_of=DAY, expected_fingerprint=preview.context_fingerprint
        )
        assert isinstance(again, Workout)
        assert again.id == workout.id and again.accepted_revision_id == revision.id


def test_missing_health_keeps_scheduled_quality_and_pending_revision_does_not_replace_it(
    session_factory,
):
    from app.services.planning.workout_proposals import RunningRevisionRequest

    with session_factory() as session:
        user = runner(session, 50, 4)
        workout = accepted_generated(session, user)
        original = session.get(WorkoutRevision, workout.accepted_revision_id)
        preview = recommend_today(session, user, as_of=DAY)
        assert preview.adaptation == "Training beibehalten"
        RunningProposalService(session, user, as_of=DAY).revise(
            RunningRevisionRequest(
                workout_id=workout.id,
                revision_id=original.id,
                suggested_for=DAY,
                available_minutes=50,
                idempotency_key="pending-revision",
            )
        )
        after = recommend_today(session, user, as_of=DAY)
        assert after.revision_id == original.id
        assert after.definition == original.definition_model


def test_unscheduled_unaccepted_child_does_not_count_as_planned_training(session_factory):
    with session_factory() as session:
        user = runner(session, 50, 4)
        session.add(AthleteGoal(user_id=user.id, event_type="10k"))
        child = accepted_generated(session, user)
        child.accepted_revision_id = None
        child.approval_status = "proposed"
        child.scheduled_for = None
        child.local_schedule_status = "unscheduled"
        session.flush()
        assert recommend_today(session, user, as_of=DAY, available_minutes=80).state == "workout"


def test_week_boundary_hard_run_suppresses_quality(session_factory):
    monday = date(2026, 9, 14)
    with session_factory() as session:
        user = runner(session, 50, 4)
        session.add(AthleteGoal(user_id=user.id, event_type="10k"))
        latest = session.scalar(
            select(Activity).where(Activity.user_id == user.id).order_by(Activity.started_at.desc())
        )
        latest.started_at = datetime.combine(monday - timedelta(days=1), datetime.min.time())
        latest.workout_rpe = 8
        session.flush()
        result = recommend_today(session, user, as_of=monday, available_minutes=80)
        assert result.template_id == "easy_run"
        assert "13.09." in result.reasons[0]


def test_partial_comparable_completion_cannot_increase_dose(session_factory):
    from app.models import PostSessionFeedback

    with session_factory() as session:
        user = runner(session, 60, 4)
        session.add(AthleteGoal(user_id=user.id, event_type="10k"))
        workout = accepted_generated(session, user, day=DAY - timedelta(days=10))
        activity = Activity(
            user_id=user.id,
            garmin_activity_id="partial",
            name="Abbruch",
            activity_type="running",
            workout_id=workout.id,
            started_at=datetime.combine(DAY - timedelta(days=10), datetime.min.time()),
            duration_s=4000,
            workout_rpe=7,
            workout_feel=4,
        )
        session.add(activity)
        session.flush()
        session.add(
            PostSessionFeedback(
                user_id=user.id,
                activity_id=activity.id,
                activity_user_id=user.id,
                completion_percent=50,
                source="manual",
                content_hash="p" * 64,
                recorded_at=activity.started_at,
            )
        )
        session.flush()
        result = recommend_today(session, user, as_of=DAY, available_minutes=90)
        assert result.work_seconds == 15 * 60


def test_recent_subjective_time_and_mild_fatigue_reduce_only_with_evidence(session_factory):
    with session_factory() as session:
        user = runner(session, 50, 4)
        before = recommend_today(session, user, as_of=DAY)
        session.add(
            PreSessionFeedback(
                user_id=user.id,
                fatigue=5,
                leg_freshness=1,
                soreness=6,
                available_minutes=35,
                source="manual",
                content_hash="m" * 64,
                recorded_at=datetime.combine(DAY, datetime.min.time()),
            )
        )
        session.flush()
        after = recommend_today(session, user, as_of=DAY)
        assert after.duration_seconds < before.duration_seconds
        assert after.available_minutes == 35


def test_chat_read_uses_identical_daily_result(session_factory):
    import json

    from app.services.coach.conversation import CoachRuntimeContext
    from app.services.coach.tools import get_today_recommendation

    with session_factory() as session:
        user = runner(session)
        session.commit()
        expected = recommend_today(session, user, as_of=DAY)
        runtime = CoachRuntimeContext(user.id, DAY, session_factory)
    actual = json.loads(get_today_recommendation(runtime))
    assert expected.definition is not None
    assert actual["context_fingerprint"] == expected.context_fingerprint
    assert actual["definition"] == expected.definition.model_dump(mode="json")


def test_vo2_needs_quality_experience_and_varies_previous_stimulus(session_factory):
    from app.models import PerformanceAnchor
    from tests.test_running_baseline import _complete_activity_history

    with session_factory() as session:
        user = runner(session, 60, 4)
        session.add(AthleteGoal(user_id=user.id, event_type="5k"))
        session.add(_complete_activity_history(user.id, DAY))
        session.add(
            PerformanceAnchor(
                user_id=user.id,
                kind="race",
                source="manual",
                distance_m=5000,
                duration_s=1300,
                achieved_on=DAY - timedelta(days=18),
                reliable=True,
            )
        )
        for age, template, work in ((14, "vo2_intervals", 3), (10, "threshold_cruise", 6)):
            workout = accepted_generated(
                session, user, day=DAY - timedelta(days=age), template=template, work=work
            )
            session.add(
                Activity(
                    user_id=user.id,
                    garmin_activity_id=f"stimulus-{age}",
                    name="Erledigt",
                    activity_type="running",
                    workout_id=workout.id,
                    duration_s=3900,
                    workout_rpe=7,
                    workout_feel=4,
                    started_at=datetime.combine(DAY - timedelta(days=age), datetime.min.time()),
                )
            )
        session.flush()
        result = recommend_today(session, user, as_of=DAY, available_minutes=90)
        assert result.template_id == "vo2_intervals"
        assert result.work_seconds == 15 * 60


def test_known_short_strides_do_not_consume_sustained_quality_allocation(session_factory):
    from app.services.planning.workout_proposals import RunningProposalRequest

    with session_factory() as session:
        user = runner(session, 50, 4)
        session.add(AthleteGoal(user_id=user.id, event_type="10k"))
        old_day = DAY - timedelta(days=4)
        workout = RunningProposalService(session, user, as_of=old_day).create(
            RunningProposalRequest(
                template_id="strides",
                suggested_for=old_day,
                available_minutes=50,
                idempotency_key="old-strides",
            )
        )
        workout.accepted_revision_id = workout.current_revision_id
        existing = session.scalar(
            select(Activity).where(
                Activity.user_id == user.id,
                Activity.started_at == datetime.combine(old_day, datetime.min.time()),
            )
        )
        existing.workout_id, existing.workout_rpe, existing.workout_feel = workout.id, 7, 4
        session.flush()
        assert (
            recommend_today(session, user, as_of=DAY, available_minutes=80).template_id
            == "threshold_cruise"
        )


def test_close_race_and_nonfinite_duration_remain_conservative(session_factory):
    with session_factory() as session:
        user = runner(session, 50, 4)
        session.add(
            AthleteGoal(user_id=user.id, event_type="10k", target_date=DAY + timedelta(days=3))
        )
        session.add(
            Activity(
                user_id=user.id,
                garmin_activity_id="invalid-duration",
                name="Unvollständig",
                activity_type="cycling",
                duration_s=float("inf"),
                started_at=datetime.combine(DAY - timedelta(days=10), datetime.min.time()),
            )
        )
        session.flush()
        result = recommend_today(session, user, as_of=DAY, available_minutes=80)
        assert result.template_id == "easy_run" and result.duration_seconds < 50 * 60
