from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from statistics import median
from typing import Literal

from sqlalchemy.orm import Session

from app.models import Activity, ActivitySplit, DailyFitness
from app.repositories.activities import activities_with_history_between
from app.repositories.fitness import fitness_between
from app.repositories.sync_state import sync_states_for_user
from app.services.analytics.detail_evidence import DetailEvidence, analyze_detail_evidence
from app.services.analytics.performance_state import (
    AutomaticPerformanceState,
    build_performance_state,
)

RUNNING_TYPES = {"running", "street_running", "track_running"}
TRAIL_TYPES = {"trail_running", "trail_run", "ultra_run", "obstacle_run"}
TREADMILL_TYPES = {"treadmill_running", "indoor_running"}
ALL_RUNNING_TYPES = RUNNING_TYPES | TRAIL_TYPES | TREADMILL_TYPES
TrainingRangeKey = Literal[
    "easy",
    "tempo",
    "interval",
    "long_run",
]
BEST_EFFORT_TARGETS = (
    ("1k", 1_000.0, "personal_record_1k_seconds"),
    ("5k", 5_000.0, "personal_record_5k_seconds"),
    ("10k", 10_000.0, "personal_record_10k_seconds"),
    ("half_marathon", 21_097.5, "personal_record_half_seconds"),
    ("marathon", 42_195.0, "personal_record_marathon_seconds"),
)


@dataclass(frozen=True)
class ProfileCoverage:
    history_complete: bool
    oldest_synced_date: date | None
    newest_synced_date: date | None
    window_start: date
    window_end: date


@dataclass(frozen=True)
class ObservedBestEffort:
    distance_key: str
    distance_m: float
    duration_s: float
    occurred_on: date | None
    source: Literal["garmin_personal_record", "observed_split", "fit", "sampled_detail"]
    activity_id: int | None
    clock: Literal["provider", "elapsed", "timer"]
    confidence: Literal["reported", "high", "medium"]


@dataclass(frozen=True)
class WeeklyCapacity:
    weeks: int
    covered: bool
    sustainable_distance_m: float | None
    weekly_distance_p25_m: float | None
    weekly_distance_p75_m: float | None
    sessions_per_week_median: float | None
    active_days_per_week_median: float | None


@dataclass(frozen=True)
class LongestRun:
    stratum: Literal["road", "trail", "treadmill"]
    distance_m: float
    duration_s: float | None
    activity_id: int
    occurred_on: date


@dataclass(frozen=True)
class IntensityDistribution:
    window_days: int
    eligible_activities: int
    total_running_seconds: float
    covered_seconds: float
    coverage_percent: float
    sufficient: bool
    low_percent: float | None
    moderate_percent: float | None
    high_percent: float | None
    unknown_seconds: float


@dataclass(frozen=True)
class EmpiricalTrainingRange:
    key: TrainingRangeKey
    stratum: Literal["road", "trail", "treadmill"]
    sufficient: bool
    pace_median_s_per_km: float | None
    pace_p25_s_per_km: float | None
    pace_p75_s_per_km: float | None
    heart_rate_median: float | None
    sample_sessions: int
    sample_efforts: int
    sample_minutes: float
    confidence: Literal["medium", "low", "insufficient"]


@dataclass(frozen=True)
class AutomaticAthleteProfile:
    schema_version: str
    formula_version: str
    as_of: date
    coverage: ProfileCoverage
    best_efforts: tuple[ObservedBestEffort, ...]
    weekly_capacity: WeeklyCapacity
    longest_runs: tuple[LongestRun, ...]
    intensity: IntensityDistribution
    training_ranges: tuple[EmpiricalTrainingRange, ...]
    detail_evidence: DetailEvidence
    performance_state: AutomaticPerformanceState
    warnings: tuple[str, ...]


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] * (1 - fraction) + ordered[upper] * fraction, 2)


def _running_stratum(activity_type: str) -> Literal["road", "trail", "treadmill"] | None:
    normalized = activity_type.lower()
    if normalized in RUNNING_TYPES:
        return "road"
    if normalized in TRAIL_TYPES:
        return "trail"
    if normalized in TREADMILL_TYPES:
        return "treadmill"
    return None


def _activity_duration(activity: Activity) -> float | None:
    return activity.duration_s or activity.elapsed_duration_s or activity.moving_duration_s


def _split_duration(split: ActivitySplit) -> tuple[float | None, Literal["elapsed", "timer"]]:
    if split.elapsed_duration_s is not None:
        return split.elapsed_duration_s, "elapsed"
    return split.duration_s, "timer"


def _latest_fitness_value(
    rows: list[DailyFitness], attribute: str
) -> tuple[float | None, date | None]:
    for row in reversed(rows):
        value = getattr(row, attribute)
        if isinstance(value, (int, float)) and value > 0:
            return float(value), row.day
    return None, None


def _best_efforts(
    activities: list[Activity],
    fitness: list[DailyFitness],
    detail_evidence: DetailEvidence,
) -> tuple[ObservedBestEffort, ...]:
    detailed = {item.distance_key: item for item in detail_evidence.best_efforts}
    results = []
    for distance_key, target_m, fitness_attribute in BEST_EFFORT_TARGETS:
        provider_value, provider_day = _latest_fitness_value(fitness, fitness_attribute)
        if provider_value is not None:
            results.append(
                ObservedBestEffort(
                    distance_key,
                    target_m,
                    provider_value,
                    provider_day,
                    "garmin_personal_record",
                    None,
                    "provider",
                    "reported",
                )
            )
            continue

        candidates: list[tuple[float, Activity, Literal["elapsed", "timer"]]] = []
        tolerance = max(10.0, target_m * 0.005)
        for activity in activities:
            if _running_stratum(activity.activity_type) is None:
                continue
            for split in activity.splits:
                if split.distance_m is None or abs(split.distance_m - target_m) > tolerance:
                    continue
                duration, clock = _split_duration(split)
                if duration is not None and duration > 0:
                    candidates.append((duration, activity, clock))
        if candidates:
            duration, activity, clock = min(candidates, key=lambda item: item[0])
            results.append(
                ObservedBestEffort(
                    distance_key,
                    target_m,
                    duration,
                    activity.started_at.date(),
                    "observed_split",
                    activity.id,
                    clock,
                    "medium",
                )
            )
            continue
        detail = detailed.get(distance_key)
        if detail is not None:
            results.append(
                ObservedBestEffort(
                    distance_key,
                    target_m,
                    detail.duration_s,
                    detail.occurred_on,
                    detail.source,
                    detail.activity_id,
                    "timer",
                    detail.confidence,
                )
            )
    return tuple(results)


def _weekly_capacity(
    activities: list[Activity], as_of: date, coverage: ProfileCoverage
) -> WeeklyCapacity:
    last_complete_week_end = as_of - timedelta(days=as_of.weekday() + 1)
    first_week_start = last_complete_week_end - timedelta(weeks=12) + timedelta(days=1)
    covered = (
        coverage.history_complete
        and coverage.oldest_synced_date is not None
        and coverage.oldest_synced_date <= first_week_start
        and coverage.newest_synced_date is not None
        and coverage.newest_synced_date >= last_complete_week_end
    )
    if not covered:
        return WeeklyCapacity(12, False, None, None, None, None, None)

    distances = [0.0] * 12
    sessions = [0] * 12
    active_days: list[set[date]] = [set() for _ in range(12)]
    for activity in activities:
        if activity.activity_type.lower() not in ALL_RUNNING_TYPES:
            continue
        day = activity.started_at.date()
        if not first_week_start <= day <= last_complete_week_end:
            continue
        index = (day - first_week_start).days // 7
        sessions[index] += 1
        active_days[index].add(day)
        if activity.distance_m is not None:
            distances[index] += activity.distance_m
    recent_six = distances[-6:]
    return WeeklyCapacity(
        weeks=12,
        covered=True,
        sustainable_distance_m=round(float(median(recent_six)), 2),
        weekly_distance_p25_m=_percentile(distances, 0.25),
        weekly_distance_p75_m=_percentile(distances, 0.75),
        sessions_per_week_median=round(float(median(sessions[-8:])), 2),
        active_days_per_week_median=round(
            float(median([len(days) for days in active_days[-8:]])), 2
        ),
    )


def _longest_runs(activities: list[Activity], as_of: date) -> tuple[LongestRun, ...]:
    start = as_of - timedelta(days=83)
    longest: dict[Literal["road", "trail", "treadmill"], Activity] = {}
    for activity in activities:
        stratum = _running_stratum(activity.activity_type)
        if (
            stratum is None
            or activity.distance_m is None
            or activity.distance_m <= 0
            or activity.started_at.date() < start
        ):
            continue
        current = longest.get(stratum)
        if current is None or activity.distance_m > (current.distance_m or 0):
            longest[stratum] = activity
    return tuple(
        LongestRun(
            stratum=stratum,
            distance_m=activity.distance_m or 0,
            duration_s=_activity_duration(activity),
            activity_id=activity.id,
            occurred_on=activity.started_at.date(),
        )
        for stratum, activity in sorted(longest.items())
    )


def _intensity_distribution(
    activities: list[Activity], as_of: date, coverage: ProfileCoverage
) -> IntensityDistribution:
    start = as_of - timedelta(days=83)
    total_seconds = 0.0
    covered_seconds = 0.0
    low = 0.0
    moderate = 0.0
    high = 0.0
    eligible = 0
    for activity in activities:
        if (
            activity.activity_type.lower() not in ALL_RUNNING_TYPES
            or activity.started_at.date() < start
        ):
            continue
        duration = _activity_duration(activity)
        if duration is None or duration <= 0:
            continue
        total_seconds += duration
        hr_zones = [
            zone for zone in activity.zones if zone.zone_type == "heart_rate" and zone.seconds
        ]
        zone_total = sum(zone.seconds or 0 for zone in hr_zones)
        if zone_total <= 0:
            continue
        eligible += 1
        covered_seconds += min(zone_total, duration)
        low += sum(zone.seconds or 0 for zone in hr_zones if zone.zone_number <= 2)
        moderate += sum(zone.seconds or 0 for zone in hr_zones if zone.zone_number == 3)
        high += sum(zone.seconds or 0 for zone in hr_zones if zone.zone_number >= 4)
    coverage_percent = (
        round(min(covered_seconds / total_seconds * 100, 100), 1) if total_seconds else 0.0
    )
    classified = low + moderate + high
    history_covered = (
        coverage.history_complete
        and coverage.oldest_synced_date is not None
        and coverage.oldest_synced_date <= start
        and coverage.newest_synced_date is not None
        and coverage.newest_synced_date >= as_of
    )
    sufficient = (
        history_covered and eligible >= 10 and covered_seconds >= 14_400 and coverage_percent >= 70
    )
    return IntensityDistribution(
        window_days=84,
        eligible_activities=eligible,
        total_running_seconds=round(total_seconds, 2),
        covered_seconds=round(covered_seconds, 2),
        coverage_percent=coverage_percent,
        sufficient=sufficient,
        low_percent=round(low / classified * 100, 1) if sufficient and classified else None,
        moderate_percent=(
            round(moderate / classified * 100, 1) if sufficient and classified else None
        ),
        high_percent=round(high / classified * 100, 1) if sufficient and classified else None,
        unknown_seconds=round(max(total_seconds - covered_seconds, 0), 2),
    )


def _pace(duration_s: float | None, distance_m: float | None) -> float | None:
    if duration_s is None or distance_m is None or duration_s <= 0 or distance_m <= 0:
        return None
    return duration_s * 1_000 / distance_m


def _stop_ratio(activity: Activity) -> float | None:
    if (
        activity.elapsed_duration_s is None
        or activity.moving_duration_s is None
        or activity.moving_duration_s <= 0
    ):
        return None
    return activity.elapsed_duration_s / activity.moving_duration_s


def _elevation_per_km(activity: Activity) -> float | None:
    if activity.elevation_gain_m is None or not activity.distance_m:
        return None
    return activity.elevation_gain_m / (activity.distance_m / 1_000)


def _has_interval_structure(activity: Activity) -> bool:
    return any(
        split.split_type.startswith("typed_")
        and (
            "interval" in split.split_type.lower()
            or (split.intensity_type or "").lower()
            in {"interval", "work", "active", "recovery", "rest"}
        )
        for split in activity.splits
    )


def _is_explicit_tempo_split(split: ActivitySplit) -> bool:
    split_type = split.split_type.lower()
    intensity_type = (split.intensity_type or "").lower()
    return (
        "tempo" in split_type
        or "threshold" in split_type
        or intensity_type
        in {
            "tempo",
            "threshold",
        }
    )


def _lap_pace_cv(activity: Activity) -> float | None:
    paces = [
        pace
        for split in activity.splits
        if split.split_type == "lap"
        and (split_duration := split.elapsed_duration_s or split.duration_s) is not None
        and 500 <= (split.distance_m or 0) <= 1_500
        and (pace := _pace(split_duration, split.distance_m)) is not None
    ]
    if len(paces) < 3:
        return None
    mean_pace = sum(paces) / len(paces)
    variance = sum((pace - mean_pace) ** 2 for pace in paces) / len(paces)
    return variance**0.5 / mean_pace * 100


def _hr_zone_shares(activity: Activity) -> tuple[float, float, float, float] | None:
    zones = [zone for zone in activity.zones if zone.zone_type == "heart_rate" and zone.seconds]
    total = sum(zone.seconds or 0 for zone in zones)
    duration = _activity_duration(activity)
    if not zones or total <= 0 or duration is None or duration <= 0:
        return None
    return (
        sum(zone.seconds or 0 for zone in zones if zone.zone_number <= 2) / total,
        sum(zone.seconds or 0 for zone in zones if zone.zone_number == 3) / total,
        sum(zone.seconds or 0 for zone in zones if zone.zone_number >= 4) / total,
        min(total / duration, 1),
    )


def _range(
    key: TrainingRangeKey,
    stratum: Literal["road", "trail", "treadmill"],
    samples: list[tuple[float, int | None, int, float]],
    *,
    min_sessions: int,
    min_efforts: int,
    min_minutes: float,
    confidence: Literal["medium", "low"] = "medium",
) -> EmpiricalTrainingRange:
    sessions = {activity_id for _, _, activity_id, _ in samples}
    minutes = sum(duration for _, _, _, duration in samples) / 60
    sufficient = (
        len(sessions) >= min_sessions and len(samples) >= min_efforts and minutes >= min_minutes
    )
    paces = [pace for pace, _, _, _ in samples]
    heart_rates = [float(hr) for _, hr, _, _ in samples if hr is not None]
    return EmpiricalTrainingRange(
        key=key,
        stratum=stratum,
        sufficient=sufficient,
        pace_median_s_per_km=(round(float(median(paces)), 2) if sufficient else None),
        pace_p25_s_per_km=_percentile(paces, 0.25) if sufficient else None,
        pace_p75_s_per_km=_percentile(paces, 0.75) if sufficient else None,
        heart_rate_median=(
            round(float(median(heart_rates)), 1) if sufficient and heart_rates else None
        ),
        sample_sessions=len(sessions),
        sample_efforts=len(samples),
        sample_minutes=round(minutes, 1),
        confidence=confidence if sufficient else "insufficient",
    )


def _training_ranges(activities: list[Activity], as_of: date) -> tuple[EmpiricalTrainingRange, ...]:
    samples: dict[
        tuple[str, Literal["road", "trail", "treadmill"]],
        list[tuple[float, int | None, int, float]],
    ] = {}
    recent_84 = as_of - timedelta(days=83)
    recent_120 = as_of - timedelta(days=119)
    recent_112 = as_of - timedelta(days=111)

    qualifying_durations = [
        duration
        for activity in activities
        if _running_stratum(activity.activity_type) is not None
        and (duration := _activity_duration(activity)) is not None
        and duration >= 600
    ]
    typical_duration = float(median(qualifying_durations)) if qualifying_durations else None

    for activity in activities:
        stratum = _running_stratum(activity.activity_type)
        duration = _activity_duration(activity)
        pace = _pace(duration, activity.distance_m)
        if stratum is None or duration is None or pace is None or duration < 600:
            continue
        stop_ratio = _stop_ratio(activity)
        if stop_ratio is not None and stop_ratio > 1.10:
            continue
        elevation = _elevation_per_km(activity)
        shares = _hr_zone_shares(activity)
        has_intervals = _has_interval_structure(activity)
        training_label = (activity.training_effect_label or "").upper()

        low_intensity = False
        if shares is not None:
            low, _, high, hr_coverage = shares
            low_intensity = low >= 0.8 and high <= 0.05 and hr_coverage >= 0.7
        if activity.workout_rpe is not None and activity.workout_rpe <= 4:
            low_intensity = True
        if (
            activity.aerobic_training_effect is not None
            and activity.aerobic_training_effect < 3.0
            and (activity.anaerobic_training_effect or 0) < 1.5
            and activity.workout_rpe is None
        ):
            low_intensity = True
        if (
            training_label == "AEROBIC_BASE"
            and (activity.anaerobic_training_effect or 0) < 1.5
            and (activity.workout_rpe is None or activity.workout_rpe <= 5)
        ):
            low_intensity = True

        if (
            activity.started_at.date() >= recent_84
            and duration >= 1_200
            and not has_intervals
            and low_intensity
            and (elevation is None or elevation <= 20)
        ):
            samples.setdefault(("easy", stratum), []).append(
                (pace, activity.average_hr, activity.id, duration)
            )

        long_threshold = (
            max(2_700, min(3_600, typical_duration * 1.33))
            if typical_duration is not None
            else 3_600
        )
        if (
            activity.started_at.date() >= recent_112
            and duration >= long_threshold
            and not has_intervals
            and low_intensity
        ):
            samples.setdefault(("long_run", stratum), []).append(
                (pace, activity.average_hr, activity.id, duration)
            )

        if activity.started_at.date() < recent_120:
            continue
        explicit_tempo_split = any(_is_explicit_tempo_split(split) for split in activity.splits)
        lap_pace_cv = _lap_pace_cv(activity)
        if (
            not explicit_tempo_split
            and training_label in {"TEMPO", "LACTATE_THRESHOLD"}
            and 900 <= duration <= 4_500
            and not has_intervals
            and (lap_pace_cv is None or lap_pace_cv <= 8)
        ):
            samples.setdefault(("tempo", stratum), []).append(
                (pace, activity.average_hr, activity.id, duration)
            )
        if training_label in {"VO2MAX", "ANAEROBIC_CAPACITY", "ANAEROBIC"}:
            repeated_runs = []
            for split in activity.splits:
                split_duration = split.elapsed_duration_s or split.duration_s
                split_pace = _pace(split_duration, split.distance_m)
                if (
                    split.split_type.lower().startswith("typed_rwd_run")
                    and split_duration is not None
                    and split_pace is not None
                    and 45 <= split_duration <= 480
                    and 200 <= (split.distance_m or 0) <= 2_000
                ):
                    repeated_runs.append((split, split_duration, split_pace))
            if len(repeated_runs) >= 4:
                typical_repetition_pace = float(median(item[2] for item in repeated_runs))
                for split, split_duration, split_pace in repeated_runs:
                    if split_pace <= typical_repetition_pace * 1.12:
                        samples.setdefault(("interval", stratum), []).append(
                            (split_pace, split.average_hr, activity.id, split_duration)
                        )
        for split in activity.splits:
            split_duration = split.elapsed_duration_s or split.duration_s
            split_pace = _pace(split_duration, split.distance_m)
            if split_duration is None or split_pace is None:
                continue
            split_type = split.split_type.lower()
            intensity_type = (split.intensity_type or "").lower()
            if intensity_type in {"recovery", "rest", "warmup", "cooldown"}:
                continue
            if 900 <= split_duration <= 2_400 and _is_explicit_tempo_split(split):
                samples.setdefault(("tempo", stratum), []).append(
                    (split_pace, split.average_hr, activity.id, split_duration)
                )
            if not split_type.startswith("typed_") or not (
                "interval" in split_type or intensity_type in {"interval", "work", "active"}
            ):
                continue
            if 45 <= split_duration <= 480 or 200 <= (split.distance_m or 0) <= 2_000:
                samples.setdefault(("interval", stratum), []).append(
                    (split_pace, split.average_hr, activity.id, split_duration)
                )

    requirements: dict[TrainingRangeKey, tuple[int, int, float, Literal["medium", "low"]]] = {
        "easy": (6, 6, 180.0, "medium"),
        "tempo": (3, 3, 60.0, "medium"),
        "interval": (1, 4, 8.0, "low"),
        "long_run": (4, 4, 300.0, "medium"),
    }
    result = []
    for stratum in ("road", "trail", "treadmill"):
        for key, required in requirements.items():
            values = samples.get((key, stratum), [])
            if values or stratum == "road":
                confidence = required[3]
                if key == "interval":
                    interval_sessions = {activity_id for _, _, activity_id, _ in values}
                    interval_minutes = sum(duration for _, _, _, duration in values) / 60
                    if len(interval_sessions) >= 3 and len(values) >= 12 and interval_minutes >= 24:
                        confidence = "medium"
                result.append(
                    _range(
                        key,
                        stratum,
                        values,
                        min_sessions=required[0],
                        min_efforts=required[1],
                        min_minutes=required[2],
                        confidence=confidence,
                    )
                )
    return tuple(result)


def get_automatic_athlete_profile(
    session: Session,
    user_id: int,
    *,
    as_of: date | None = None,
    include_detail_evidence: bool = True,
) -> AutomaticAthleteProfile:
    end = as_of or date.today()
    query_start = end - timedelta(days=364)
    activities = activities_with_history_between(
        session,
        user_id,
        datetime.combine(query_start, time.min),
        datetime.combine(end + timedelta(days=1), time.min),
    )
    fitness = fitness_between(session, user_id, query_start, end)
    states = {state.resource: state for state in sync_states_for_user(session, user_id)}
    activity_state = states.get("activities")
    coverage = ProfileCoverage(
        history_complete=activity_state.backfill_complete if activity_state else False,
        oldest_synced_date=activity_state.oldest_synced_date if activity_state else None,
        newest_synced_date=activity_state.newest_synced_date if activity_state else None,
        window_start=query_start,
        window_end=end,
    )
    capacity = _weekly_capacity(activities, end, coverage)
    intensity = _intensity_distribution(activities, end, coverage)
    training_ranges = _training_ranges(activities, end)
    detail_evidence = analyze_detail_evidence(
        activities if include_detail_evidence else [],
        fitness if include_detail_evidence else [],
        end,
    )
    best_efforts = _best_efforts(activities, fitness, detail_evidence)
    longest_runs = _longest_runs(activities, end)
    performance_state = build_performance_state(
        activities=activities,
        fitness=fitness,
        as_of=end,
        coverage=coverage,
        weekly_capacity=capacity,
        longest_runs=longest_runs,
        intensity=intensity,
        training_ranges=training_ranges,
        detail_evidence=detail_evidence,
        best_efforts=best_efforts,
    )
    warnings = []
    if not coverage.history_complete:
        warnings.append("training_history_partial")
    if not capacity.covered:
        warnings.append("weekly_capacity_unknown")
    if not intensity.sufficient:
        warnings.append("intensity_coverage_insufficient")
    if not any(item.sufficient for item in training_ranges):
        warnings.append("training_ranges_insufficient")
    if include_detail_evidence and detail_evidence.coverage.analyzed_activities == 0:
        warnings.append("detail_evidence_unavailable")
    return AutomaticAthleteProfile(
        schema_version="automatic-athlete-profile.v3",
        formula_version="observed-profile.v1",
        as_of=end,
        coverage=coverage,
        best_efforts=best_efforts,
        weekly_capacity=capacity,
        longest_runs=longest_runs,
        intensity=intensity,
        training_ranges=training_ranges,
        detail_evidence=detail_evidence,
        performance_state=performance_state,
        warnings=tuple(warnings),
    )
