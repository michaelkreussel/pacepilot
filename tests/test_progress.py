import json
from dataclasses import asdict
from datetime import date, datetime

import pytest
from sqlalchemy import select

from app.models import (
    Activity,
    AthleteGoal,
    DailyHealth,
    GarminSyncState,
    PostSessionFeedback,
    PreSessionFeedback,
    TrainingCycle,
    TrainingCycleRevision,
    TrainingCycleWeek,
    TrainingPlan,
    TrainingPlanRevision,
    TrainingPlanWorkout,
    User,
    Workout,
)
from app.services.analytics.progress import ProgressReferenceError, get_progress
from app.services.coach.conversation import CoachRuntimeContext
from app.services.coach.tools import get_adaptive_context

AS_OF = date(2026, 6, 28)


def _user(session, name: str) -> User:
    user = User(display_name=name)
    session.add(user)
    session.flush()
    return user


def _definition(identifier: str, *, seconds: int, meters: int) -> dict[str, object]:
    return {
        "blocks": [
            {
                "id": f"{identifier}-time",
                "kind": "step",
                "step_type": "interval",
                "end": {"type": "time", "seconds": seconds},
                "target": {"type": "none"},
            },
            {
                "id": f"{identifier}-distance",
                "kind": "step",
                "step_type": "interval",
                "end": {"type": "distance", "meters": meters},
                "target": {"type": "none"},
            },
        ]
    }


def _accepted_cycle(
    session,
    user: User,
    goal: AthleteGoal,
    *,
    revision_number: int = 1,
    workout_dates: tuple[date, ...] = (date(2026, 6, 23), date(2026, 6, 25)),
    phase: str = "build",
) -> tuple[TrainingCycle, TrainingCycleRevision, tuple[Workout, ...]]:
    cycle = session.scalar(
        select(TrainingCycle).where(TrainingCycle.user_id == user.id).order_by(TrainingCycle.id)
    )
    if cycle is None:
        cycle = TrainingCycle(
            user_id=user.id,
            goal_id=goal.id,
            event_type=goal.event_type,
            start_date=date(2026, 6, 1),
            target_date=goal.target_date,
        )
        session.add(cycle)
        session.flush()

    revision = TrainingCycleRevision(
        cycle_id=cycle.id,
        owner_user_id=user.id,
        revision_number=revision_number,
        event_type=goal.event_type,
        start_date=cycle.start_date,
        target_date=cycle.target_date,
        planner_version="test",
        knowledge_base_version="test",
        input_fingerprint=str(revision_number) * 64,
        confidence="high",
        phase_plan_json=[],
        assumptions_json={},
        impact_json={"projected_sessions": 99, "projected_completion_percent": 100},
        validation_report_json={"valid": True},
    )
    session.add(revision)
    session.flush()

    plan = session.scalar(
        select(TrainingPlan).where(
            TrainingPlan.user_id == user.id,
            TrainingPlan.week_start == date(2026, 6, 22),
        )
    )
    if plan is None:
        plan = TrainingPlan(
            user_id=user.id,
            week_start=date(2026, 6, 22),
            status="active",
        )
        session.add(plan)
        session.flush()
    plan_revision = TrainingPlanRevision(
        plan_id=plan.id,
        owner_user_id=user.id,
        revision_number=revision_number,
        week_start=plan.week_start,
        week_end=date(2026, 6, 28),
        planner_version="test",
        knowledge_base_version="test",
        input_fingerprint=f"{revision_number:x}" * 64,
        generation_context_json={},
        validation_report_json={"valid": True},
    )
    session.add(plan_revision)
    session.flush()
    plan.current_revision_id = plan_revision.id
    session.add(
        TrainingCycleWeek(
            cycle_revision_id=revision.id,
            training_plan_revision_id=plan_revision.id,
            owner_user_id=user.id,
            position=0,
            week_start=plan.week_start,
            phase=phase,
        )
    )

    workouts = []
    for position, scheduled_for in enumerate(workout_dates):
        workout = Workout(
            user_id=user.id,
            name=f"Plan {revision_number}-{position}",
            sport="running",
            scheduled_for=None,
            status="draft",
            source_type="weekly_plan",
            approval_status="proposed",
            local_schedule_status="unscheduled",
            definition=_definition(f"r{revision_number}-{position}", seconds=1_800, meters=5_000),
        )
        session.add(workout)
        session.flush()
        session.add(
            TrainingPlanWorkout(
                plan_revision_id=plan_revision.id,
                workout_id=workout.id,
                owner_user_id=user.id,
                position=position,
                role="easy" if position == 0 else "long_run",
                scheduled_for=scheduled_for,
            )
        )
        workouts.append(workout)

    cycle.current_revision_id = revision.id
    cycle.accepted_revision_id = revision.id
    return cycle, revision, tuple(workouts)


def test_progress_compares_accepted_plan_with_observed_work_and_feedback(session_factory) -> None:
    with session_factory() as session:
        user = _user(session, "Progress")
        other = _user(session, "Other")
        goal = AthleteGoal(
            user_id=user.id,
            event_type="10k",
            event_name="Sommerlauf",
            target_date=date(2026, 8, 30),
        )
        session.add(goal)
        session.flush()
        _, revision, workouts = _accepted_cycle(session, user, goal)
        other_workout = Workout(
            user_id=other.id,
            name="Hidden",
            sport="running",
            status="draft",
            definition=_definition("hidden", seconds=9_999, meters=99_999),
        )
        session.add(other_workout)
        session.flush()

        linked = Activity(
            user_id=user.id,
            garmin_activity_id="linked",
            name="Linked",
            activity_type="running",
            started_at=datetime(2026, 6, 23, 8),
            duration_s=1_900,
            distance_m=5_100,
            workout_id=workouts[0].id,
        )
        unlinked = Activity(
            user_id=user.id,
            garmin_activity_id="unlinked",
            name="Unlinked",
            activity_type="running",
            started_at=datetime(2026, 6, 26, 8),
            duration_s=2_000,
            distance_m=6_000,
        )
        prior = Activity(
            user_id=user.id,
            garmin_activity_id="prior",
            name="Prior",
            activity_type="running",
            started_at=datetime(2026, 6, 20, 8),
            duration_s=1_000,
            distance_m=3_000,
        )
        invalid_cross_user_link = Activity(
            user_id=user.id,
            garmin_activity_id="cross-link",
            name="Cross link",
            activity_type="running",
            started_at=datetime(2026, 6, 27, 8),
            duration_s=900,
            distance_m=2_000,
            workout_id=other_workout.id,
        )
        hidden = Activity(
            user_id=other.id,
            garmin_activity_id="hidden",
            name="Hidden",
            activity_type="running",
            started_at=datetime(2026, 6, 24, 8),
            duration_s=9_999,
            distance_m=99_999,
            workout_id=workouts[1].id,
        )
        session.add_all([linked, unlinked, prior, invalid_cross_user_link, hidden])
        session.flush()
        session.add_all(
            [
                GarminSyncState(
                    user_id=user.id,
                    resource="activities",
                    status="ok",
                    backfill_complete=True,
                    oldest_synced_date=date(2026, 6, 1),
                    newest_synced_date=AS_OF,
                ),
                PostSessionFeedback(
                    user_id=user.id,
                    workout_id=workouts[0].id,
                    workout_user_id=user.id,
                    activity_id=linked.id,
                    activity_user_id=user.id,
                    completion_percent=80,
                    session_rpe=8,
                    pain_present=True,
                    pain_location="Knie",
                    stopped_reason="Schmerzen",
                    content_hash="a" * 64,
                    recorded_at=datetime(2026, 6, 23, 9),
                ),
                PostSessionFeedback(
                    user_id=user.id,
                    workout_id=workouts[0].id,
                    workout_user_id=user.id,
                    completion_percent=0,
                    pain_present=True,
                    stopped_reason="Veraltete Rückmeldung",
                    content_hash="d" * 64,
                    recorded_at=datetime(2026, 6, 1, 9),
                ),
                PreSessionFeedback(
                    user_id=user.id,
                    workout_id=workouts[1].id,
                    workout_user_id=user.id,
                    fatigue=4,
                    pain_present=False,
                    illness_signal="mild_upper_respiratory",
                    content_hash="b" * 64,
                    recorded_at=datetime(2026, 6, 25, 7),
                ),
                DailyHealth(
                    user_id=user.id,
                    day=AS_OF,
                    sleep_seconds=18_000,
                    sleep_need_seconds=28_800,
                ),
            ]
        )
        session.commit()

        progress = get_progress(session, user.id, as_of=AS_OF, days=7, goal_id=goal.id)

    assert progress.period.start == date(2026, 6, 22)
    assert progress.period.end == AS_OF
    assert progress.goal is not None
    assert progress.goal.target_date == date(2026, 8, 30)
    assert progress.plan is not None
    assert progress.plan.accepted_revision_id == revision.id
    assert progress.plan.phase == "build"
    assert progress.comparison.planned_sessions == 2
    assert progress.comparison.completed_planned_sessions == 1
    assert progress.comparison.observed_activity_sessions == 3
    assert progress.comparison.planned_duration_s == 3_600
    assert progress.comparison.completed_duration_s == 1_900
    assert progress.comparison.adherence_percent == 50
    assert progress.matching.matched_sessions == 1
    assert progress.matching.unmatched_planned_sessions == 1
    assert progress.matching.unmatched_activity_sessions == 2
    assert progress.matching.linkage_confidence == "medium"
    assert progress.feedback.completion_percent == 80
    assert progress.feedback.pain_reports == 1
    assert progress.feedback.stopped_sessions == 1
    assert progress.feedback.illness_signals == ("mild_upper_respiratory",)
    assert progress.trend is not None
    assert progress.trend.session_change == 2
    assert progress.trend.consistency_percent == 100
    assert progress.recovery is not None
    assert set(progress.recovery.constraints) >= {
        "illness:mild_upper_respiratory",
        "pain_reported",
        "session_stopped",
        "sleep_shortfall",
    }
    assert progress.coverage.activity_period_complete is True
    assert progress.coverage.planned_duration_complete is False
    assert progress.coverage.planned_distance_complete is False
    assert set(progress.uncertainty) == {
        "planned_duration_incomplete",
        "planned_distance_incomplete",
    }


def test_progress_keeps_missing_sources_unavailable_instead_of_zero(session_factory) -> None:
    with session_factory() as session:
        user = _user(session, "No coverage")
        session.add_all(
            [
                Activity(
                    user_id=user.id,
                    garmin_activity_id="incomplete",
                    name="Incomplete",
                    activity_type="running",
                    started_at=datetime(2026, 6, 27, 8),
                ),
                GarminSyncState(
                    user_id=user.id,
                    resource="activities",
                    status="error",
                    backfill_complete=False,
                ),
            ]
        )
        session.commit()

        progress = get_progress(session, user.id, as_of=AS_OF, days=7)

    assert progress.goal is None
    assert progress.plan is None
    assert progress.comparison.planned_sessions is None
    assert progress.comparison.completed_planned_sessions is None
    assert progress.comparison.observed_activity_sessions is None
    assert progress.comparison.adherence_percent is None
    assert progress.matching.matched_sessions is None
    assert progress.matching.linkage_confidence == "unavailable"
    assert progress.trend is None
    assert progress.recovery is None
    assert set(progress.uncertainty) >= {
        "accepted_plan_unavailable",
        "activity_sync_unavailable",
        "linkage_unavailable",
        "recovery_unavailable",
        "trend_unavailable",
    }


def test_incomplete_sync_does_not_become_zero_completion(session_factory) -> None:
    with session_factory() as session:
        user = _user(session, "Incomplete period")
        goal = AthleteGoal(
            user_id=user.id,
            event_type="5k",
            target_date=date(2026, 8, 1),
        )
        session.add(goal)
        session.flush()
        _accepted_cycle(session, user, goal, workout_dates=(date(2026, 6, 24),))
        session.add(
            GarminSyncState(
                user_id=user.id,
                resource="activities",
                status="ok",
                backfill_complete=False,
                oldest_synced_date=date(2026, 6, 25),
                newest_synced_date=AS_OF,
            )
        )
        session.commit()

        progress = get_progress(session, user.id, as_of=AS_OF, days=7)

    assert progress.comparison.planned_sessions == 1
    assert progress.comparison.completed_planned_sessions is None
    assert progress.comparison.observed_activity_sessions is None
    assert progress.comparison.adherence_percent is None
    assert progress.matching.matched_sessions is None
    assert "activity_sync_incomplete" in progress.uncertainty


def test_accepted_cycle_is_not_paired_with_an_unrelated_active_goal(session_factory) -> None:
    with session_factory() as session:
        user = _user(session, "Goal alignment")
        cycle_goal = AthleteGoal(
            user_id=user.id,
            event_type="10k",
            target_date=date(2026, 8, 30),
        )
        session.add(cycle_goal)
        session.flush()
        cycle, _, _ = _accepted_cycle(session, user, cycle_goal)
        cycle_goal.status = "archived"
        unrelated = AthleteGoal(
            user_id=user.id,
            event_type="marathon",
            target_date=date(2026, 10, 1),
        )
        session.add(unrelated)
        session.commit()

        progress = get_progress(session, user.id, as_of=AS_OF, days=7)

    assert progress.plan is not None and progress.plan.cycle_id == cycle.id
    assert progress.goal is not None and progress.goal.id == cycle_goal.id


def test_progress_recomputes_changed_goal_activity_feedback_and_accepted_plan(
    session_factory,
) -> None:
    with session_factory() as session:
        user = _user(session, "Changed facts")
        goal = AthleteGoal(
            user_id=user.id,
            event_type="10k",
            target_date=date(2026, 8, 30),
        )
        session.add(goal)
        session.flush()
        cycle, first_revision, workouts = _accepted_cycle(
            session,
            user,
            goal,
            workout_dates=(date(2026, 6, 23),),
        )
        session.add(
            GarminSyncState(
                user_id=user.id,
                resource="activities",
                status="ok",
                backfill_complete=True,
                oldest_synced_date=date(2026, 6, 1),
                newest_synced_date=AS_OF,
            )
        )
        session.commit()

        before = get_progress(session, user.id, as_of=AS_OF, days=7, goal_id=goal.id)
        runtime = CoachRuntimeContext(user.id, AS_OF, session_factory)
        before_context = json.loads(
            get_adaptive_context(runtime, focus="progress", days=7, goal_id=goal.id)
        )
        assert before.comparison.completed_planned_sessions == 0
        assert before.comparison.observed_activity_sessions == 0
        assert before.comparison.planned_sessions == 1

        goal.target_date = date(2026, 9, 6)
        activity = Activity(
            user_id=user.id,
            garmin_activity_id="new-linked",
            name="New linked",
            activity_type="running",
            started_at=datetime(2026, 6, 23, 8),
            duration_s=1_850,
            distance_m=5_000,
            workout_id=workouts[0].id,
        )
        session.add(activity)
        session.flush()
        session.add(
            PostSessionFeedback(
                user_id=user.id,
                workout_id=workouts[0].id,
                workout_user_id=user.id,
                activity_id=activity.id,
                activity_user_id=user.id,
                completion_percent=90,
                pain_present=False,
                content_hash="c" * 64,
                recorded_at=datetime(2026, 6, 23, 9),
            )
        )
        _, second_revision, _ = _accepted_cycle(
            session,
            user,
            goal,
            revision_number=2,
            workout_dates=(date(2026, 6, 23), date(2026, 6, 25)),
            phase="peak",
        )
        cycle.current_revision_id = second_revision.id
        cycle.accepted_revision_id = second_revision.id
        session.commit()

        after = get_progress(session, user.id, as_of=AS_OF, days=7, goal_id=goal.id)
        after_context = json.loads(
            get_adaptive_context(runtime, focus="progress", days=7, goal_id=goal.id)
        )

    assert first_revision.id != second_revision.id
    assert after.goal is not None and after.goal.target_date == date(2026, 9, 6)
    assert after.plan is not None and after.plan.phase == "peak"
    assert after.comparison.planned_sessions == 2
    assert after.comparison.completed_planned_sessions == 0
    assert after.comparison.observed_activity_sessions == 1
    assert after.feedback.completion_percent == 90
    assert before_context != after_context
    assert after_context["planning"]["goals"][0]["target_date"] == "2026-09-06"
    assert after_context["progress"]["plan"]["phase"] == "peak"
    assert after_context["progress"]["comparison"]["observed_activity_sessions"] == 1
    assert after_context["progress"]["feedback"]["completion_percent"] == 90


def test_progress_ignores_projected_impact_and_rejects_cross_user_goal(session_factory) -> None:
    with session_factory() as session:
        user = _user(session, "Owner")
        other = _user(session, "Other")
        goal = AthleteGoal(
            user_id=user.id,
            event_type="5k",
            target_date=date(2026, 8, 1),
        )
        hidden_goal = AthleteGoal(
            user_id=other.id,
            event_type="marathon",
            event_name="Hidden goal",
            target_date=date(2026, 10, 1),
        )
        session.add_all([goal, hidden_goal])
        session.flush()
        _accepted_cycle(session, user, goal, workout_dates=(date(2026, 6, 24),))
        session.add(
            GarminSyncState(
                user_id=user.id,
                resource="activities",
                status="ok",
                backfill_complete=True,
                oldest_synced_date=date(2026, 6, 1),
                newest_synced_date=AS_OF,
            )
        )
        session.commit()

        progress = get_progress(session, user.id, as_of=AS_OF, days=7, goal_id=goal.id)
        with pytest.raises(ProgressReferenceError, match="goal not found"):
            get_progress(session, user.id, as_of=AS_OF, days=7, goal_id=hidden_goal.id)

    payload = asdict(progress)
    assert progress.comparison.planned_sessions == 1
    assert progress.comparison.completed_planned_sessions == 0
    assert progress.comparison.adherence_percent == 0
    assert "impact" not in repr(payload).lower()
    assert "projected" not in repr(payload).lower()


@pytest.mark.parametrize("days", [0, 6, 85])
def test_progress_rejects_unbounded_periods(session_factory, days: int) -> None:
    with session_factory() as session:
        user = _user(session, "Bounds")
        with pytest.raises(ValueError, match="days must be between 7 and 84"):
            get_progress(session, user.id, as_of=AS_OF, days=days)
