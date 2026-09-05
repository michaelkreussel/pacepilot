from datetime import date, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import func, select

from app.models import (
    Activity,
    AthleteAvailability,
    GarminSyncState,
    PreSessionFeedback,
    User,
    Workout,
    WorkoutEvent,
    WorkoutRevision,
)
from app.services.planning.registry import get_knowledge_registry
from app.services.planning.weekly_planner import (
    DayAvailability,
    GoalSummary,
    WeeklyPlanCandidate,
    WeeklyPlannerError,
    WeeklyPlannerSnapshot,
    _assign_strides_day,
    _count_consistent_weeks,
    compose_week,
    plan_shadow_week,
)

ACTIVE_TEMPLATE_IDS = {"easy_run", "long_run", "strides"}
MONDAY = date(2026, 8, 31)


def _snapshot(**overrides) -> WeeklyPlannerSnapshot:
    values: dict[str, Any] = {
        "week_start": MONDAY,
        "as_of": date(2026, 8, 26),
        "availability": (
            DayAvailability(weekday=0, available_minutes=60),
            DayAvailability(weekday=2, available_minutes=75),
            DayAvailability(weekday=5, available_minutes=120),
            DayAvailability(weekday=6, available_minutes=90),
        ),
        "preferred_long_run_weekday": None,
        "experience_level": "intermediate",
        "effective_reentry": False,
        "goals": (GoalSummary(event_type="10k", status="active", target_date=None),),
        "baseline_confidence": "medium",
        "typical_weekly_runs_median": 3.0,
        "observed_runs_per_week": 3.0,
        "consistent_running_weeks": 4,
        "longest_run_28d_seconds": 4800.0,
        "typical_longest_run_seconds": 4200.0,
        "median_run_seconds": 2700.0,
        "hard_runs_28d": 2,
        "intensity_mode": "rpe_talk_test",
        "intensity_confidence": "medium",
        "baseline_fingerprint": "b" * 64,
        "intensity_fingerprint": "i" * 64,
        "knowledge_base_version": get_knowledge_registry().version,
    }
    values.update(overrides)
    return WeeklyPlannerSnapshot(**values)


def test_planner_places_only_active_supported_templates() -> None:
    candidate = compose_week(_snapshot())
    registry = get_knowledge_registry()
    assert {session.template_id for session in candidate.sessions} <= ACTIVE_TEMPLATE_IDS
    for session in candidate.sessions:
        template = registry.workouts[session.template_id]
        assert template.status == "active"


def test_sessions_respect_availability_and_budgets() -> None:
    snapshot = _snapshot(preferred_long_run_weekday=6)
    candidate = compose_week(snapshot)
    budgets = {day.weekday: day.available_minutes for day in snapshot.availability}
    assert {session.weekday for session in candidate.sessions} <= set(budgets)
    for session in candidate.sessions:
        assert session.planned_minutes <= budgets[session.weekday]
    long_runs = [session for session in candidate.sessions if session.role == "long_run"]
    assert len(long_runs) == 1
    assert long_runs[0].weekday == 6


def test_target_frequency_is_capped_by_baseline_and_availability() -> None:
    wide_availability = tuple(
        DayAvailability(weekday=day, available_minutes=120) for day in range(6)
    )
    wide = _snapshot(
        availability=wide_availability,
        typical_weekly_runs_median=3.4,
        observed_runs_per_week=3.4,
    )
    assert len(compose_week(wide).sessions) == 3
    low_confidence = _snapshot(
        availability=wide_availability,
        typical_weekly_runs_median=5.0,
        observed_runs_per_week=5.0,
        baseline_confidence="low",
    )
    assert len(compose_week(low_confidence).sessions) == 3
    reentry = _snapshot(
        availability=wide_availability,
        typical_weekly_runs_median=5.0,
        observed_runs_per_week=5.0,
        effective_reentry=True,
    )
    assert len(compose_week(reentry).sessions) == 3
    growing = _snapshot(
        availability=wide_availability,
        typical_weekly_runs_median=5.0,
        observed_runs_per_week=5.0,
    )
    assert len(compose_week(growing).sessions) == 5


def test_long_run_requires_consistent_running_weeks() -> None:
    candidate = compose_week(_snapshot(consistent_running_weeks=3))
    assert all(session.role != "long_run" for session in candidate.sessions)
    assert len(candidate.sessions) == candidate.target_days


def test_sparse_history_produces_advisory_draft_with_conservative_scope() -> None:
    candidate = compose_week(
        _snapshot(
            typical_weekly_runs_median=1.0,
            observed_runs_per_week=1.0,
            consistent_running_weeks=1,
        ),
        enforce_history_gates=False,
    )

    assert candidate.target_days == 2
    assert len(candidate.sessions) == 2
    raw_advisory = candidate.generation_context.get("advisory")
    assert isinstance(raw_advisory, dict)
    assert raw_advisory["confidence"] == "low"
    raw_warnings = raw_advisory.get("warnings")
    assert isinstance(raw_warnings, list)
    assert "planner.weekly_frequency_low" in raw_warnings
    assert "planner.consistent_weeks_sparse" in raw_warnings
    assert raw_advisory["recommendation"]
    assert raw_advisory["alternative"]
    raw_evidence = raw_advisory.get("evidence")
    assert isinstance(raw_evidence, list)
    assert any(
        isinstance(item, dict) and item.get("observed_on") == "2026-08-26" for item in raw_evidence
    )
    assert raw_advisory["coverage"]
    raw_gates = candidate.generation_context.get("history_gates")
    assert isinstance(raw_gates, dict)
    assert raw_gates["mode"] == "advisory"
    checks = candidate.validation_report["checks"]
    assert isinstance(checks, list)
    assert {
        check["result"]
        for check in checks
        if isinstance(check, dict) and check.get("code") == "planner.history_gates"
    } == {"advisory"}


def test_consistent_history_counter_reaches_eight_weeks(session_factory) -> None:
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Eight Week Runner")
        session.add(user)
        session.flush()
        for week in range(8):
            for run in range(2):
                age = week * 7 + run
                session.add(
                    Activity(
                        user_id=user.id,
                        garmin_activity_id=f"eight-week-{week}-{run}",
                        name="Run",
                        activity_type="running",
                        started_at=datetime.combine(
                            today - timedelta(days=age), datetime.min.time()
                        ),
                        duration_s=2400,
                        distance_m=6000,
                    )
                )
        session.flush()

        assert _count_consistent_weeks(session, user.id, today) == 8


def test_long_run_is_bounded_by_recent_longest_run() -> None:
    bounded = compose_week(_snapshot(longest_run_28d_seconds=3600.0))
    long_run = next(s for s in bounded.sessions if s.role == "long_run")
    assert long_run.planned_minutes <= 66

    growth_warning = compose_week(
        _snapshot(longest_run_28d_seconds=5400.0, typical_longest_run_seconds=4200.0)
    )
    warned = next(s for s in growth_warning.sessions if s.role == "long_run")
    assert warned.planned_minutes == 95
    assert "planner.long_run_above_typical_weekly_longest" in warned.warnings

    too_short = compose_week(_snapshot(longest_run_28d_seconds=3000.0))
    assert all(session.role != "long_run" for session in too_short.sessions)
    assert any(
        skip.reason_code == "planner.long_run_below_template_minimum_after_history_bound"
        for skip in too_short.skipped_days
    )


def test_quality_sessions_keep_minimum_spacing() -> None:
    assigned, skips = _assign_strides_day(
        (
            DayAvailability(weekday=0, available_minutes=60),
            DayAvailability(weekday=1, available_minutes=60),
            DayAvailability(weekday=2, available_minutes=60),
        ),
        required_minutes=41,
        taken={0},
    )
    assert skips.get(1) == "planner.quality_spacing_violation"
    assert assigned == 2


def test_strides_require_hard_running_exposure_and_consistent_weeks() -> None:
    no_exposure = compose_week(_snapshot(hard_runs_28d=0))
    assert all(session.role != "strides" for session in no_exposure.sessions)
    inconsistent = compose_week(
        _snapshot(hard_runs_28d=2, consistent_running_weeks=2, typical_weekly_runs_median=5.0)
    )
    assert all(session.role != "strides" for session in inconsistent.sessions)


def test_plan_never_stacks_missed_sessions() -> None:
    candidate = compose_week(_snapshot())
    weekdays = [session.weekday for session in candidate.sessions]
    assert len(weekdays) == len(set(weekdays))
    assert len(candidate.sessions) <= candidate.target_days


def test_tiny_budget_day_is_skipped_with_reason() -> None:
    candidate = compose_week(
        _snapshot(
            availability=(
                DayAvailability(weekday=0, available_minutes=15),
                DayAvailability(weekday=5, available_minutes=120),
                DayAvailability(weekday=6, available_minutes=90),
            ),
            typical_weekly_runs_median=3.0,
        )
    )
    assert all(session.weekday != 0 for session in candidate.sessions)
    assert any(
        skip.weekday == 0 and skip.reason_code == "planner.budget_below_easy_minimum"
        for skip in candidate.skipped_days
    )


def test_skipped_days_never_contain_placed_weekdays() -> None:
    candidate = compose_week(_snapshot())
    placed = {session.weekday for session in candidate.sessions}
    skipped = {skip.weekday for skip in candidate.skipped_days}
    assert placed.isdisjoint(skipped)


def test_identical_inputs_produce_identical_candidates() -> None:
    first = compose_week(_snapshot())
    second = compose_week(_snapshot())
    assert first == second
    assert first.input_fingerprint == second.input_fingerprint


def test_sparse_baseline_produces_advisory_draft_and_only_availability_blocks() -> None:
    insufficient = compose_week(_snapshot(baseline_confidence="insufficient"))
    assert insufficient.sessions
    raw_insufficient = insufficient.generation_context.get("advisory")
    assert isinstance(raw_insufficient, dict)
    assert raw_insufficient["confidence"] == "low"
    insufficient_warnings = raw_insufficient.get("warnings")
    assert isinstance(insufficient_warnings, list)
    assert "planner.baseline_confidence_insufficient" in insufficient_warnings
    sparse = compose_week(_snapshot(typical_weekly_runs_median=1.0))
    assert sparse.sessions
    raw_sparse = sparse.generation_context.get("advisory")
    assert isinstance(raw_sparse, dict)
    sparse_warnings = raw_sparse.get("warnings")
    assert isinstance(sparse_warnings, list)
    assert "planner.weekly_frequency_low" in sparse_warnings
    with pytest.raises(WeeklyPlannerError) as availability_error:
        compose_week(_snapshot(availability=()))
    assert availability_error.value.code == "planner.no_available_days"


def test_health_concern_produces_advisory_draft_instead_of_blocking(session_factory) -> None:
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Blocked Runner")
        session.add(user)
        session.flush()
        for index, age in enumerate((0, 7, 14, 21)):
            session.add(
                Activity(
                    user_id=user.id,
                    garmin_activity_id=f"blocked-{index}",
                    name=f"Run {index}",
                    activity_type="running",
                    started_at=datetime.combine(today - timedelta(days=age), datetime.min.time()),
                    duration_s=2400,
                    distance_m=6000,
                    workout_rpe=3,
                )
            )
        session.add(
            GarminSyncState(
                user_id=user.id,
                resource="activities",
                status="ok",
                backfill_complete=True,
                oldest_synced_date=today - timedelta(days=365),
                newest_synced_date=today,
            )
        )
        session.add(
            PreSessionFeedback(
                user_id=user.id,
                illness_signal="fever",
                pain_present=False,
                content_hash="c" * 64,
            )
        )
        session.add_all(
            [
                AthleteAvailability(
                    user_id=user.id, weekday=0, available=True, available_minutes=60
                ),
                AthleteAvailability(
                    user_id=user.id, weekday=2, available=True, available_minutes=75
                ),
            ]
        )
        session.commit()

        candidate = plan_shadow_week(session, user, week_start=MONDAY, as_of=today)

        assert candidate.sessions
        raw_health = candidate.generation_context.get("advisory")
        assert isinstance(raw_health, dict)
        assert raw_health["warnings"]
        assert raw_health["evidence"]
        assert raw_health["coverage"]
        assert raw_health["recommendation"]
        assert raw_health["alternative"]
        raw_fit = raw_health.get("training_fit")
        assert isinstance(raw_fit, dict)
        assert raw_fit["policy_version"]
        assert raw_fit["authoritative_input_fingerprint"]

        with pytest.raises(WeeklyPlannerError) as invalid_week:
            plan_shadow_week(session, user, week_start=date(2026, 9, 1), as_of=today)
        assert invalid_week.value.code == "planner.week_start_invalid"


def test_supplied_availability_is_validated(session_factory) -> None:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    with session_factory() as session:
        user = User(display_name="Invalid Availability Runner")
        session.add(user)
        session.flush()
        session.commit()

        with pytest.raises(WeeklyPlannerError) as invalid:
            plan_shadow_week(
                session,
                user,
                week_start=monday,
                as_of=today,
                availability=(DayAvailability(weekday=9, available_minutes=60),),
            )
        assert invalid.value.code == "planner.availability_invalid"


def test_supplied_availability_slot_generates_direct_draft(session_factory) -> None:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    with session_factory() as session:
        user = User(display_name="Supplied Availability Runner")
        session.add(user)
        session.flush()
        session.commit()

        candidate = plan_shadow_week(
            session,
            user,
            week_start=monday,
            as_of=today,
            availability=(DayAvailability(weekday=1, available_minutes=60),),
        )

        assert candidate.sessions
        raw_supplied = candidate.generation_context.get("advisory")
        assert isinstance(raw_supplied, dict)
        assert raw_supplied["confidence"]
        assert {session.weekday for session in candidate.sessions} == {1}


def test_planning_writes_no_rows(session_factory) -> None:
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Shadow Runner")
        session.add(user)
        session.flush()
        for index, age in enumerate((0, 1, 3, 8, 9, 10, 15, 16, 17, 22, 23, 24)):
            session.add(
                Activity(
                    user_id=user.id,
                    garmin_activity_id=f"shadow-{index}",
                    name=f"Run {index}",
                    activity_type="running",
                    started_at=datetime.combine(today - timedelta(days=age), datetime.min.time()),
                    duration_s=2700 + index * 60,
                    distance_m=6000,
                    workout_rpe=3 if index % 2 else 8,
                )
            )
        session.add(
            GarminSyncState(
                user_id=user.id,
                resource="activities",
                status="ok",
                backfill_complete=True,
                oldest_synced_date=today - timedelta(days=365),
                newest_synced_date=today,
            )
        )
        session.add_all(
            [
                AthleteAvailability(
                    user_id=user.id, weekday=0, available=True, available_minutes=60
                ),
                AthleteAvailability(
                    user_id=user.id, weekday=2, available=True, available_minutes=75
                ),
                AthleteAvailability(
                    user_id=user.id, weekday=5, available=True, available_minutes=120
                ),
                AthleteAvailability(
                    user_id=user.id, weekday=6, available=True, available_minutes=90
                ),
            ]
        )
        session.commit()

        def counts() -> tuple[int, int, int]:
            return (
                session.scalar(select(func.count()).select_from(Workout)),
                session.scalar(select(func.count()).select_from(WorkoutRevision)),
                session.scalar(select(func.count()).select_from(WorkoutEvent)),
            )

        before = counts()
        monday = today - timedelta(days=today.weekday())
        candidate = plan_shadow_week(session, user, week_start=monday, as_of=today)
        session.commit()
        after = counts()

        assert before == after
        assert isinstance(candidate, WeeklyPlanCandidate)
        assert candidate.validation_report["valid"] is True
        assert candidate.generation_context["as_of"] == today.isoformat()


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=6),
            st.integers(min_value=10, max_value=180),
        ),
        min_size=1,
        max_size=7,
        unique_by=lambda item: item[0],
    ),
    st.sampled_from(["medium", "high"]),
    st.integers(min_value=2, max_value=6),
    st.integers(min_value=0, max_value=4),
    st.one_of(st.none(), st.integers(min_value=3300, max_value=7200)),
    st.booleans(),
)
def test_property_plan_respects_hard_invariants(
    raw_availability,
    baseline_confidence,
    median_runs,
    consistent_weeks,
    longest_seconds,
    reentry,
) -> None:
    availability = tuple(
        DayAvailability(weekday=weekday, available_minutes=budget)
        for weekday, budget in sorted(raw_availability)
    )
    snapshot = _snapshot(
        availability=availability,
        baseline_confidence=baseline_confidence,
        typical_weekly_runs_median=float(median_runs),
        observed_runs_per_week=float(median_runs),
        consistent_running_weeks=consistent_weeks,
        longest_run_28d_seconds=float(longest_seconds) if longest_seconds else None,
        effective_reentry=reentry,
    )
    try:
        first = compose_week(snapshot)
    except WeeklyPlannerError:
        return
    assert compose_week(snapshot) == first

    budgets = {day.weekday: day.available_minutes for day in availability}
    quality_weekdays = [
        session.weekday for session in first.sessions if session.intensity_domain != "low"
    ]
    long_runs = [session for session in first.sessions if session.role == "long_run"]

    for session in first.sessions:
        assert session.weekday in budgets
        assert session.planned_minutes <= budgets[session.weekday]
    weekdays = [session.weekday for session in first.sessions]
    assert len(weekdays) == len(set(weekdays))
    assert len(first.sessions) <= first.target_days
    skipped = {skip.weekday for skip in first.skipped_days}
    assert set(weekdays).isdisjoint(skipped)
    for index, weekday in enumerate(quality_weekdays):
        for other in quality_weekdays[index + 1 :]:
            assert abs(other - weekday) * 24 >= 48
    for session in long_runs:
        assert snapshot.longest_run_28d_seconds is not None
        bound = int(snapshot.longest_run_28d_seconds * 1.1 / 60) // 5 * 5
        assert session.planned_minutes <= min(bound, 120)
    assert {session.template_id for session in first.sessions} <= ACTIVE_TEMPLATE_IDS
