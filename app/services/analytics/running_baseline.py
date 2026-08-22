import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from app.models import Activity, GarminSyncState
from app.repositories.activities import activities_between, activity_query
from app.repositories.sync_state import sync_states_for_user
from app.services.analytics.activity_semantics import (
    HARD_ACTIVITY_RULE_VERSION,
    SPORT_CLASSIFICATION_VERSION,
    calendar_window,
    hard_activity_data_available,
    is_hard_activity,
    is_running_sport,
)

RUNNING_BASELINE_VERSION = "1.0"
RUNNING_BASELINE_WINDOWS = (7, 28, 56, 180)
INTERRUPTION_DAYS = 7
REENTRY_INTERRUPTION_DAYS = 14
REENTRY_OBSERVATION_DAYS = 14
DISTANCE_SPIKE_RATIO = 1.1


@dataclass(frozen=True)
class RobustStatistic:
    median: float | None
    median_absolute_deviation: float | None
    sample_count: int


@dataclass(frozen=True)
class MetricCoverage:
    available: int
    total: int
    percent: float


@dataclass(frozen=True)
class PeakRunMetric:
    value: float | None
    day: date | None


@dataclass(frozen=True)
class RunningDataQuality:
    source: str
    sync_status: str
    history_complete: bool
    oldest_synced_date: date | None
    newest_synced_date: date | None
    history_coverage_percent: float
    sync_age_days: int | None
    latest_run_day: date | None
    latest_run_age_days: int | None
    duration: MetricCoverage
    distance: MetricCoverage
    rpe: MetricCoverage
    srpe: MetricCoverage
    hard_classification: MetricCoverage
    invalid_duration_values: int
    invalid_distance_values: int
    invalid_rpe_values: int
    confidence: str
    confidence_reasons: tuple[str, ...]


@dataclass(frozen=True)
class RunningWindowBaseline:
    start: date
    end: date
    days: int
    runs: int
    active_days: int
    frequency_per_week: float
    total_duration_s: float | None
    total_distance_m: float | None
    per_run_duration_s: RobustStatistic
    per_run_distance_m: RobustStatistic
    weekly_runs: RobustStatistic
    weekly_duration_s: RobustStatistic
    weekly_distance_m: RobustStatistic
    weekly_longest_duration_s: RobustStatistic
    weekly_longest_distance_m: RobustStatistic
    longest_duration: PeakRunMetric
    longest_distance: PeakRunMetric
    hard_runs: int
    hard_days: int
    hard_days_per_week: float
    quality_density_percent: float | None
    minimum_hard_day_gap_days: int | None
    consecutive_hard_days: bool
    total_srpe: float | None
    per_run_srpe: RobustStatistic
    quality: RunningDataQuality


@dataclass(frozen=True)
class RunningInterruption:
    previous_run_day: date
    resumed_on: date | None
    inactive_days: int
    current: bool


@dataclass(frozen=True)
class RunningReentry:
    active: bool
    resumed_on: date | None
    preceding_inactive_days: int | None
    observation_days: int


@dataclass(frozen=True)
class DistanceSpike:
    run_day: date
    distance_m: float
    prior_30d_longest_distance_m: float
    ratio: float
    exceeds_110_percent: bool


@dataclass(frozen=True)
class RunningBaseline:
    as_of: date
    baseline_version: str
    sport_classification_version: str
    hard_activity_rule_version: str
    windows: tuple[RunningWindowBaseline, ...]
    interruptions: tuple[RunningInterruption, ...]
    reentry: RunningReentry
    latest_distance_spike: DistanceSpike | None
    input_fingerprint: str

    def window(self, days: int) -> RunningWindowBaseline:
        match = next((window for window in self.windows if window.days == days), None)
        if match is None:
            raise ValueError(f"unsupported running baseline window: {days}")
        return match


def _round(value: float) -> float:
    return round(value, 2)


def _robust(values: list[float]) -> RobustStatistic:
    if not values:
        return RobustStatistic(None, None, 0)
    center = float(median(values))
    spread = float(median([abs(value - center) for value in values]))
    return RobustStatistic(_round(center), _round(spread), len(values))


def _coverage(available: int, total: int) -> MetricCoverage:
    percent = round(available * 100 / total, 1) if total else 0.0
    return MetricCoverage(available=available, total=total, percent=percent)


def _optional_total(runs: list[Activity], values: list[float]) -> float | None:
    if not runs:
        return 0.0
    return _round(sum(values)) if values else None


def _valid_measurement(value: float | int | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _peak(runs: list[Activity], attribute: str) -> PeakRunMetric:
    available = [
        (run, value)
        for run in runs
        if (value := _valid_measurement(getattr(run, attribute))) is not None
    ]
    if not available:
        return PeakRunMetric(None, None)
    peak, value = max(available, key=lambda item: item[1])
    return PeakRunMetric(_round(value), peak.started_at.date())


def _history_coverage(state: GarminSyncState | None, start: date, end: date) -> float:
    if state is None:
        return 0.0
    if state.backfill_complete:
        return 100.0
    if state.oldest_synced_date is None or state.newest_synced_date is None:
        return 0.0
    overlap_start = max(start, state.oldest_synced_date)
    overlap_end = min(end, state.newest_synced_date)
    if overlap_start > overlap_end:
        return 0.0
    return round(((overlap_end - overlap_start).days + 1) * 100 / ((end - start).days + 1), 1)


def _confidence(
    runs: list[Activity],
    history_percent: float,
    latest_age_days: int | None,
    duration: MetricCoverage,
    distance: MetricCoverage,
    hard: MetricCoverage,
    invalid_duration: int,
    invalid_distance: int,
    invalid_rpe: int,
    sync_status: str,
    sync_age_days: int | None,
) -> tuple[str, tuple[str, ...]]:
    reasons: list[str] = []
    if len(runs) < 2:
        reasons.append("fewer_than_2_runs")
    elif len(runs) < 6:
        reasons.append("fewer_than_6_runs")
    elif len(runs) < 12:
        reasons.append("fewer_than_12_runs")
    if history_percent < 50:
        reasons.append("history_coverage_below_50_percent")
    elif history_percent < 80:
        reasons.append("history_coverage_below_80_percent")
    elif history_percent < 100:
        reasons.append("history_coverage_incomplete")
    if latest_age_days is None:
        reasons.append("no_recent_run")
    elif latest_age_days > 56:
        reasons.append("latest_run_older_than_56_days")
    elif latest_age_days > 21:
        reasons.append("latest_run_older_than_21_days")
    elif latest_age_days > 14:
        reasons.append("latest_run_older_than_14_days")
    if duration.percent < 70:
        reasons.append("duration_coverage_below_70_percent")
    elif duration.percent < 90:
        reasons.append("duration_coverage_below_90_percent")
    if distance.percent < 70:
        reasons.append("distance_coverage_below_70_percent")
    elif distance.percent < 90:
        reasons.append("distance_coverage_below_90_percent")
    if hard.percent < 80:
        reasons.append("hard_classification_coverage_below_80_percent")
    if invalid_duration:
        reasons.append("invalid_duration_values")
    if invalid_distance:
        reasons.append("invalid_distance_values")
    if invalid_rpe:
        reasons.append("invalid_rpe_values")
    if sync_status != "ok":
        reasons.append("activity_sync_not_ok")
    if sync_age_days is not None and sync_age_days > 7:
        reasons.append("activity_sync_older_than_7_days")

    if (
        len(runs) >= 12
        and history_percent == 100
        and latest_age_days is not None
        and latest_age_days <= 14
        and duration.percent >= 90
        and distance.percent >= 90
        and hard.percent >= 80
        and sync_status == "ok"
        and (sync_age_days is None or sync_age_days <= 2)
    ):
        return "high", tuple(reasons)
    if (
        len(runs) >= 6
        and history_percent >= 80
        and latest_age_days is not None
        and latest_age_days <= 21
        and duration.percent >= 70
        and distance.percent >= 70
        and hard.percent >= 50
        and sync_status in {"ok", "partial"}
        and (sync_age_days is None or sync_age_days <= 7)
    ):
        return "medium", tuple(reasons)
    if len(runs) >= 2 and history_percent >= 50 and latest_age_days is not None:
        return "low", tuple(reasons)
    return "insufficient", tuple(reasons)


def _weekly_values(
    runs: list[Activity], start: date, end: date
) -> tuple[list[float], list[float], list[float], list[float], list[float]]:
    counts: list[float] = []
    durations: list[float] = []
    distances: list[float] = []
    longest_durations: list[float] = []
    longest_distances: list[float] = []
    week_end = end
    while week_end >= start:
        week_start = max(start, week_end - timedelta(days=6))
        week = [run for run in runs if week_start <= run.started_at.date() <= week_end]
        counts.append(float(len(week)))
        duration_values = [
            value for run in week if (value := _valid_measurement(run.duration_s)) is not None
        ]
        distance_values = [
            value for run in week if (value := _valid_measurement(run.distance_m)) is not None
        ]
        if not week:
            durations.append(0.0)
            distances.append(0.0)
            longest_durations.append(0.0)
            longest_distances.append(0.0)
        else:
            if duration_values:
                durations.append(sum(duration_values))
                longest_durations.append(max(duration_values))
            if distance_values:
                distances.append(sum(distance_values))
                longest_distances.append(max(distance_values))
        week_end = week_start - timedelta(days=1)
    return counts, durations, distances, longest_durations, longest_distances


def _window_baseline(
    all_runs: list[Activity],
    state: GarminSyncState | None,
    *,
    days: int,
    as_of: date,
) -> RunningWindowBaseline:
    window = calendar_window(days, as_of=as_of)
    runs = [run for run in all_runs if run.started_at.date() >= window.start]
    duration_values = [
        value for run in runs if (value := _valid_measurement(run.duration_s)) is not None
    ]
    distance_values = [
        value for run in runs if (value := _valid_measurement(run.distance_m)) is not None
    ]
    valid_rpe = [run for run in runs if run.workout_rpe is not None and 1 <= run.workout_rpe <= 10]
    invalid_duration = sum(
        run.duration_s is not None and _valid_measurement(run.duration_s) is None for run in runs
    )
    invalid_distance = sum(
        run.distance_m is not None and _valid_measurement(run.distance_m) is None for run in runs
    )
    invalid_rpe = sum(
        run.workout_rpe is not None and not 1 <= run.workout_rpe <= 10 for run in runs
    )
    srpe_values: list[float] = []
    for run in valid_rpe:
        rpe = run.workout_rpe
        duration = _valid_measurement(run.duration_s)
        if rpe is not None and duration is not None:
            srpe_values.append(float(rpe) * duration / 60)
    hard_runs = [run for run in runs if is_hard_activity(run)]
    hard_days = sorted({run.started_at.date() for run in hard_runs})
    gaps = [
        (later - earlier).days for earlier, later in zip(hard_days, hard_days[1:], strict=False)
    ]
    duration_coverage = _coverage(len(duration_values), len(runs))
    distance_coverage = _coverage(len(distance_values), len(runs))
    rpe_coverage = _coverage(len(valid_rpe), len(runs))
    srpe_coverage = _coverage(len(srpe_values), len(runs))
    hard_coverage = _coverage(sum(hard_activity_data_available(run) for run in runs), len(runs))
    history_percent = _history_coverage(state, window.start, window.end)
    latest_day = max((run.started_at.date() for run in runs), default=None)
    latest_age = (as_of - latest_day).days if latest_day else None
    sync_status = state.status if state else "not_synced"
    sync_age = (
        max(0, (as_of - state.last_success_at.date()).days)
        if state and state.last_success_at
        else None
    )
    confidence, confidence_reasons = _confidence(
        runs,
        history_percent,
        latest_age,
        duration_coverage,
        distance_coverage,
        hard_coverage,
        invalid_duration,
        invalid_distance,
        invalid_rpe,
        sync_status,
        sync_age,
    )
    (
        weekly_runs,
        weekly_duration,
        weekly_distance,
        weekly_longest_duration,
        weekly_longest_distance,
    ) = _weekly_values(runs, window.start, window.end)
    return RunningWindowBaseline(
        start=window.start,
        end=window.end,
        days=days,
        runs=len(runs),
        active_days=len({run.started_at.date() for run in runs}),
        frequency_per_week=round(len(runs) * 7 / days, 2),
        total_duration_s=_optional_total(runs, duration_values),
        total_distance_m=_optional_total(runs, distance_values),
        per_run_duration_s=_robust(duration_values),
        per_run_distance_m=_robust(distance_values),
        weekly_runs=_robust(weekly_runs),
        weekly_duration_s=_robust(weekly_duration),
        weekly_distance_m=_robust(weekly_distance),
        weekly_longest_duration_s=_robust(weekly_longest_duration),
        weekly_longest_distance_m=_robust(weekly_longest_distance),
        longest_duration=_peak(runs, "duration_s"),
        longest_distance=_peak(runs, "distance_m"),
        hard_runs=len(hard_runs),
        hard_days=len(hard_days),
        hard_days_per_week=round(len(hard_days) * 7 / days, 2),
        quality_density_percent=(
            round(len(hard_days) * 100 / len({run.started_at.date() for run in runs}), 1)
            if runs
            else None
        ),
        minimum_hard_day_gap_days=min(gaps) if gaps else None,
        consecutive_hard_days=any(gap == 1 for gap in gaps),
        total_srpe=_round(sum(srpe_values)) if srpe_values else None,
        per_run_srpe=_robust(srpe_values),
        quality=RunningDataQuality(
            source="garmin_activities",
            sync_status=sync_status,
            history_complete=state.backfill_complete if state else False,
            oldest_synced_date=state.oldest_synced_date if state else None,
            newest_synced_date=(
                min(state.newest_synced_date, as_of) if state and state.newest_synced_date else None
            ),
            history_coverage_percent=history_percent,
            sync_age_days=sync_age,
            latest_run_day=latest_day,
            latest_run_age_days=latest_age,
            duration=duration_coverage,
            distance=distance_coverage,
            rpe=rpe_coverage,
            srpe=srpe_coverage,
            hard_classification=hard_coverage,
            invalid_duration_values=invalid_duration,
            invalid_distance_values=invalid_distance,
            invalid_rpe_values=invalid_rpe,
            confidence=confidence,
            confidence_reasons=confidence_reasons,
        ),
    )


def _interruptions(
    runs: list[Activity], predecessor: Activity | None, as_of: date
) -> tuple[RunningInterruption, ...]:
    days = sorted(
        {run.started_at.date() for run in runs}
        | ({predecessor.started_at.date()} if predecessor else set())
    )
    interruptions = [
        RunningInterruption(previous, resumed, (resumed - previous).days - 1, False)
        for previous, resumed in zip(days, days[1:], strict=False)
        if (resumed - previous).days - 1 >= INTERRUPTION_DAYS
    ]
    if days and (as_of - days[-1]).days >= INTERRUPTION_DAYS:
        interruptions.append(RunningInterruption(days[-1], None, (as_of - days[-1]).days, True))
    return tuple(interruptions)


def _reentry(interruptions: tuple[RunningInterruption, ...], as_of: date) -> RunningReentry:
    completed = [item for item in interruptions if item.resumed_on is not None]
    latest = completed[-1] if completed else None
    resumed_on = latest.resumed_on if latest else None
    active = bool(
        latest
        and resumed_on is not None
        and latest.inactive_days >= REENTRY_INTERRUPTION_DAYS
        and (as_of - resumed_on).days <= REENTRY_OBSERVATION_DAYS
    )
    return RunningReentry(
        active=active,
        resumed_on=latest.resumed_on if active and latest else None,
        preceding_inactive_days=latest.inactive_days if active and latest else None,
        observation_days=REENTRY_OBSERVATION_DAYS,
    )


def _distance_spike(runs: list[Activity], reference_runs: list[Activity]) -> DistanceSpike | None:
    measured = [run for run in runs if _valid_measurement(run.distance_m) is not None]
    references = [run for run in reference_runs if _valid_measurement(run.distance_m) is not None]
    if not measured:
        return None
    latest = measured[-1]
    latest_day = latest.started_at.date()
    reference_start = latest_day - timedelta(days=30)
    prior = [
        value
        for run in references
        if reference_start <= run.started_at.date() < latest_day
        and (value := _valid_measurement(run.distance_m)) is not None
    ]
    if not prior:
        return None
    reference = max(prior)
    latest_distance = latest.distance_m
    if latest_distance is None:
        return None
    ratio = latest_distance / reference
    return DistanceSpike(
        run_day=latest_day,
        distance_m=_round(latest_distance),
        prior_30d_longest_distance_m=_round(reference),
        ratio=round(ratio, 3),
        exceeds_110_percent=ratio > DISTANCE_SPIKE_RATIO,
    )


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _fingerprint_value(value: float | int | None) -> float | int | str | None:
    if isinstance(value, float) and not math.isfinite(value):
        return "non_finite"
    return value


def _input_fingerprint(
    source_runs: list[Activity], state: GarminSyncState | None, as_of: date
) -> str:
    payload = {
        "as_of": as_of,
        "baseline_version": RUNNING_BASELINE_VERSION,
        "sport_classification_version": SPORT_CLASSIFICATION_VERSION,
        "hard_activity_rule_version": HARD_ACTIVITY_RULE_VERSION,
        "activities": [
            {
                "garmin_activity_id": run.garmin_activity_id,
                "started_at": run.started_at,
                "activity_type": run.activity_type,
                "duration_s": _fingerprint_value(run.duration_s),
                "distance_m": _fingerprint_value(run.distance_m),
                "aerobic_training_effect": _fingerprint_value(run.aerobic_training_effect),
                "anaerobic_training_effect": _fingerprint_value(run.anaerobic_training_effect),
                "workout_rpe": run.workout_rpe,
                "source_fingerprint": run.source_fingerprint,
            }
            for run in source_runs
        ],
        "activity_sync": (
            {
                "status": state.status,
                "backfill_complete": state.backfill_complete,
                "oldest_synced_date": state.oldest_synced_date,
                "newest_synced_date": state.newest_synced_date,
                "last_success_at": state.last_success_at,
            }
            if state
            else None
        ),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def get_running_baseline(
    session: Session, user_id: int, *, as_of: date | None = None
) -> RunningBaseline:
    end = as_of or date.today()
    longest = calendar_window(max(RUNNING_BASELINE_WINDOWS), as_of=end)
    query_start = longest.start - timedelta(days=30)
    candidates = activities_between(
        session,
        user_id,
        datetime.combine(query_start, time.min),
        datetime.combine(end + timedelta(days=1), time.min),
    )
    reference_runs = [
        activity for activity in candidates if is_running_sport(activity.activity_type)
    ]
    runs = [activity for activity in reference_runs if activity.started_at.date() >= longest.start]
    predecessor = next(
        (run for run in reversed(reference_runs) if run.started_at.date() < longest.start),
        None,
    )
    if predecessor is None:
        predecessor = next(
            (
                activity
                for activity in session.scalars(
                    activity_query(user_id).where(
                        Activity.started_at < datetime.combine(query_start, time.min)
                    )
                )
                if is_running_sport(activity.activity_type)
            ),
            None,
        )
    states = {state.resource: state for state in sync_states_for_user(session, user_id)}
    activity_state = states.get("activities")
    interruptions = _interruptions(runs, predecessor, end)
    fingerprint_runs = list(reference_runs)
    if predecessor and all(run.id != predecessor.id for run in reference_runs):
        fingerprint_runs.insert(0, predecessor)
    return RunningBaseline(
        as_of=end,
        baseline_version=RUNNING_BASELINE_VERSION,
        sport_classification_version=SPORT_CLASSIFICATION_VERSION,
        hard_activity_rule_version=HARD_ACTIVITY_RULE_VERSION,
        windows=tuple(
            _window_baseline(runs, activity_state, days=days, as_of=end)
            for days in RUNNING_BASELINE_WINDOWS
        ),
        interruptions=interruptions,
        reentry=_reentry(interruptions, end),
        latest_distance_spike=_distance_spike(runs, reference_runs),
        input_fingerprint=_input_fingerprint(fingerprint_runs, activity_state, end),
    )
