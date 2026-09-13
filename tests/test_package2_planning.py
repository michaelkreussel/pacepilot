from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.models import (
    Activity,
    AthleteGoal,
    PerformanceAnchor,
    PreSessionFeedback,
    TrainingPlanWorkout,
    Workout,
    WorkoutRevision,
)
from app.services.garmin.workout_export import compile_workout_with_report
from app.services.planning.multiweek_planner import plan_training_cycle
from app.services.planning.weekly_plan_service import persist_week_candidate
from app.services.planning.weekly_planner import DayAvailability, plan_shadow_week
from app.services.planning.workout_views import revision_view
from tests.test_daily_recommendation import DAY, runner


def test_current_week_has_no_past_or_catchup_sessions(session_factory):
    with session_factory() as session:
        user = runner(session, minutes=30, frequency=3)
        week = DAY - timedelta(days=DAY.weekday())
        candidate = plan_shadow_week(
            session,
            user,
            week_start=week,
            as_of=DAY,
            availability=tuple(DayAvailability(i, 120) for i in range(7)),
        )
        assert all(item.scheduled_for >= DAY for item in candidate.sessions)
        assert sum(item.planned_minutes for item in candidate.sessions) <= 90


def test_weekly_persistence_preserves_executable_preview(session_factory):
    with session_factory() as session:
        user = runner(session, minutes=55, frequency=4)
        week = DAY + timedelta(days=7 - DAY.weekday())
        candidate = plan_shadow_week(
            session,
            user,
            week_start=week,
            as_of=DAY,
            availability=tuple(DayAvailability(i, 120) for i in (0, 2, 4, 6)),
        )
        revision = persist_week_candidate(session, user, candidate)
        links = session.scalars(
            select(TrainingPlanWorkout)
            .where(TrainingPlanWorkout.plan_revision_id == revision.id)
            .order_by(TrainingPlanWorkout.position)
        ).all()
        for item, link in zip(candidate.sessions, links, strict=True):
            workout = session.get(Workout, link.workout_id)
            saved = session.get(WorkoutRevision, workout.current_revision_id)
            assert item.data is not None
            assert saved.definition_model == item.data.definition
            assert saved.load_estimate_json == item.load_estimate_json
            assert saved.generation_context_json.get("selected_parameters")


def test_quality_repetitions_targets_and_compilation_survive_plan_save(session_factory):
    from tests.test_running_baseline import _complete_activity_history

    with session_factory() as session:
        user = runner(session, minutes=55, frequency=4)
        session.add(_complete_activity_history(user.id, DAY))
        session.add(AthleteGoal(user_id=user.id, event_type="10k"))
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
        week = DAY + timedelta(days=7 - DAY.weekday())
        candidate = plan_shadow_week(
            session,
            user,
            week_start=week,
            as_of=DAY,
            availability=tuple(DayAvailability(i, 120) for i in (0, 2, 4, 6)),
        )
        quality = next(i for i in candidate.sessions if i.template_id == "threshold_cruise")
        assert quality.data is not None and quality.metadata is not None
        assert quality.metadata.generation_context_json is not None
        assert quality.metadata.guidance_json is not None
        parameters = quality.metadata.generation_context_json["selected_parameters"]
        assert isinstance(parameters, dict) and parameters["repetitions"] >= 3
        assert parameters.get("work_minutes") or parameters.get("work_distance_meters")
        assert parameters["warmup_minutes"] in (10, 15)
        pace_target = quality.metadata.guidance_json["pace_target"]
        assert isinstance(pace_target, dict) and pace_target["source_day"]
        revision = persist_week_candidate(session, user, candidate)
        saved = session.scalar(
            select(WorkoutRevision)
            .join(TrainingPlanWorkout, TrainingPlanWorkout.workout_id == WorkoutRevision.workout_id)
            .where(
                TrainingPlanWorkout.plan_revision_id == revision.id,
                WorkoutRevision.template_id == "threshold_cruise",
            )
        )
        assert saved.definition_model == quality.data.definition
        assert saved.guidance_json["pace_target"] == quality.metadata.guidance_json["pace_target"]
        preview = SimpleNamespace(
            name=quality.data.name,
            sport=quality.data.sport,
            description=quality.data.description,
            definition_model=quality.data.definition,
        )
        assert compile_workout_with_report(preview) == compile_workout_with_report(saved)
        assert revision_view(saved).target_label.startswith("Pace ")


def test_future_week_ignores_transient_recovery_and_respects_observed_volume(session_factory):
    with session_factory() as session:
        user = runner(session, minutes=55, frequency=4)
        session.add(AthleteGoal(user_id=user.id, event_type="10k"))
        week = DAY + timedelta(days=7 - DAY.weekday())
        availability = tuple(DayAvailability(i, 180) for i in range(7))
        before = plan_shadow_week(
            session, user, week_start=week, as_of=DAY, availability=availability
        )
        session.add(
            PreSessionFeedback(
                user_id=user.id,
                recorded_at=datetime.combine(DAY, datetime.min.time()),
                illness_signal="fever",
                pain_present=False,
                available_minutes=10,
                content_hash="fever",
            )
        )
        session.flush()
        after = plan_shadow_week(
            session, user, week_start=week, as_of=DAY, availability=availability
        )
        assert [i.data.definition for i in before.sessions if i.data] == [
            i.data.definition for i in after.sessions if i.data
        ]
        assert sum(i.planned_minutes for i in after.sessions) <= 220


def test_completed_sunday_quality_prevents_monday_quality(session_factory):
    with session_factory() as session:
        user = runner(session, minutes=55, frequency=4)
        session.add(AthleteGoal(user_id=user.id, event_type="10k"))
        sunday = DAY + timedelta(days=2)
        session.add(
            Activity(
                user_id=user.id,
                garmin_activity_id="sunday-hard",
                name="Hart",
                activity_type="running",
                started_at=datetime.combine(sunday, datetime.min.time()),
                duration_s=3000,
                workout_rpe=8,
            )
        )
        candidate = plan_shadow_week(
            session,
            user,
            week_start=sunday + timedelta(days=1),
            as_of=sunday,
            availability=tuple(DayAvailability(i, 90) for i in (0, 2, 4)),
        )
        assert all(
            i.role not in {"threshold_cruise", "vo2_intervals"}
            or (i.scheduled_for - sunday).days >= 2
            for i in candidate.sessions
        )


def test_cycle_uses_shared_selected_quality_and_exact_scaled_duration(session_factory):
    from math import ceil

    from app.models import AthleteAvailability
    from app.services.planning.workout_definition import estimated_duration_seconds

    with session_factory() as session:
        user = runner(session, minutes=55, frequency=4)
        for i in (0, 2, 4, 6):
            session.add(
                AthleteAvailability(
                    user_id=user.id, weekday=i, available=True, available_minutes=120
                )
            )
        week = DAY + timedelta(days=7 - DAY.weekday())
        cycle = plan_training_cycle(
            session,
            user,
            start_date=week,
            target_date=week + timedelta(weeks=7, days=6),
            as_of=DAY,
            event_type="10k",
        )
        assert cycle.validation_report["valid"]
        quality = [
            i
            for w in cycle.weeks
            for i in w.weekly_plan.sessions
            if i.template_id == "threshold_cruise"
        ]
        assert quality
        for item in quality:
            assert item.metadata is not None and item.metadata.generation_context_json is not None
            parameters = item.metadata.generation_context_json["selected_parameters"]
            assert isinstance(parameters, dict) and parameters["repetitions"] == 3
        for week in cycle.weeks:
            for item in week.weekly_plan.sessions:
                assert item.data is not None and item.metadata is not None
                assert (
                    ceil(estimated_duration_seconds(item.data.definition) / 60)
                    == item.planned_minutes
                )
                assert item.metadata.load_estimate_json == item.load_estimate_json


def test_linked_completed_and_accepted_work_count_once(session_factory):
    from tests.test_daily_recommendation import accepted_generated

    with session_factory() as session:
        user = runner(session, minutes=55, frequency=4)
        workout = accepted_generated(session, user, day=DAY)
        session.add(
            Activity(
                user_id=user.id,
                garmin_activity_id="linked",
                name="Erledigt",
                activity_type="running",
                started_at=datetime.combine(DAY, datetime.min.time()),
                duration_s=2400,
                workout_id=workout.id,
            )
        )
        candidate = plan_shadow_week(
            session,
            user,
            week_start=DAY - timedelta(days=DAY.weekday()),
            as_of=DAY,
            availability=tuple(DayAvailability(i, 120) for i in range(7)),
        )
        # Two 55-minute runs precede the linked 40-minute completion this week.
        facts = candidate.generation_context["accounting"]
        assert isinstance(facts, dict)
        assert facts["completed_and_remaining_minutes"] == 150
        assert all(i.scheduled_for > DAY for i in candidate.sessions)


def test_novice_and_experienced_weeks_differ_materially(session_factory):
    with session_factory() as session:
        novice, experienced = runner(session, 25, 2), runner(session, 55, 4)
        week = DAY + timedelta(days=7 - DAY.weekday())
        for user in (novice, experienced):
            session.add(AthleteGoal(user_id=user.id, event_type="10k"))
        availability = tuple(DayAvailability(i, 180) for i in range(7))
        first, second = (
            plan_shadow_week(
                session, novice, week_start=week, as_of=DAY, availability=availability
            ),
            plan_shadow_week(
                session, experienced, week_start=week, as_of=DAY, availability=availability
            ),
        )
        assert sum(i.planned_minutes for i in first.sessions) <= 50
        assert sum(i.planned_minutes for i in second.sessions) > 100
        assert not any(i.role in {"threshold_cruise", "vo2_intervals"} for i in first.sessions)
        assert any(i.role == "threshold_cruise" for i in second.sessions)
