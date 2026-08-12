from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median
from typing import TYPE_CHECKING, Literal

from app.models import Activity, DailyFitness
from app.services.garmin.activity_fit import fit_eligible_activity_type

if TYPE_CHECKING:
    from app.services.analytics.automatic_profile import (
        EmpiricalTrainingRange,
        IntensityDistribution,
        LongestRun,
        ObservedBestEffort,
        ProfileCoverage,
        WeeklyCapacity,
    )
    from app.services.analytics.detail_evidence import DetailEvidence

TrendDirection = Literal["higher", "stable", "lower", "unknown"]
FindingKind = Literal["strength", "development_gap", "data_gap"]


@dataclass(frozen=True)
class PerformanceCapability:
    key: str
    value: float
    unit: str
    source: str
    source_day: date | None
    freshness: Literal["current", "aging", "stale", "unknown"]
    confidence: Literal["reported", "high", "medium"]


@dataclass(frozen=True)
class EnduranceBase:
    covered: bool
    sustainable_weekly_distance_m: float | None
    sessions_per_week: float | None
    active_days_per_week: float | None
    longest_run_distance_m: float | None
    low_intensity_percent: float | None
    heart_rate_drift_percent: float | None


@dataclass(frozen=True)
class HabitualLoad:
    covered: bool
    recent_weekly_distance_m: float | None
    habitual_weekly_distance_m: float | None
    recent_weekly_duration_s: float | None
    habitual_weekly_duration_s: float | None
    recent_sessions_per_week: float | None
    habitual_sessions_per_week: float | None
    recent_hard_sessions_per_week: float | None
    habitual_hard_sessions_per_week: float | None
    distance_ratio: float | None


@dataclass(frozen=True)
class PerformanceTrend:
    horizon_weeks: Literal[4, 12, 26]
    covered: bool
    earlier_weekly_distance_m: float | None
    recent_weekly_distance_m: float | None
    distance_change_percent: float | None
    distance_direction: TrendDirection
    earlier_weekly_duration_s: float | None
    recent_weekly_duration_s: float | None
    duration_direction: TrendDirection
    earlier_sessions_per_week: float | None
    recent_sessions_per_week: float | None
    sessions_direction: TrendDirection
    earlier_threshold_pace_s_per_km: float | None
    recent_threshold_pace_s_per_km: float | None
    threshold_pace_direction: TrendDirection


@dataclass(frozen=True)
class ObservedTrainingTolerance:
    covered: bool
    recent_weekly_distance_m: float | None
    habitual_distance_p25_m: float | None
    habitual_distance_p75_m: float | None
    distance_position: Literal["below_usual", "usual", "above_usual", "unknown"]
    recent_hard_sessions_per_week: float | None
    strain_coverage_percent: float | None


@dataclass(frozen=True)
class PerformanceFinding:
    kind: FindingKind
    key: str
    observed_value: float | None
    unit: str | None


@dataclass(frozen=True)
class PerformanceDataQuality:
    level: Literal["high", "medium", "low"]
    covered_complete_weeks: int
    running_sessions: int
    distance_coverage_percent: float | None
    duration_coverage_percent: float | None
    strain_coverage_percent: float | None
    split_coverage_percent: float | None
    detail_coverage_percent: float | None
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class AutomaticPerformanceState:
    schema_version: str
    formula_version: str
    as_of: date
    sport: Literal["running"]
    capabilities: tuple[PerformanceCapability, ...]
    endurance_base: EnduranceBase
    habitual_load: HabitualLoad
    trends: tuple[PerformanceTrend, ...]
    training_tolerance: ObservedTrainingTolerance
    strengths: tuple[PerformanceFinding, ...]
    development_gaps: tuple[PerformanceFinding, ...]
    data_gaps: tuple[PerformanceFinding, ...]
    data_quality: PerformanceDataQuality


@dataclass(frozen=True)
class _WeeklyEvidence:
    week_start: date
    distance_m: float | None
    duration_s: float | None
    sessions: int
    active_days: int
    hard_sessions: int
    strain_observations: int


def _activity_duration(activity: Activity) -> float | None:
    return activity.duration_s or activity.elapsed_duration_s or activity.moving_duration_s


def _is_hard(activity: Activity) -> bool:
    return (
        (activity.aerobic_training_effect or 0) >= 3.5
        or (activity.anaerobic_training_effect or 0) >= 2.5
        or (activity.workout_rpe or 0) >= 7
    )


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _weekly_evidence(
    activities: list[Activity], as_of: date, weeks: int = 26
) -> tuple[_WeeklyEvidence, ...]:
    last_complete_sunday = as_of - timedelta(days=as_of.weekday() + 1)
    first_monday = last_complete_sunday - timedelta(weeks=weeks) + timedelta(days=1)
    points = []
    last_complete_sunday = as_of - timedelta(days=as_of.weekday() + 1)
    quality_start = last_complete_sunday - timedelta(weeks=26) + timedelta(days=1)
    running = [
        activity
        for activity in activities
        if fit_eligible_activity_type(activity.activity_type)
        and quality_start <= activity.started_at.date() <= last_complete_sunday
    ]
    for index in range(weeks):
        start = first_monday + timedelta(weeks=index)
        end = start + timedelta(days=6)
        selected = [activity for activity in running if start <= activity.started_at.date() <= end]
        distances = [activity.distance_m for activity in selected]
        durations = [_activity_duration(activity) for activity in selected]
        points.append(
            _WeeklyEvidence(
                week_start=start,
                distance_m=(
                    sum(float(value) for value in distances if value is not None)
                    if all(value is not None for value in distances)
                    else None
                ),
                duration_s=(
                    sum(float(value) for value in durations if value is not None)
                    if all(value is not None for value in durations)
                    else None
                ),
                sessions=len(selected),
                active_days=len({activity.started_at.date() for activity in selected}),
                hard_sessions=sum(_is_hard(activity) for activity in selected),
                strain_observations=sum(
                    activity.aerobic_training_effect is not None
                    or activity.anaerobic_training_effect is not None
                    or activity.workout_rpe is not None
                    for activity in selected
                ),
            )
        )
    return tuple(points)


def _weeks_covered(coverage: ProfileCoverage, as_of: date, maximum: int = 26) -> int:
    if (
        not coverage.history_complete
        or coverage.oldest_synced_date is None
        or coverage.newest_synced_date is None
    ):
        return 0
    last_complete_sunday = as_of - timedelta(days=as_of.weekday() + 1)
    if coverage.newest_synced_date < last_complete_sunday:
        return 0
    days = (last_complete_sunday - coverage.oldest_synced_date).days + 1
    return min(max(days // 7, 0), maximum)


def _direction(
    earlier: float | None,
    recent: float | None,
    *,
    relative_epsilon: float,
    absolute_epsilon: float,
) -> TrendDirection:
    if earlier is None or recent is None:
        return "unknown"
    epsilon = max(abs(earlier) * relative_epsilon, absolute_epsilon)
    change = recent - earlier
    if abs(change) <= epsilon:
        return "stable"
    return "higher" if change > 0 else "lower"


def _median_optional(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return float(median(present)) if len(present) == len(values) and present else None


def _trend(
    weekly: tuple[_WeeklyEvidence, ...],
    fitness: list[DailyFitness],
    coverage: ProfileCoverage,
    as_of: date,
    horizon: Literal[4, 12, 26],
) -> PerformanceTrend:
    covered = _weeks_covered(coverage, as_of) >= horizon
    selected = weekly[-horizon:]
    midpoint = horizon // 2
    earlier = selected[:midpoint]
    recent = selected[midpoint:]
    earlier_distance = (
        _median_optional([point.distance_m for point in earlier]) if covered else None
    )
    recent_distance = _median_optional([point.distance_m for point in recent]) if covered else None
    earlier_duration = (
        _median_optional([point.duration_s for point in earlier]) if covered else None
    )
    recent_duration = _median_optional([point.duration_s for point in recent]) if covered else None
    earlier_sessions = float(median(point.sessions for point in earlier)) if covered else None
    recent_sessions = float(median(point.sessions for point in recent)) if covered else None
    earlier_threshold = _fitness_at(
        fitness,
        "lactate_threshold_speed_mps",
        as_of - timedelta(weeks=horizon),
        max_age_days=180,
    )
    recent_threshold = _fitness_at(
        fitness,
        "lactate_threshold_speed_mps",
        as_of,
        max_age_days=180,
    )
    earlier_threshold_pace = 1_000 / earlier_threshold if earlier_threshold else None
    recent_threshold_pace = 1_000 / recent_threshold if recent_threshold else None
    change_percent = (
        round((recent_distance - earlier_distance) / earlier_distance * 100, 1)
        if earlier_distance is not None and earlier_distance > 0 and recent_distance is not None
        else None
    )
    return PerformanceTrend(
        horizon_weeks=horizon,
        covered=covered,
        earlier_weekly_distance_m=earlier_distance,
        recent_weekly_distance_m=recent_distance,
        distance_change_percent=change_percent,
        distance_direction=_direction(
            earlier_distance,
            recent_distance,
            relative_epsilon=0.05,
            absolute_epsilon=2_000,
        ),
        earlier_weekly_duration_s=earlier_duration,
        recent_weekly_duration_s=recent_duration,
        duration_direction=_direction(
            earlier_duration,
            recent_duration,
            relative_epsilon=0.05,
            absolute_epsilon=1_800,
        ),
        earlier_sessions_per_week=earlier_sessions,
        recent_sessions_per_week=recent_sessions,
        sessions_direction=_direction(
            earlier_sessions,
            recent_sessions,
            relative_epsilon=0.05,
            absolute_epsilon=0.5,
        ),
        earlier_threshold_pace_s_per_km=earlier_threshold_pace,
        recent_threshold_pace_s_per_km=recent_threshold_pace,
        threshold_pace_direction=_direction(
            earlier_threshold_pace,
            recent_threshold_pace,
            relative_epsilon=0.02,
            absolute_epsilon=5,
        ),
    )


def _latest_fitness(rows: list[DailyFitness], attribute: str) -> tuple[float | None, date | None]:
    for row in reversed(rows):
        value = getattr(row, attribute)
        if isinstance(value, (int, float)) and value > 0:
            return float(value), row.day
    return None, None


def _fitness_at(
    rows: list[DailyFitness], attribute: str, endpoint: date, *, max_age_days: int
) -> float | None:
    for row in reversed(rows):
        if row.day > endpoint:
            continue
        value = getattr(row, attribute)
        if isinstance(value, (int, float)) and value > 0:
            return float(value) if (endpoint - row.day).days <= max_age_days else None
    return None


def _freshness(source_day: date | None, as_of: date, current: int, aging: int):
    if source_day is None:
        return "unknown"
    age = (as_of - source_day).days
    return "current" if age <= current else "aging" if age <= aging else "stale"


def _capabilities(
    fitness: list[DailyFitness], best_efforts: tuple[ObservedBestEffort, ...], as_of: date
) -> tuple[PerformanceCapability, ...]:
    result = []
    for key, attribute, unit, transform in (
        ("vo2max", "vo2max", "ml/kg/min", lambda value: value),
        ("threshold_pace", "lactate_threshold_speed_mps", "s/km", lambda value: 1_000 / value),
        ("threshold_hr", "lactate_threshold_hr", "bpm", lambda value: value),
        ("running_threshold_power", "running_ftp_watts", "W", lambda value: value),
    ):
        value, source_day = _latest_fitness(fitness, attribute)
        if value is not None:
            result.append(
                PerformanceCapability(
                    key,
                    round(transform(value), 2),
                    unit,
                    "garmin_snapshot",
                    source_day,
                    _freshness(source_day, as_of, 90, 180),
                    "reported",
                )
            )
    for effort in best_efforts:
        result.append(
            PerformanceCapability(
                f"best_effort_{effort.distance_key}",
                effort.duration_s,
                "s",
                effort.source,
                effort.occurred_on,
                (
                    "unknown"
                    if effort.source == "garmin_personal_record"
                    else _freshness(effort.occurred_on, as_of, 84, 183)
                ),
                effort.confidence,
            )
        )
    return tuple(result)


def build_performance_state(
    *,
    activities: list[Activity],
    fitness: list[DailyFitness],
    as_of: date,
    coverage: ProfileCoverage,
    weekly_capacity: WeeklyCapacity,
    longest_runs: tuple[LongestRun, ...],
    intensity: IntensityDistribution,
    training_ranges: tuple[EmpiricalTrainingRange, ...],
    detail_evidence: DetailEvidence,
    best_efforts: tuple[ObservedBestEffort, ...],
) -> AutomaticPerformanceState:
    weekly = _weekly_evidence(activities, as_of)
    covered_weeks = _weeks_covered(coverage, as_of)
    recent = weekly[-4:]
    habitual = weekly[-12:-4]
    load_covered = covered_weeks >= 12
    recent_distance = (
        _median_optional([point.distance_m for point in recent]) if load_covered else None
    )
    habitual_distance = (
        _median_optional([point.distance_m for point in habitual]) if load_covered else None
    )
    recent_duration = (
        _median_optional([point.duration_s for point in recent]) if load_covered else None
    )
    habitual_duration = (
        _median_optional([point.duration_s for point in habitual]) if load_covered else None
    )
    recent_sessions = float(median(point.sessions for point in recent)) if load_covered else None
    habitual_sessions = (
        float(median(point.sessions for point in habitual)) if load_covered else None
    )
    recent_hard = float(median(point.hard_sessions for point in recent)) if load_covered else None
    habitual_hard = (
        float(median(point.hard_sessions for point in habitual)) if load_covered else None
    )
    habitual_distances = (
        [float(point.distance_m) for point in habitual if point.distance_m is not None]
        if load_covered and all(point.distance_m is not None for point in habitual)
        else []
    )
    habitual_p25 = _percentile(habitual_distances, 0.25)
    habitual_p75 = _percentile(habitual_distances, 0.75)
    distance_position: Literal["below_usual", "usual", "above_usual", "unknown"] = "unknown"
    if recent_distance is not None and habitual_p25 is not None and habitual_p75 is not None:
        distance_position = (
            "below_usual"
            if recent_distance < habitual_p25
            else "above_usual"
            if recent_distance > habitual_p75
            else "usual"
        )

    running = [
        activity for activity in activities if fit_eligible_activity_type(activity.activity_type)
    ]
    distances_present = sum(activity.distance_m is not None for activity in running)
    durations_present = sum(_activity_duration(activity) is not None for activity in running)
    strain_present = sum(
        activity.aerobic_training_effect is not None
        or activity.anaerobic_training_effect is not None
        or activity.workout_rpe is not None
        for activity in running
    )
    split_complete = sum(activity.splits_complete for activity in running)

    def coverage_percent(count: int) -> float | None:
        return round(count / len(running) * 100, 1) if running else None

    distance_coverage = coverage_percent(distances_present)
    duration_coverage = coverage_percent(durations_present)
    strain_coverage = coverage_percent(strain_present)
    split_coverage = coverage_percent(split_complete)
    detail_eligible = [
        activity
        for activity in running
        if (activity.distance_m or 0) >= 1_000 and (_activity_duration(activity) or 0) >= 600
    ]
    detail_available = sum(
        activity.fit_file is not None or activity.details_file is not None
        for activity in detail_eligible
    )
    detail_coverage = (
        round(detail_available / len(detail_eligible) * 100, 1) if detail_eligible else None
    )
    quality_level: Literal["high", "medium", "low"] = (
        "high"
        if covered_weeks >= 26 and (distance_coverage or 0) >= 95 and (duration_coverage or 0) >= 95
        else "medium"
        if covered_weeks >= 12 and (distance_coverage or 0) >= 80 and (duration_coverage or 0) >= 80
        else "low"
    )
    limitations = []
    if covered_weeks < 26:
        limitations.append("training_history_partial")
    if distance_coverage is None or distance_coverage < 95:
        limitations.append("distance_coverage_partial")
    if duration_coverage is None or duration_coverage < 95:
        limitations.append("duration_coverage_partial")
    if not intensity.sufficient:
        limitations.append("heart_rate_coverage_partial")
    if detail_coverage is None or detail_coverage < 70:
        limitations.append("detail_coverage_partial")

    strengths = []
    development_gaps = []
    data_gaps = []
    active_weeks = sum(point.sessions > 0 for point in weekly[-12:])
    active_days = float(median(point.active_days for point in weekly[-8:]))
    if covered_weeks >= 12 and active_weeks >= 10 and active_days >= 3:
        strengths.append(
            PerformanceFinding("strength", "training_consistency", active_weeks, "weeks")
        )
    elif covered_weeks >= 12 and active_weeks <= 6:
        development_gaps.append(
            PerformanceFinding("development_gap", "training_consistency", active_weeks, "weeks")
        )
    drift = detail_evidence.heart_rate_drift
    if drift.sufficient and drift.median_percent is not None:
        target = (
            strengths
            if drift.median_percent <= 5
            else development_gaps
            if drift.median_percent >= 8
            else None
        )
        if target is not None:
            target.append(
                PerformanceFinding(
                    "strength" if drift.median_percent <= 5 else "development_gap",
                    "aerobic_durability",
                    drift.median_percent,
                    "percent",
                )
            )
    else:
        data_gaps.append(PerformanceFinding("data_gap", "aerobic_durability_unknown", None, None))
    threshold_present = any(
        item.key in {"threshold_pace", "threshold_hr"} and item.freshness != "stale"
        for item in _capabilities(fitness, (), as_of)
    )
    tempo_present = any(item.key == "tempo" and item.sufficient for item in training_ranges)
    if not threshold_present and not tempo_present:
        data_gaps.append(PerformanceFinding("data_gap", "threshold_evidence_missing", None, None))
    if covered_weeks < 12:
        data_gaps.append(
            PerformanceFinding("data_gap", "training_history_partial", covered_weeks, "weeks")
        )
    if not weekly_capacity.covered:
        data_gaps.append(PerformanceFinding("data_gap", "weekly_capacity_unknown", None, None))
    if not intensity.sufficient:
        data_gaps.append(
            PerformanceFinding("data_gap", "heart_rate_coverage_insufficient", None, None)
        )

    road_longest = next((item for item in longest_runs if item.stratum == "road"), None)
    return AutomaticPerformanceState(
        schema_version="automatic-performance-state.v1",
        formula_version="running-performance-state.v1",
        as_of=as_of,
        sport="running",
        capabilities=_capabilities(fitness, best_efforts, as_of),
        endurance_base=EnduranceBase(
            covered=weekly_capacity.covered,
            sustainable_weekly_distance_m=weekly_capacity.sustainable_distance_m,
            sessions_per_week=weekly_capacity.sessions_per_week_median,
            active_days_per_week=weekly_capacity.active_days_per_week_median,
            longest_run_distance_m=road_longest.distance_m if road_longest else None,
            low_intensity_percent=intensity.low_percent if intensity.sufficient else None,
            heart_rate_drift_percent=drift.median_percent if drift.sufficient else None,
        ),
        habitual_load=HabitualLoad(
            covered=load_covered,
            recent_weekly_distance_m=recent_distance,
            habitual_weekly_distance_m=habitual_distance,
            recent_weekly_duration_s=recent_duration,
            habitual_weekly_duration_s=habitual_duration,
            recent_sessions_per_week=recent_sessions,
            habitual_sessions_per_week=habitual_sessions,
            recent_hard_sessions_per_week=recent_hard,
            habitual_hard_sessions_per_week=habitual_hard,
            distance_ratio=(
                round(recent_distance / habitual_distance, 2)
                if recent_distance is not None
                and habitual_distance is not None
                and habitual_distance > 0
                else None
            ),
        ),
        trends=tuple(_trend(weekly, fitness, coverage, as_of, horizon) for horizon in (4, 12, 26)),
        training_tolerance=ObservedTrainingTolerance(
            covered=load_covered,
            recent_weekly_distance_m=recent_distance,
            habitual_distance_p25_m=habitual_p25,
            habitual_distance_p75_m=habitual_p75,
            distance_position=distance_position,
            recent_hard_sessions_per_week=recent_hard,
            strain_coverage_percent=strain_coverage,
        ),
        strengths=tuple(strengths),
        development_gaps=tuple(development_gaps),
        data_gaps=tuple(data_gaps),
        data_quality=PerformanceDataQuality(
            level=quality_level,
            covered_complete_weeks=covered_weeks,
            running_sessions=len(running),
            distance_coverage_percent=distance_coverage,
            duration_coverage_percent=duration_coverage,
            strain_coverage_percent=strain_coverage,
            split_coverage_percent=split_coverage,
            detail_coverage_percent=detail_coverage,
            limitations=tuple(limitations),
        ),
    )
