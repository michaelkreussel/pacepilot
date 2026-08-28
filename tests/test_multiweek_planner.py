from dataclasses import replace
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AthleteGoal,
    TrainingCycle,
    TrainingCycleRevision,
    TrainingCycleWeek,
    TrainingPlan,
    TrainingPlanRevision,
    User,
    Workout,
    WorkoutRevision,
)
from app.services.planning.multiweek_planner import (
    MultiweekPlannerError,
    TrainingCyclePersistenceError,
    accept_training_cycle_revision,
    compose_training_cycle,
    persist_training_cycle,
    plan_training_cycle,
)
from app.services.planning.planning_queries import (
    get_accepted_training_cycle,
    get_current_training_cycle,
    list_accepted_training_cycles,
    list_current_training_plans,
    list_training_cycle_week_details,
    list_training_cycles,
)
from app.services.planning.registry import get_knowledge_registry
from app.services.planning.weekly_planner import (
    DayAvailability,
    GoalSummary,
    WeeklyPlanCandidate,
    WeeklyPlannerSnapshot,
    compose_week,
)

START = date(2026, 8, 31)


def _weekly_candidates(
    count: int = 8, *, enable_deferred_quality: bool = False
) -> tuple[WeeklyPlanCandidate, ...]:
    output = []
    for offset in range(count):
        snapshot = WeeklyPlannerSnapshot(
            week_start=START + timedelta(weeks=offset),
            as_of=date(2026, 8, 26),
            availability=(
                DayAvailability(weekday=0, available_minutes=60),
                DayAvailability(weekday=2, available_minutes=75),
                DayAvailability(weekday=5, available_minutes=120),
                DayAvailability(weekday=6, available_minutes=90),
            ),
            preferred_long_run_weekday=6,
            experience_level="intermediate",
            effective_reentry=False,
            goals=(GoalSummary(event_type="half_marathon", status="active", target_date=None),),
            baseline_confidence="medium",
            typical_weekly_runs_median=3.0,
            observed_runs_per_week=3.0,
            consistent_running_weeks=4,
            longest_run_28d_seconds=4800,
            typical_longest_run_seconds=4200,
            median_run_seconds=2700,
            hard_runs_28d=1,
            intensity_mode="rpe_talk_test",
            intensity_confidence="medium",
            baseline_fingerprint="b" * 64,
            intensity_fingerprint="i" * 64,
            knowledge_base_version=get_knowledge_registry().version,
        )
        output.append(
            compose_week(
                snapshot,
                enforce_history_gates=not enable_deferred_quality,
                enable_deferred_quality=enable_deferred_quality,
            )
        )
    return tuple(output)


def _user(session: Session) -> User:
    user = User(display_name="Cycle Runner")
    session.add(user)
    session.flush()
    return user


def test_cycle_has_versioned_phases_and_taper() -> None:
    cycle = compose_training_cycle(
        _weekly_candidates(),
        start_date=START,
        target_date=START + timedelta(weeks=7, days=6),
        event_type="half_marathon",
    )

    assert [week.phase for week in cycle.weeks] == [
        "base",
        "base",
        "build",
        "build",
        "build",
        "specific",
        "taper",
        "taper",
    ]
    assert cycle.validation_report["valid"] is True
    assert cycle.weeks[-1].total_minutes < cycle.weeks[-2].total_minutes
    assert cycle.weeks[-1].weekly_plan.sessions
    assert all(item.role != "strides" for item in cycle.weeks[-1].weekly_plan.sessions)


def test_development_cycle_uses_phase_specific_quality_templates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "coach_deferred_quality_templates_enabled", True)
    cycle = compose_training_cycle(
        _weekly_candidates(enable_deferred_quality=True),
        start_date=START,
        target_date=START + timedelta(weeks=7, days=6),
        event_type="10k",
        enable_deferred_quality=True,
    )

    templates_by_phase = {
        week.phase: {item.template_id for item in week.weekly_plan.sessions} for week in cycle.weeks
    }
    assert "threshold_cruise" in templates_by_phase["build"]
    assert "vo2_intervals" in templates_by_phase["specific"]
    assert "threshold_cruise" not in templates_by_phase["base"]
    assert "vo2_intervals" not in templates_by_phase["taper"]
    assert cycle.validation_report["valid"] is True
    assert cycle.assumptions["deferred_quality_development_override"] is True


def test_partial_target_week_does_not_fail_taper_reduction() -> None:
    target_date = START + timedelta(weeks=7, days=1)

    cycle = compose_training_cycle(
        _weekly_candidates(),
        start_date=START,
        target_date=target_date,
        event_type="half_marathon",
    )

    assert cycle.validation_report["valid"] is True
    assert cycle.weeks[-1].phase == "taper"
    assert all(
        session.scheduled_for <= target_date for session in cycle.weeks[-1].weekly_plan.sessions
    )


def test_cycle_reentry_and_interruptions_never_catch_up() -> None:
    cycle = compose_training_cycle(
        _weekly_candidates(),
        start_date=START,
        target_date=START + timedelta(weeks=7, days=6),
        event_type="10k",
        effective_reentry=True,
        interrupted_weeks=frozenset({2}),
    )

    assert cycle.weeks[0].phase == "reentry"
    assert cycle.weeks[1].phase == "reentry"
    assert cycle.weeks[2].phase == "recovery"
    assert cycle.weeks[2].weekly_plan.sessions == ()
    assert cycle.validation_report["valid"] is True
    assert cycle.assumptions["interrupted_weeks"] == [2]


def test_cycle_progression_uses_last_positive_week_after_interruption() -> None:
    candidates = list(_weekly_candidates(6))
    first_easy = next(item for item in candidates[0].sessions if item.role == "easy_run")
    candidates[0] = replace(
        candidates[0],
        sessions=(replace(first_easy, planned_minutes=30),),
        target_days=1,
    )

    cycle = compose_training_cycle(
        tuple(candidates),
        start_date=START,
        target_date=START + timedelta(weeks=5, days=6),
        event_type="10k",
        interrupted_weeks=frozenset({1}),
    )

    assert cycle.validation_report["valid"] is False
    checks = cycle.validation_report["checks"]
    assert isinstance(checks, list)
    progression = next(
        check
        for check in checks
        if isinstance(check, dict) and check.get("code") == "cycle.weekly_volume_progression"
    )
    assert progression["result"] == "fail"


def test_cycle_handles_low_confidence_without_wearable_metrics() -> None:
    candidates = []
    for weekly in _weekly_candidates(6):
        context = dict(weekly.generation_context)
        baseline_value = context.get("baseline")
        assert isinstance(baseline_value, dict)
        baseline = dict(baseline_value)
        baseline["confidence"] = "low"
        context["baseline"] = baseline
        context["intensity"] = {"mode": "rpe_talk_test", "confidence": "low"}
        candidates.append(replace(weekly, generation_context=context))

    cycle = compose_training_cycle(
        tuple(candidates),
        start_date=START,
        target_date=START + timedelta(weeks=5, days=6),
        event_type="5k",
    )

    assert cycle.confidence == "low"
    assert cycle.validation_report["valid"] is True
    assert all("wearable" not in week.weekly_plan.generation_context for week in cycle.weeks)


def test_cycle_rejects_excess_quality_density() -> None:
    candidates = list(_weekly_candidates())
    first = candidates[0]
    candidates[0] = replace(
        first,
        sessions=tuple(
            replace(item, intensity_domain="high") if index < 2 else item
            for index, item in enumerate(first.sessions)
        ),
    )

    cycle = compose_training_cycle(
        tuple(candidates),
        start_date=START,
        target_date=START + timedelta(weeks=7, days=6),
        event_type="half_marathon",
    )

    assert cycle.validation_report["valid"] is False
    checks = cycle.validation_report["checks"]
    assert isinstance(checks, list)
    quality_check = next(
        check
        for check in checks
        if isinstance(check, dict) and check.get("code") == "cycle.long_run_and_quality_density"
    )
    assert quality_check["result"] == "fail"


def test_cycle_rejects_unrealistic_horizons_and_incomplete_inputs() -> None:
    with pytest.raises(MultiweekPlannerError) as too_short:
        compose_training_cycle(
            _weekly_candidates(4),
            start_date=START,
            target_date=START + timedelta(weeks=3, days=6),
            event_type="half_marathon",
        )
    assert too_short.value.code == "cycle.goal_horizon_too_short"

    with pytest.raises(MultiweekPlannerError) as incomplete:
        compose_training_cycle(
            _weekly_candidates(4),
            start_date=START,
            target_date=START + timedelta(weeks=7, days=6),
            event_type="half_marathon",
        )
    assert incomplete.value.code == "cycle.week_candidates_incomplete"


def test_cycle_rule_profiles_are_goal_specific() -> None:
    candidates = _weekly_candidates(12)
    total_minutes: dict[str, int] = {}
    for event_type in ("general_fitness", "5k", "10k", "half_marathon", "marathon"):
        cycle = compose_training_cycle(
            candidates,
            start_date=START,
            target_date=START + timedelta(weeks=11, days=6),
            event_type=event_type,
        )
        assert cycle.validation_report["valid"] is True
        minutes = cycle.impact["total_minutes"]
        assert isinstance(minutes, int)
        total_minutes[event_type] = minutes

    assert total_minutes["general_fitness"] < total_minutes["10k"]
    assert total_minutes["10k"] < total_minutes["marathon"]


def test_cycle_rejects_invalid_week_candidate() -> None:
    candidates = list(_weekly_candidates())
    candidates[3] = replace(
        candidates[3],
        validation_report={**candidates[3].validation_report, "valid": False},
    )

    cycle = compose_training_cycle(
        tuple(candidates),
        start_date=START,
        target_date=START + timedelta(weeks=7, days=6),
        event_type="half_marathon",
    )

    assert cycle.validation_report["valid"] is False
    checks = cycle.validation_report["checks"]
    assert isinstance(checks, list)
    assert (
        next(
            check
            for check in checks
            if isinstance(check, dict) and check.get("code") == "cycle.week_candidates_valid"
        )["result"]
        == "fail"
    )


def test_cycle_target_boundary_does_not_place_after_goal() -> None:
    target = START + timedelta(weeks=7, days=1)
    cycle = compose_training_cycle(
        _weekly_candidates(),
        start_date=START,
        target_date=target,
        event_type="half_marathon",
    )

    assert all(
        item.scheduled_for <= target for week in cycle.weeks for item in week.weekly_plan.sessions
    )


def test_cycle_requires_exact_active_goal_when_selected(session_factory) -> None:
    with session_factory() as session:
        user = _user(session)
        archived = AthleteGoal(
            user_id=user.id,
            event_type="10k",
            target_date=START + timedelta(weeks=7),
            status="archived",
        )
        session.add(archived)
        session.commit()
        target_date = archived.target_date
        assert target_date is not None

        with pytest.raises(MultiweekPlannerError) as archived_error:
            plan_training_cycle(
                session,
                user,
                start_date=START,
                target_date=target_date,
                goal_id=archived.id,
            )
        assert archived_error.value.code == "cycle.goal_not_found"

        with pytest.raises(MultiweekPlannerError) as missing_error:
            plan_training_cycle(
                session,
                user,
                start_date=START,
                target_date=START + timedelta(weeks=7),
                goal_id=999_999,
            )
        assert missing_error.value.code == "cycle.goal_not_found"


def test_persist_cycle_is_idempotent_and_keeps_revisions_immutable(session_factory) -> None:
    candidate = compose_training_cycle(
        _weekly_candidates(),
        start_date=START,
        target_date=START + timedelta(weeks=7, days=6),
        event_type="half_marathon",
    )
    with session_factory() as session:
        user = _user(session)
        goal = AthleteGoal(
            user_id=user.id,
            event_type="half_marathon",
            target_date=candidate.target_date,
        )
        session.add(goal)
        session.commit()
        candidate = replace(candidate, goal_id=goal.id)

        first = persist_training_cycle(session, user, candidate)
        second = persist_training_cycle(session, user, candidate)

        assert first.id == second.id
        assert session.scalar(select(func.count()).select_from(TrainingCycle)) == 1
        assert session.scalar(select(func.count()).select_from(TrainingCycleRevision)) == 1
        assert session.scalar(select(func.count()).select_from(TrainingCycleWeek)) == 8
        assert session.scalar(select(func.count()).select_from(Workout)) > 0

        with pytest.raises(ValueError, match="immutable"):
            first.confidence = "high"
            session.flush()
        session.rollback()

        membership = session.scalar(select(TrainingCycleWeek))
        assert membership is not None
        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(
                update(TrainingCycleWeek)
                .where(TrainingCycleWeek.id == membership.id)
                .values(phase="rewritten")
            )


def test_persist_cycle_replays_deferred_quality_templates(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "coach_deferred_quality_templates_enabled", True)
    candidate = compose_training_cycle(
        _weekly_candidates(enable_deferred_quality=True),
        start_date=START,
        target_date=START + timedelta(weeks=7, days=6),
        event_type="10k",
        enable_deferred_quality=True,
    )
    with session_factory() as session:
        user = _user(session)

        revision = persist_training_cycle(session, user, candidate)

        template_ids = set(
            session.scalars(
                select(WorkoutRevision.template_id).where(
                    WorkoutRevision.template_id.in_({"threshold_cruise", "vo2_intervals"})
                )
            )
        )
        assert template_ids == {"threshold_cruise", "vo2_intervals"}

        monkeypatch.setattr(get_settings(), "coach_deferred_quality_templates_enabled", False)
        with pytest.raises(TrainingCyclePersistenceError) as disabled:
            accept_training_cycle_revision(
                session,
                user,
                cycle_id=revision.cycle_id,
                revision_id=revision.id,
            )
        assert disabled.value.code == "cycle.deferred_quality_disabled"


def test_reactivating_cycle_revision_restores_weekly_revision_pointers(session_factory) -> None:
    first_candidate = compose_training_cycle(
        _weekly_candidates(),
        start_date=START,
        target_date=START + timedelta(weeks=7, days=6),
        event_type="half_marathon",
    )
    changed_weeks = list(_weekly_candidates())
    changed_week = changed_weeks[3]
    changed_sessions = tuple(
        replace(item, planned_minutes=item.planned_minutes - 5)
        if item.role == "easy_run" and item.planned_minutes > 30
        else item
        for item in changed_week.sessions
    )
    changed_weeks[3] = replace(
        changed_week,
        sessions=changed_sessions,
        input_fingerprint="c" * 64,
    )
    second_candidate = compose_training_cycle(
        tuple(changed_weeks),
        start_date=START,
        target_date=START + timedelta(weeks=7, days=6),
        event_type="half_marathon",
    )

    with session_factory() as session:
        user = _user(session)
        first = persist_training_cycle(session, user, first_candidate)
        second = persist_training_cycle(session, user, second_candidate)
        assert first.id != second.id

        restored = persist_training_cycle(session, user, first_candidate)

        assert restored.id == first.id
        weekly_revision_ids = set(
            session.scalars(
                select(TrainingCycleWeek.training_plan_revision_id).where(
                    TrainingCycleWeek.cycle_revision_id == first.id
                )
            )
        )
        current_revision_ids = set(
            session.scalars(
                select(TrainingPlan.current_revision_id)
                .join(TrainingPlanRevision, TrainingPlanRevision.plan_id == TrainingPlan.id)
                .where(TrainingPlanRevision.id.in_(weekly_revision_ids))
            )
        )
        assert current_revision_ids == weekly_revision_ids

        second_weekly_revision = session.scalar(
            select(TrainingPlanRevision)
            .join(
                TrainingCycleWeek,
                TrainingCycleWeek.training_plan_revision_id == TrainingPlanRevision.id,
            )
            .where(
                TrainingCycleWeek.cycle_revision_id == second.id,
                TrainingPlanRevision.id.not_in(weekly_revision_ids),
            )
        )
        assert second_weekly_revision is not None
        drifted_plan = session.get(TrainingPlan, second_weekly_revision.plan_id)
        assert drifted_plan is not None
        drifted_plan.current_revision_id = second_weekly_revision.id
        session.commit()

        persist_training_cycle(session, user, first_candidate)

        expected_revision = session.scalar(
            select(TrainingPlanRevision.id)
            .join(
                TrainingCycleWeek,
                TrainingCycleWeek.training_plan_revision_id == TrainingPlanRevision.id,
            )
            .where(
                TrainingCycleWeek.cycle_revision_id == first.id,
                TrainingPlanRevision.plan_id == drifted_plan.id,
            )
        )
        assert drifted_plan.current_revision_id == expected_revision


def test_referenced_goal_cannot_be_deleted_from_cycle(session_factory) -> None:
    candidate = compose_training_cycle(
        _weekly_candidates(),
        start_date=START,
        target_date=START + timedelta(weeks=7, days=6),
        event_type="half_marathon",
    )
    with session_factory() as session:
        user = _user(session)
        goal = AthleteGoal(
            user_id=user.id,
            event_type="half_marathon",
            target_date=candidate.target_date,
        )
        session.add(goal)
        session.commit()
        persist_training_cycle(session, user, replace(candidate, goal_id=goal.id))

        with pytest.raises(IntegrityError):
            session.delete(goal)
            session.commit()


def test_cycle_revision_pointers_are_validated_on_insert(session_factory) -> None:
    with session_factory() as session:
        user = _user(session)
        session.add(
            TrainingCycle(
                user_id=user.id,
                event_type="10k",
                start_date=START,
                target_date=START + timedelta(weeks=7),
                current_revision_id=999_999,
            )
        )

        with pytest.raises(IntegrityError, match="Cycle revision must belong"):
            session.commit()


def test_cycle_delete_cascades_across_parent_revisions(session_factory) -> None:
    first_candidate = compose_training_cycle(
        _weekly_candidates(),
        start_date=START,
        target_date=START + timedelta(weeks=7, days=6),
        event_type="half_marathon",
    )
    second_candidate = replace(first_candidate, input_fingerprint="d" * 64)
    with session_factory() as session:
        user = _user(session)
        first = persist_training_cycle(session, user, first_candidate)
        second = persist_training_cycle(session, user, second_candidate)
        assert first.id != second.id
        cycle = session.get(TrainingCycle, first.cycle_id)
        assert cycle is not None

        session.delete(cycle)
        session.commit()

        assert session.scalar(select(func.count()).select_from(TrainingCycleRevision)) == 0
        assert session.scalar(select(func.count()).select_from(TrainingCycleWeek)) == 0


def test_cycle_acceptance_is_explicit_and_user_scoped(session_factory) -> None:
    candidate = compose_training_cycle(
        _weekly_candidates(),
        start_date=START,
        target_date=START + timedelta(weeks=7, days=6),
        event_type="half_marathon",
    )
    with session_factory() as session:
        user = _user(session)
        revision = persist_training_cycle(session, user, candidate)
        accepted = accept_training_cycle_revision(
            session, user, cycle_id=revision.cycle_id, revision_id=revision.id
        )
        cycle = session.get(TrainingCycle, revision.cycle_id)
        assert accepted.id == revision.id
        assert cycle is not None
        assert cycle.accepted_revision_id == revision.id
        assert all(
            workout.approval_status == "proposed" for workout in session.scalars(select(Workout))
        )

        other = _user(session)
        with pytest.raises(TrainingCyclePersistenceError) as not_found:
            accept_training_cycle_revision(
                session, other, cycle_id=revision.cycle_id, revision_id=revision.id
            )
        assert not_found.value.code == "cycle.not_found"


def test_current_plans_and_cycle_revisions_are_user_scoped(session_factory) -> None:
    candidate = compose_training_cycle(
        _weekly_candidates(),
        start_date=START,
        target_date=START + timedelta(weeks=7, days=6),
        event_type="half_marathon",
    )
    with session_factory() as session:
        user = _user(session)
        other = _user(session)
        revision = persist_training_cycle(session, user, candidate)

        current = get_current_training_cycle(session, user.id, revision.cycle_id)
        assert current is not None
        assert current.cycle.id == revision.cycle_id
        assert current.revision.id == revision.id
        assert get_current_training_cycle(session, other.id, revision.cycle_id) is None
        assert get_accepted_training_cycle(session, user.id, revision.cycle_id) is None
        assert [cycle.id for cycle in list_training_cycles(session, user.id)] == [revision.cycle_id]
        assert list_training_cycles(session, other.id) == ()
        assert [
            week.membership.position
            for week in list_training_cycle_week_details(session, user.id, revision.id)
        ] == list(range(8))
        assert list_training_cycle_week_details(session, other.id, revision.id) == ()

        plans = list_current_training_plans(
            session,
            user.id,
            starts_on=candidate.start_date,
            ends_on=candidate.target_date,
        )
        assert [plan.week_start for plan in plans] == [
            START + timedelta(weeks=offset) for offset in range(8)
        ]
        assert [
            plan.week_start
            for plan in list_current_training_plans(
                session,
                user.id,
                starts_on=START + timedelta(weeks=3),
                ends_on=START + timedelta(weeks=3),
            )
        ] == [START + timedelta(weeks=3)]
        assert (
            list_current_training_plans(
                session,
                other.id,
                starts_on=candidate.start_date,
                ends_on=candidate.target_date,
            )
            == ()
        )

        accept_training_cycle_revision(
            session,
            user,
            cycle_id=revision.cycle_id,
            revision_id=revision.id,
        )

        accepted = get_accepted_training_cycle(session, user.id, revision.cycle_id)
        assert accepted is not None
        assert accepted.revision.id == revision.id
        assert [item.revision.id for item in list_accepted_training_cycles(session, user.id)] == [
            revision.id
        ]
        assert list_accepted_training_cycles(session, other.id) == ()
