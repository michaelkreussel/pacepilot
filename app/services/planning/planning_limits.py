from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from math import floor
from typing import Literal

from app.services.analytics.automatic_profile import (
    EmpiricalTrainingRange,
    LongestRun,
    WeeklyCapacity,
)
from app.services.analytics.health_trends import RecoveryState
from app.services.analytics.performance_state import AutomaticPerformanceState

Confidence = Literal["high", "medium", "low", "insufficient"]
LimitStatus = Literal["usable", "limited", "review_required", "insufficient", "unsupported"]
Stratum = Literal["road", "trail", "treadmill"]


@dataclass(frozen=True)
class PlanningLimitReason:
    code: str
    effect: Literal["supports", "limits", "blocks", "unknown"]
    observed_value: float | None = None
    unit: str | None = None


@dataclass(frozen=True)
class PlanningAvailabilityLimit:
    weekday: int
    max_duration_minutes: int


@dataclass(frozen=True)
class WeeklyVolumeLimit:
    sustainable_distance_m: float | None
    recent_distance_m: float | None
    normal_ceiling_m: float | None
    week_distance_max_m: float | None
    progression_max_percent: float | None
    scheduled_duration_max_s: int | None
    confidence: Confidence
    reasons: tuple[PlanningLimitReason, ...]


@dataclass(frozen=True)
class HardSessionLimit:
    observed_per_week: float | None
    max_sessions: int
    minimum_spacing_days: int
    avoid_adjacent_to_long_run: bool
    confidence: Confidence
    reasons: tuple[PlanningLimitReason, ...]


@dataclass(frozen=True)
class LongRunLimit:
    stratum: Stratum
    observed_distance_m: float | None
    observed_duration_s: float | None
    weekly_share_max_percent: float
    distance_max_m: float | None
    duration_max_s: float | None
    confidence: Confidence
    reasons: tuple[PlanningLimitReason, ...]


@dataclass(frozen=True)
class RunningTargetLimit:
    key: str
    stratum: Stratum
    pace_fast_s_per_km: float | None
    pace_slow_s_per_km: float | None
    heart_rate_min_bpm: float | None
    heart_rate_max_bpm: float | None
    heart_rate_zone: int | None
    source: Literal[
        "empirical",
        "empirical_easy_fallback",
        "threshold_fallback",
        "heart_rate_zones",
        "unavailable",
    ]
    confidence: Confidence


@dataclass(frozen=True)
class PeriodizationLimit:
    phase: Literal["build", "deload", "taper", "event"]
    block_week: int | None
    volume_multiplier: float
    hard_session_cap: int | None
    deload_every_weeks: int
    deload_volume_multiplier: float
    taper_weeks: int | None
    reasons: tuple[PlanningLimitReason, ...]


@dataclass(frozen=True)
class PlanningAdjustment:
    kind: Literal["recovery", "constraint"]
    volume_multiplier: float
    hard_session_cap: int | None
    review_required: bool
    reasons: tuple[PlanningLimitReason, ...]


@dataclass(frozen=True)
class RunningPlanningLimits:
    schema_version: str
    formula_version: str
    as_of: date
    week_start: date
    week_end: date
    sport: str
    stratum: Stratum
    status: LimitStatus
    automatic_generation_allowed: bool
    availability: tuple[PlanningAvailabilityLimit, ...]
    weekly_volume: WeeklyVolumeLimit
    hard_sessions: HardSessionLimit
    long_run: LongRunLimit
    targets: tuple[RunningTargetLimit, ...]
    periodization: PeriodizationLimit
    adjustments: tuple[PlanningAdjustment, ...]
    confidence: Confidence
    reasons: tuple[PlanningLimitReason, ...]


def _floor_to(value: float, step: int) -> float:
    return float(floor(value / step) * step)


def _taper_weeks(distance_m: float | None) -> int | None:
    if distance_m is None or not 3_000 <= distance_m <= 42_195:
        return None
    if distance_m <= 10_000:
        return 1
    if distance_m <= 21_097.5:
        return 2
    return 3


def _periodization(
    *,
    week_start: date,
    plan_start: date | None,
    goal_sport: str | None,
    goal_date: date | None,
    goal_distance_m: float | None,
) -> PeriodizationLimit:
    block_week = None
    if plan_start is not None and week_start >= plan_start:
        block_week = (week_start - plan_start).days // 7 + 1
    taper_weeks = _taper_weeks(goal_distance_m) if goal_sport == "running" else None
    if goal_date is not None and taper_weeks is not None:
        event_week = goal_date - timedelta(days=goal_date.weekday())
        weeks_before = (event_week - week_start).days // 7
        if 0 <= weeks_before < taper_weeks:
            multipliers = {
                1: (0.65,),
                2: (0.80, 0.60),
                3: (0.85, 0.70, 0.55),
            }[taper_weeks]
            index = taper_weeks - weeks_before - 1
            phase = "event" if weeks_before == 0 else "taper"
            return PeriodizationLimit(
                phase=phase,
                block_week=block_week,
                volume_multiplier=multipliers[index],
                hard_session_cap=1,
                deload_every_weeks=4,
                deload_volume_multiplier=0.75,
                taper_weeks=taper_weeks,
                reasons=(PlanningLimitReason("goal_taper", "limits"),),
            )
    if block_week is not None and block_week % 4 == 0:
        return PeriodizationLimit(
            phase="deload",
            block_week=block_week,
            volume_multiplier=0.75,
            hard_session_cap=None,
            deload_every_weeks=4,
            deload_volume_multiplier=0.75,
            taper_weeks=taper_weeks,
            reasons=(PlanningLimitReason("scheduled_deload", "limits"),),
        )
    return PeriodizationLimit(
        phase="build",
        block_week=block_week,
        volume_multiplier=1.0,
        hard_session_cap=None,
        deload_every_weeks=4,
        deload_volume_multiplier=0.75,
        taper_weeks=taper_weeks,
        reasons=(),
    )


def _recovery_adjustment(
    recovery: RecoveryState, as_of: date, week_start: date, week_end: date
) -> PlanningAdjustment | None:
    current_week = week_start <= as_of <= week_end
    fresh = recovery.health_day is not None and 0 <= (as_of - recovery.health_day).days <= 2
    trusted = recovery.pacepilot_readiness_confidence >= 50
    score = recovery.pacepilot_readiness_score
    if not current_week or not fresh or not trusted or score is None:
        return None
    if score < 45:
        return PlanningAdjustment(
            "recovery",
            0.75,
            0,
            False,
            (PlanningLimitReason("recovery_reduced_low", "limits", score, "score"),),
        )
    if score < 65:
        return PlanningAdjustment(
            "recovery",
            0.90,
            1,
            False,
            (PlanningLimitReason("recovery_reduced_fair", "limits", score, "score"),),
        )
    return None


def _constraint_adjustment(
    constraint_note: str | None, constraint_until: date | None, week_start: date
) -> PlanningAdjustment | None:
    if not constraint_note or (constraint_until is not None and constraint_until < week_start):
        return None
    return PlanningAdjustment(
        "constraint",
        1.0,
        None,
        True,
        (PlanningLimitReason("active_unstructured_constraint", "blocks"),),
    )


def _target_limits(
    *,
    stratum: Stratum,
    training_ranges: Sequence[EmpiricalTrainingRange],
    threshold_pace_s_per_km: float | None,
    threshold_freshness: str | None,
    heart_rate_zones: Sequence[tuple[int, float, float | None]],
) -> tuple[RunningTargetLimit, ...]:
    empirical = {
        item.key: item for item in training_ranges if item.stratum == stratum and item.sufficient
    }
    zones = {number: (lower, upper) for number, lower, upper in heart_rate_zones}
    fallback = {
        "easy": (1.15, 1.30),
        "tempo": (0.98, 1.05),
        "interval": (0.88, 0.98),
        "long_run": (1.15, 1.30),
    }
    hr_zone_map = {
        "easy": (2, 2),
        "tempo": (3, 3),
        "interval": (4, 4),
        "long_run": (2, 2),
    }
    results = []
    for key in fallback:
        item = empirical.get(key)
        pace_fast = item.pace_p25_s_per_km if item else None
        pace_slow = item.pace_p75_s_per_km if item else None
        source: Literal[
            "empirical",
            "empirical_easy_fallback",
            "threshold_fallback",
            "heart_rate_zones",
            "unavailable",
        ] = "empirical" if item else "unavailable"
        confidence: Confidence = item.confidence if item else "insufficient"
        if item is None and key == "long_run" and (easy := empirical.get("easy")) is not None:
            pace_fast = easy.pace_p25_s_per_km
            pace_slow = easy.pace_p75_s_per_km
            source = "empirical_easy_fallback"
            confidence = "low"
        if (
            item is None
            and pace_fast is None
            and stratum == "road"
            and threshold_pace_s_per_km is not None
            and threshold_freshness in {"current", "aging"}
        ):
            fast_factor, slow_factor = fallback[key]
            pace_fast = round(threshold_pace_s_per_km * fast_factor, 2)
            pace_slow = round(threshold_pace_s_per_km * slow_factor, 2)
            source = "threshold_fallback"
            confidence = "low"
        hr_min = None
        hr_max = None
        heart_rate_zone = hr_zone_map.get(key, (None, None))[0]
        if key in hr_zone_map:
            first_zone, last_zone = hr_zone_map[key]
            first = zones.get(first_zone)
            last = zones.get(last_zone)
            if first is not None and last is not None and last[1] is not None:
                hr_min = first[0]
                hr_max = last[1]
                if source == "unavailable":
                    source = "heart_rate_zones"
                    confidence = "low"
        results.append(
            RunningTargetLimit(
                key,
                stratum,
                pace_fast,
                pace_slow,
                hr_min,
                hr_max,
                heart_rate_zone,
                source,
                confidence,
            )
        )
    return tuple(results)


def derive_running_planning_limits(
    *,
    as_of: date,
    sport: str,
    performance_state: AutomaticPerformanceState,
    weekly_capacity: WeeklyCapacity,
    longest_runs: Sequence[LongestRun],
    training_ranges: Sequence[EmpiricalTrainingRange],
    recovery: RecoveryState,
    availability: Sequence[tuple[int, int]],
    constraint_note: str | None,
    constraint_until: date | None,
    goal_sport: str | None,
    goal_date: date | None,
    goal_distance_m: float | None,
    threshold_pace_s_per_km: float | None,
    threshold_freshness: str | None,
    heart_rate_zones: Sequence[tuple[int, float, float | None]],
    week_start: date | None = None,
    plan_start: date | None = None,
    stratum: Stratum = "road",
) -> RunningPlanningLimits:
    selected_week_start = week_start or (as_of - timedelta(days=as_of.weekday()))
    if selected_week_start.weekday() != 0 or (plan_start is not None and plan_start.weekday() != 0):
        raise ValueError("week_start and plan_start must be Mondays")
    week_end = selected_week_start + timedelta(days=6)
    availability_limits = tuple(
        PlanningAvailabilityLimit(weekday, minutes) for weekday, minutes in availability
    )
    available_duration_s = (
        sum(item.max_duration_minutes for item in availability_limits) * 60
        if availability_limits
        else None
    )
    periodization = _periodization(
        week_start=selected_week_start,
        plan_start=plan_start,
        goal_sport=goal_sport,
        goal_date=goal_date,
        goal_distance_m=goal_distance_m,
    )
    recovery_adjustment = _recovery_adjustment(recovery, as_of, selected_week_start, week_end)
    constraint_adjustment = _constraint_adjustment(
        constraint_note, constraint_until, selected_week_start
    )
    adjustments = tuple(
        item for item in (recovery_adjustment, constraint_adjustment) if item is not None
    )
    state = performance_state
    quality = state.data_quality
    volume_usable = (
        sport == "running"
        and weekly_capacity.covered
        and (weekly_capacity.sustainable_distance_m or 0) > 0
        and quality.covered_complete_weeks >= 12
        and (quality.distance_coverage_percent or 0) >= 80
    )
    sustainable = weekly_capacity.sustainable_distance_m if volume_usable else None
    recent = state.habitual_load.recent_weekly_distance_m if volume_usable else None
    four_week = next(item for item in state.trends if item.horizon_weeks == 4)
    progression_allowed = (
        volume_usable
        and quality.level == "high"
        and state.training_tolerance.distance_position == "usual"
        and four_week.distance_direction == "stable"
        and constraint_adjustment is None
    )
    reference = sustainable
    if (
        reference is not None
        and recent is not None
        and state.training_tolerance.distance_position == "below_usual"
    ):
        reference = min(reference, recent * 1.05)
    normal_ceiling = reference * (1.10 if progression_allowed else 1.0) if reference else None
    anchor = recent or reference
    build_limit = (
        min(normal_ceiling, anchor * 1.05)
        if progression_allowed and normal_ceiling is not None and anchor is not None
        else normal_ceiling
    )
    adjustment_multiplier = min(
        [periodization.volume_multiplier, *(item.volume_multiplier for item in adjustments)]
    )
    week_limit = (
        _floor_to(build_limit * adjustment_multiplier, 500) if build_limit is not None else None
    )
    volume_reasons = []
    if volume_usable:
        volume_reasons.append(
            PlanningLimitReason("weekly_capacity_12w", "supports", sustainable, "m/week")
        )
    else:
        volume_reasons.append(PlanningLimitReason("weekly_volume_evidence_insufficient", "unknown"))
    if progression_allowed:
        volume_reasons.append(PlanningLimitReason("progression_stable_history", "supports"))
    else:
        volume_reasons.append(PlanningLimitReason("progression_not_supported", "limits"))
    volume_confidence: Confidence = (
        "high"
        if volume_usable and quality.level == "high"
        else "medium"
        if volume_usable
        else "insufficient"
    )

    strain_coverage = quality.strain_coverage_percent
    habitual_hard = state.habitual_load.habitual_hard_sessions_per_week
    sessions = weekly_capacity.sessions_per_week_median
    active_days = weekly_capacity.active_days_per_week_median
    hard_trusted = volume_usable and (strain_coverage or 0) >= 80 and habitual_hard is not None
    base_hard_cap = 0
    if hard_trusted and habitual_hard > 0:
        base_hard_cap = 1
        if habitual_hard >= 1.5 and (sessions or 0) >= 4 and (active_days or 0) >= 4:
            base_hard_cap = 2
    hard_cap = base_hard_cap
    if periodization.phase == "deload":
        hard_cap = max(hard_cap - 1, 0)
    if periodization.hard_session_cap is not None:
        hard_cap = min(hard_cap, periodization.hard_session_cap)
    for adjustment in adjustments:
        if adjustment.hard_session_cap is not None:
            hard_cap = min(hard_cap, adjustment.hard_session_cap)
    hard_reasons = (
        (PlanningLimitReason("hard_session_history", "supports", habitual_hard, "sessions/week"),)
        if hard_trusted
        else (PlanningLimitReason("hard_session_strain_unknown", "limits"),)
    )
    hard_confidence: Confidence = (
        "high" if hard_trusted and quality.level == "high" else "medium" if hard_trusted else "low"
    )

    observed_long = next((item for item in longest_runs if item.stratum == stratum), None)
    long_run_share = (
        35.0
        if weekly_capacity.sessions_per_week_median is not None
        and weekly_capacity.sessions_per_week_median >= 3
        else 100.0
    )
    long_distance = (
        _floor_to(
            min(week_limit * long_run_share / 100, observed_long.distance_m * 1.05),
            500,
        )
        if week_limit is not None and observed_long is not None
        else None
    )
    max_available_duration = (
        max(item.max_duration_minutes for item in availability_limits) * 60
        if availability_limits
        else None
    )
    observed_duration_limit = (
        _floor_to(observed_long.duration_s * 1.05, 300)
        if observed_long is not None and observed_long.duration_s is not None
        else None
    )
    long_duration = (
        min(observed_duration_limit, max_available_duration)
        if observed_duration_limit is not None and max_available_duration is not None
        else observed_duration_limit
    )
    long_confidence: Confidence = (
        "high"
        if observed_long is not None and volume_confidence == "high"
        else "medium"
        if observed_long is not None and volume_usable
        else "insufficient"
    )
    long_reasons = (
        (PlanningLimitReason("long_run_observed", "supports", observed_long.distance_m, "m"),)
        if observed_long is not None
        else (PlanningLimitReason("long_run_history_missing", "unknown"),)
    )
    targets = _target_limits(
        stratum=stratum,
        training_ranges=training_ranges,
        threshold_pace_s_per_km=threshold_pace_s_per_km,
        threshold_freshness=threshold_freshness,
        heart_rate_zones=heart_rate_zones,
    )
    overall_reasons = [*periodization.reasons]
    review_required = constraint_adjustment is not None
    if (
        periodization.phase == "event"
        and goal_distance_m is not None
        and build_limit is not None
        and goal_distance_m > build_limit
    ):
        review_required = True
        overall_reasons.append(
            PlanningLimitReason("goal_exceeds_observed_week_limit", "blocks", goal_distance_m, "m")
        )
    if not availability_limits:
        overall_reasons.append(PlanningLimitReason("availability_missing", "blocks"))
    if sport != "running":
        status: LimitStatus = "unsupported"
    elif review_required:
        status = "review_required"
    elif not volume_usable:
        status = "insufficient"
    elif (
        not availability_limits
        or hard_cap == 0
        or any(item.confidence == "insufficient" for item in targets)
    ):
        status = "limited"
    else:
        status = "usable"
    automatic_allowed = (
        sport == "running" and volume_usable and bool(availability_limits) and not review_required
    )
    overall_confidence: Confidence = (
        "insufficient"
        if not volume_usable
        else "low"
        if hard_confidence == "low" or long_confidence == "insufficient"
        else "medium"
        if "medium" in {volume_confidence, hard_confidence, long_confidence}
        else "high"
    )
    return RunningPlanningLimits(
        schema_version="running-planning-limits.v1",
        formula_version="conservative-running-limits.v1",
        as_of=as_of,
        week_start=selected_week_start,
        week_end=week_end,
        sport=sport,
        stratum=stratum,
        status=status,
        automatic_generation_allowed=automatic_allowed,
        availability=availability_limits,
        weekly_volume=WeeklyVolumeLimit(
            sustainable,
            recent,
            _floor_to(normal_ceiling, 500) if normal_ceiling is not None else None,
            week_limit,
            5.0 if progression_allowed else 0.0 if volume_usable else None,
            available_duration_s,
            volume_confidence,
            tuple(volume_reasons),
        ),
        hard_sessions=HardSessionLimit(
            habitual_hard if hard_trusted else None,
            hard_cap,
            3,
            True,
            hard_confidence,
            hard_reasons,
        ),
        long_run=LongRunLimit(
            stratum,
            observed_long.distance_m if observed_long else None,
            observed_long.duration_s if observed_long else None,
            long_run_share,
            long_distance,
            long_duration,
            long_confidence,
            long_reasons,
        ),
        targets=targets,
        periodization=periodization,
        adjustments=adjustments,
        confidence=overall_confidence,
        reasons=tuple(overall_reasons),
    )
