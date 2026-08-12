from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from app.services.analytics.automatic_profile import (
    EmpiricalTrainingRange,
    LongestRun,
    WeeklyCapacity,
)
from app.services.analytics.performance_state import (
    AutomaticPerformanceState,
    EnduranceBase,
    HabitualLoad,
    ObservedTrainingTolerance,
    PerformanceDataQuality,
    PerformanceTrend,
    TrendDirection,
)
from app.services.planning.planning_limits import derive_running_planning_limits


def _trend(direction: TrendDirection = "stable") -> PerformanceTrend:
    return PerformanceTrend(
        horizon_weeks=4,
        covered=True,
        earlier_weekly_distance_m=40_000,
        recent_weekly_distance_m=40_000,
        distance_change_percent=0,
        distance_direction=direction,
        earlier_weekly_duration_s=14_400,
        recent_weekly_duration_s=14_400,
        duration_direction="stable",
        earlier_sessions_per_week=4,
        recent_sessions_per_week=4,
        sessions_direction="stable",
        earlier_threshold_pace_s_per_km=None,
        recent_threshold_pace_s_per_km=None,
        threshold_pace_direction="unknown",
    )


def _state(
    *,
    quality: Literal["high", "medium", "low"] = "high",
    distance_coverage: float = 100,
    strain_coverage: float = 100,
    habitual_hard: float = 1.5,
    position: Literal["below_usual", "usual", "above_usual", "unknown"] = "usual",
    trend_direction: TrendDirection = "stable",
) -> AutomaticPerformanceState:
    return AutomaticPerformanceState(
        schema_version="automatic-performance-state.v1",
        formula_version="running-performance-state.v1",
        as_of=date(2026, 8, 12),
        sport="running",
        capabilities=(),
        endurance_base=EnduranceBase(True, 40_000, 4, 4, 20_000, 80, None),
        habitual_load=HabitualLoad(
            True,
            40_000,
            40_000,
            14_400,
            14_400,
            4,
            4,
            1.5,
            habitual_hard,
            1,
        ),
        trends=(_trend(trend_direction),),
        training_tolerance=ObservedTrainingTolerance(
            True,
            40_000,
            35_000,
            45_000,
            position,
            1.5,
            strain_coverage,
        ),
        strengths=(),
        development_gaps=(),
        data_gaps=(),
        data_quality=PerformanceDataQuality(
            quality,
            26,
            100,
            distance_coverage,
            100,
            strain_coverage,
            100,
            100,
            (),
        ),
    )


def _recovery(*, score: float | None = None, confidence: float = 0) -> Any:
    return SimpleNamespace(
        health_day=date(2026, 8, 12) if score is not None else None,
        pacepilot_readiness_score=score,
        pacepilot_readiness_confidence=confidence,
    )


def _limits(**overrides):
    arguments = {
        "as_of": date(2026, 8, 12),
        "sport": "running",
        "performance_state": _state(),
        "weekly_capacity": WeeklyCapacity(12, True, 40_000, 35_000, 45_000, 4, 4),
        "longest_runs": (LongestRun("road", 20_000, 7_200, 1, date(2026, 8, 1)),),
        "training_ranges": (),
        "recovery": _recovery(),
        "availability": ((0, 60), (2, 90), (5, 150)),
        "constraint_note": None,
        "constraint_until": None,
        "goal_sport": None,
        "goal_date": None,
        "goal_distance_m": None,
        "threshold_pace_s_per_km": 250,
        "threshold_freshness": "current",
        "heart_rate_zones": (),
        "week_start": date(2026, 8, 10),
        "plan_start": date(2026, 8, 10),
        "stratum": "road",
    }
    arguments.update(overrides)
    return derive_running_planning_limits(**arguments)


def test_planning_limits_allow_only_bounded_progression_with_strong_stable_evidence():
    limits = _limits()

    assert limits.status == "usable"
    assert limits.automatic_generation_allowed is True
    assert limits.weekly_volume.progression_max_percent == 5
    assert limits.weekly_volume.normal_ceiling_m == 44_000
    assert limits.weekly_volume.week_distance_max_m == 42_000
    assert limits.hard_sessions.max_sessions == 2
    assert limits.hard_sessions.minimum_spacing_days == 3
    assert limits.long_run.distance_max_m == 14_500
    assert limits.long_run.weekly_share_max_percent == 35
    assert limits.long_run.duration_max_s == 7_500


@pytest.mark.parametrize("coverage, usable", [(80.0, True), (79.9, False)])
def test_planning_limits_require_eighty_percent_distance_coverage(coverage, usable):
    limits = _limits(performance_state=_state(distance_coverage=coverage))

    assert (limits.weekly_volume.week_distance_max_m is not None) is usable
    assert limits.weekly_volume.confidence == ("high" if usable else "insufficient")


def test_planning_limits_disable_progression_when_recent_volume_is_not_stable():
    limits = _limits(performance_state=_state(trend_direction="higher"))

    assert limits.weekly_volume.progression_max_percent == 0
    assert limits.weekly_volume.week_distance_max_m == 40_000


@pytest.mark.parametrize(
    "habitual,sessions,days,expected",
    [(1.5, 4, 4, 2), (1.49, 4, 4, 1), (1.5, 3.99, 4, 1), (0, 4, 4, 0)],
)
def test_hard_session_cap_follows_observed_history(habitual, sessions, days, expected):
    state = _state(habitual_hard=habitual)
    capacity = WeeklyCapacity(12, True, 40_000, 35_000, 45_000, sessions, days)

    limits = _limits(performance_state=state, weekly_capacity=capacity)

    assert limits.hard_sessions.max_sessions == expected


def test_fourth_block_week_is_deload_and_reduces_hard_sessions():
    limits = _limits(
        week_start=date(2026, 8, 31),
        plan_start=date(2026, 8, 10),
    )

    assert limits.periodization.phase == "deload"
    assert limits.periodization.volume_multiplier == 0.75
    assert limits.weekly_volume.week_distance_max_m == 31_500
    assert limits.hard_sessions.max_sessions == 1


def test_low_frequency_long_run_uses_week_limit_instead_of_thirty_five_percent():
    capacity = WeeklyCapacity(12, True, 9_584.15, 4_500, 10_900, 1, 1)
    limits = _limits(
        weekly_capacity=capacity,
        longest_runs=(LongestRun("road", 10_846, 4_218, 1, date(2026, 5, 21)),),
        performance_state=replace(
            _state(),
            endurance_base=EnduranceBase(True, 9_584.15, 1, 1, 10_846, None, None),
            habitual_load=replace(
                _state().habitual_load,
                recent_weekly_distance_m=9_584.15,
                habitual_weekly_distance_m=7_586,
                recent_sessions_per_week=1,
                habitual_sessions_per_week=1,
            ),
            data_quality=replace(_state().data_quality, level="medium"),
        ),
    )

    assert limits.weekly_volume.week_distance_max_m == 9_500
    assert limits.long_run.weekly_share_max_percent == 100
    assert limits.long_run.distance_max_m == 9_500


def test_long_run_target_can_reuse_same_surface_empirical_easy_range():
    easy = EmpiricalTrainingRange("easy", "road", True, 330, 320, 345, 140, 8, 8, 240, "medium")

    limits = _limits(training_ranges=(easy,))

    long_run = next(item for item in limits.targets if item.key == "long_run")
    assert (long_run.pace_fast_s_per_km, long_run.pace_slow_s_per_km) == (320, 345)
    assert long_run.heart_rate_zone == 2
    assert long_run.source == "empirical_easy_fallback"
    assert long_run.confidence == "low"


def test_taper_replaces_deload_and_event_over_capacity_requires_review():
    limits = _limits(
        week_start=date(2026, 8, 31),
        plan_start=date(2026, 8, 10),
        goal_sport="running",
        goal_date=date(2026, 9, 6),
        goal_distance_m=42_195,
    )

    assert limits.periodization.phase == "event"
    assert limits.periodization.volume_multiplier == 0.55
    assert limits.status == "review_required"
    assert any(reason.code == "goal_exceeds_observed_week_limit" for reason in limits.reasons)


@pytest.mark.parametrize(
    "score, expected_multiplier, expected_hard",
    [(44.99, 0.75, 0), (45, 0.90, 1), (65, 1.0, 2)],
)
def test_recovery_can_only_reduce_limits(score, expected_multiplier, expected_hard):
    limits = _limits(recovery=_recovery(score=score, confidence=50))

    adjustment = next((item for item in limits.adjustments if item.kind == "recovery"), None)
    actual_multiplier = adjustment.volume_multiplier if adjustment else 1.0
    assert actual_multiplier == expected_multiplier
    assert limits.hard_sessions.max_sessions == expected_hard


def test_active_text_constraint_requires_review_without_interpreting_content():
    limits = _limits(
        constraint_note="Keine Bergsprints bis zur Kontrolle.",
        constraint_until=date(2026, 8, 10),
    )

    assert limits.status == "review_required"
    assert limits.automatic_generation_allowed is False
    assert any(item.kind == "constraint" for item in limits.adjustments)
    assert limits.weekly_volume.week_distance_max_m == 40_000


def test_empirical_target_has_priority_and_surfaces_stay_isolated():
    empirical = EmpiricalTrainingRange(
        "easy",
        "road",
        True,
        330,
        320,
        345,
        140,
        8,
        8,
        240,
        "medium",
    )
    road = _limits(training_ranges=(empirical,))
    trail = _limits(training_ranges=(empirical,), stratum="trail")

    road_easy = next(item for item in road.targets if item.key == "easy")
    trail_easy = next(item for item in trail.targets if item.key == "easy")
    assert (road_easy.pace_fast_s_per_km, road_easy.pace_slow_s_per_km) == (320, 345)
    assert road_easy.source == "empirical"
    assert trail_easy.pace_fast_s_per_km is None
    assert trail_easy.source == "unavailable"


def test_week_and_plan_start_must_be_mondays():
    with pytest.raises(ValueError, match="Mondays"):
        _limits(week_start=date(2026, 8, 11))
