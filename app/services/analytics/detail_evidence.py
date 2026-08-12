from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import median, pstdev
from threading import Lock
from typing import Literal

import fitdecode
from garminconnect.activity_details import parse_activity_detail_metrics

from app.models import Activity, DailyFitness
from app.services.garmin.activity_details import load_activity_detail_payload
from app.services.garmin.activity_fit import MAX_FIT_BYTES, fit_eligible_activity_type

EvidenceSource = Literal["fit", "sampled_detail"]
DETAIL_EVIDENCE_FORMULA_VERSION = "detail-evidence.v1"
DETAIL_EVIDENCE_CACHE_SIZE = 32


@dataclass(frozen=True)
class EvidenceSample:
    timer_s: float
    elapsed_s: float
    distance_m: float
    heart_rate: float | None
    elevation_m: float | None


@dataclass(frozen=True)
class DetailBestEffort:
    distance_key: Literal["1k", "5k", "10k"]
    distance_m: float
    duration_s: float
    activity_id: int
    occurred_on: date
    source: EvidenceSource
    sample_count: int
    confidence: Literal["high", "medium"]


@dataclass(frozen=True)
class HeartRateDriftEvidence:
    sufficient: bool
    median_percent: float | None
    sample_sessions: int
    source_activity_ids: tuple[int, ...]
    latest_on: date | None
    confidence: Literal["high", "medium", "insufficient"]


@dataclass(frozen=True)
class ThresholdSegmentEvidence:
    activity_id: int
    occurred_on: date
    duration_s: float
    pace_s_per_km: float
    heart_rate: float | None
    pace_cv_percent: float
    elevation_gain_m: float | None
    elevation_loss_m: float | None
    source: EvidenceSource
    sample_count: int
    confidence: Literal["high", "medium"]


@dataclass(frozen=True)
class DetailEvidenceCoverage:
    eligible_activities: int
    analyzed_activities: int
    fit_activities: int
    sampled_detail_activities: int
    unavailable_activities: int


@dataclass(frozen=True)
class DetailEvidence:
    formula_version: str
    coverage: DetailEvidenceCoverage
    best_efforts: tuple[DetailBestEffort, ...]
    heart_rate_drift: HeartRateDriftEvidence
    threshold_segments: tuple[ThresholdSegmentEvidence, ...]


_detail_evidence_cache: OrderedDict[tuple[object, ...], DetailEvidence] = OrderedDict()
_detail_evidence_cache_lock = Lock()


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if result == result and abs(result) != float("inf") else None


def _valid_samples(samples: list[EvidenceSample]) -> list[EvidenceSample]:
    valid: list[EvidenceSample] = []
    for sample in samples:
        if sample.timer_s < 0 or sample.elapsed_s < 0 or sample.distance_m < 0:
            continue
        if valid:
            previous = valid[-1]
            if sample.timer_s <= previous.timer_s or sample.distance_m < previous.distance_m:
                continue
            delta_time = sample.timer_s - previous.timer_s
            if sample.distance_m - previous.distance_m > delta_time * 12:
                continue
        valid.append(sample)
    return valid


def _sampled_detail_samples(activity: Activity) -> list[EvidenceSample]:
    payload = load_activity_detail_payload(
        activity.started_at, activity.garmin_activity_id, activity.user_id
    )
    if payload is None:
        return []
    parsed = parse_activity_detail_metrics(payload)
    rows = []
    for item in parsed:
        timer = _number(item.get("sumDuration"))
        elapsed = _number(item.get("sumElapsedDuration"))
        distance = _number(item.get("sumDistance"))
        if timer is None:
            timer = _number(item.get("sumMovingDuration"))
        if elapsed is None:
            elapsed = timer
        if timer is None or elapsed is None or distance is None:
            continue
        rows.append(
            EvidenceSample(
                timer,
                elapsed,
                distance,
                _number(item.get("directHeartRate")),
                _number(item.get("directElevation")),
            )
        )
    return _valid_samples(rows)


def _fit_samples(activity: Activity) -> list[EvidenceSample]:
    if activity.fit_file is None:
        return []
    path = Path(activity.fit_file)
    try:
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_FIT_BYTES:
            return []
        rows: list[EvidenceSample] = []
        first_timestamp: datetime | None = None
        previous_timestamp: datetime | None = None
        timer_s = 0.0
        timer_running = True
        with fitdecode.FitReader(path, check_crc=fitdecode.CrcCheck.WARN) as reader:
            for frame in reader:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                if frame.name == "event" and frame.get_value("event", fallback=None) == "timer":
                    timestamp = frame.get_value("timestamp", fallback=None)
                    if isinstance(timestamp, datetime):
                        if previous_timestamp is not None and timer_running:
                            timer_s += max((timestamp - previous_timestamp).total_seconds(), 0)
                        previous_timestamp = timestamp
                    event_type = frame.get_value("event_type", fallback=None)
                    timer_running = event_type in {"start", "start_disable_all"}
                    continue
                if frame.name != "record":
                    continue
                timestamp = frame.get_value("timestamp", fallback=None)
                distance = _number(frame.get_value("distance", fallback=None))
                if not isinstance(timestamp, datetime) or distance is None:
                    continue
                if first_timestamp is None:
                    first_timestamp = timestamp
                if previous_timestamp is not None and timer_running:
                    timer_s += max((timestamp - previous_timestamp).total_seconds(), 0)
                previous_timestamp = timestamp
                rows.append(
                    EvidenceSample(
                        timer_s,
                        max((timestamp - first_timestamp).total_seconds(), 0),
                        distance,
                        _number(frame.get_value("heart_rate", fallback=None)),
                        _number(
                            frame.get_value(
                                "enhanced_altitude",
                                fallback=frame.get_value("altitude", fallback=None),
                            )
                        ),
                    )
                )
        return _valid_samples(rows)
    except (OSError, ValueError, fitdecode.FitError):
        return []


def _interpolate_time(first: EvidenceSample, second: EvidenceSample, distance: float) -> float:
    span = second.distance_m - first.distance_m
    if span <= 0:
        return second.timer_s
    fraction = (distance - first.distance_m) / span
    return first.timer_s + fraction * (second.timer_s - first.timer_s)


def _rolling_effort(samples: list[EvidenceSample], target_m: float) -> tuple[float, int] | None:
    if len(samples) < 2 or samples[-1].distance_m - samples[0].distance_m < target_m:
        return None
    best: tuple[float, int] | None = None
    end_index = 1
    for start_index, start in enumerate(samples[:-1]):
        target = start.distance_m + target_m
        end_index = max(end_index, start_index + 1)
        while end_index < len(samples) and samples[end_index].distance_m < target:
            end_index += 1
        if end_index >= len(samples):
            break
        end_time = _interpolate_time(samples[end_index - 1], samples[end_index], target)
        duration = end_time - start.timer_s
        count = end_index - start_index + 1
        if duration > 0 and (best is None or duration < best[0]):
            best = (duration, count)
    return best


def _weighted_metrics(
    samples: list[EvidenceSample], start_s: float, end_s: float
) -> tuple[float, float | None, float, float | None, float | None, int] | None:
    speeds: list[tuple[float, float]] = []
    heart_rates: list[tuple[float, float]] = []
    elevations: list[float] = []
    for first, second in zip(samples, samples[1:], strict=False):
        interval_start = max(first.timer_s, start_s)
        interval_end = min(second.timer_s, end_s)
        duration = interval_end - interval_start
        if duration <= 0:
            continue
        distance_delta = second.distance_m - first.distance_m
        time_delta = second.timer_s - first.timer_s
        if time_delta <= 0 or distance_delta < 0:
            continue
        speed = distance_delta / time_delta
        if 0.5 <= speed <= 12:
            speeds.append((speed, duration))
        if first.heart_rate is not None and 60 <= first.heart_rate <= 230:
            heart_rates.append((first.heart_rate, duration))
        if first.elevation_m is not None:
            elevations.append(first.elevation_m)
    covered = sum(weight for _, weight in speeds)
    if covered < (end_s - start_s) * 0.8 or not speeds:
        return None
    mean_speed = sum(value * weight for value, weight in speeds) / covered
    speed_values = [value for value, _ in speeds]
    pace_cv = pstdev(speed_values) / mean_speed * 100 if len(speed_values) > 1 else 0.0
    hr_covered = sum(weight for _, weight in heart_rates)
    mean_hr = (
        sum(value * weight for value, weight in heart_rates) / hr_covered
        if hr_covered >= (end_s - start_s) * 0.8
        else None
    )
    elevation_gain = (
        sum(
            max(second - first, 0)
            for first, second in zip(elevations, elevations[1:], strict=False)
        )
        if len(elevations) >= 2
        else None
    )
    elevation_loss = (
        sum(
            max(first - second, 0)
            for first, second in zip(elevations, elevations[1:], strict=False)
        )
        if len(elevations) >= 2
        else None
    )
    return mean_speed, mean_hr, pace_cv, elevation_gain, elevation_loss, len(speeds)


def _latest_fitness_value(rows: list[DailyFitness], attribute: str) -> float | None:
    for row in reversed(rows):
        value = _number(getattr(row, attribute))
        if value is not None and value > 0:
            return value
    return None


def _file_stamp(path_value: str | None) -> tuple[str, int | None, int | None] | None:
    if path_value is None:
        return None
    path = Path(path_value)
    try:
        stat = path.stat()
        return str(path), stat.st_size, stat.st_mtime_ns
    except OSError:
        return str(path), None, None


def _evidence_cache_key(
    eligible: list[Activity],
    as_of: date,
    threshold_speed: float | None,
    threshold_hr: float | None,
) -> tuple[object, ...]:
    activity_sources = tuple(
        (
            activity.id,
            activity.user_id,
            activity.started_at,
            activity.activity_type,
            activity.distance_m,
            activity.duration_s,
            activity.source_fingerprint,
            activity.details_synced_at,
            activity.fit_synced_at,
            _file_stamp(activity.fit_file),
            _file_stamp(activity.details_file),
            any(split.split_type.startswith("typed_") for split in activity.splits),
        )
        for activity in eligible
    )
    return (
        DETAIL_EVIDENCE_FORMULA_VERSION,
        as_of,
        threshold_speed,
        threshold_hr,
        activity_sources,
    )


def clear_detail_evidence_cache() -> None:
    with _detail_evidence_cache_lock:
        _detail_evidence_cache.clear()


def analyze_detail_evidence(
    activities: list[Activity], fitness: list[DailyFitness], as_of: date
) -> DetailEvidence:
    eligible = [
        activity
        for activity in activities
        if fit_eligible_activity_type(activity.activity_type)
        and activity.started_at.date() <= as_of
        and (activity.distance_m or 0) >= 1_000
        and (activity.duration_s or 0) >= 600
    ]
    threshold_speed = _latest_fitness_value(fitness, "lactate_threshold_speed_mps")
    threshold_hr = _latest_fitness_value(fitness, "lactate_threshold_hr")
    cache_key = _evidence_cache_key(eligible, as_of, threshold_speed, threshold_hr)
    with _detail_evidence_cache_lock:
        cached = _detail_evidence_cache.get(cache_key)
        if cached is not None:
            _detail_evidence_cache.move_to_end(cache_key)
            return cached

    evidence = _analyze_detail_evidence(eligible, threshold_speed, threshold_hr)
    with _detail_evidence_cache_lock:
        _detail_evidence_cache[cache_key] = evidence
        _detail_evidence_cache.move_to_end(cache_key)
        while len(_detail_evidence_cache) > DETAIL_EVIDENCE_CACHE_SIZE:
            _detail_evidence_cache.popitem(last=False)
    return evidence


def _analyze_detail_evidence(
    eligible: list[Activity], threshold_speed: float | None, threshold_hr: float | None
) -> DetailEvidence:
    best: dict[str, DetailBestEffort] = {}
    drift_values: list[tuple[float, Activity, EvidenceSource]] = []
    threshold_segments: list[ThresholdSegmentEvidence] = []
    fit_count = 0
    detail_count = 0
    analyzed = 0

    for activity in eligible:
        source: EvidenceSource
        samples = _fit_samples(activity)
        if samples:
            source = "fit"
            fit_count += 1
        else:
            samples = _sampled_detail_samples(activity)
            if not samples:
                continue
            source = "sampled_detail"
            detail_count += 1
        analyzed += 1
        confidence = "high" if source == "fit" else "medium"

        for key, target in (("1k", 1_000.0), ("5k", 5_000.0), ("10k", 10_000.0)):
            effort = _rolling_effort(samples, target)
            if effort is None:
                continue
            duration, sample_count = effort
            candidate = DetailBestEffort(
                key,
                target,
                round(duration, 2),
                activity.id,
                activity.started_at.date(),
                source,
                sample_count,
                confidence,
            )
            if key not in best or candidate.duration_s < best[key].duration_s:
                best[key] = candidate

        total_timer = samples[-1].timer_s - samples[0].timer_s
        if total_timer >= 2_700:
            start = samples[0].timer_s + total_timer * 0.1
            midpoint = samples[0].timer_s + total_timer * 0.5
            end = samples[0].timer_s + total_timer * 0.9
            first = _weighted_metrics(samples, start, midpoint)
            second = _weighted_metrics(samples, midpoint, end)
            if (
                first
                and second
                and first[1]
                and second[1]
                and max(first[2], second[2]) <= 10
                and 0.95 <= second[0] / first[0] <= 1.05
                and not any(split.split_type.startswith("typed_") for split in activity.splits)
            ):
                first_efficiency = first[0] / first[1]
                second_efficiency = second[0] / second[1]
                drift = (first_efficiency / second_efficiency - 1) * 100
                if -10 <= drift <= 30:
                    drift_values.append((drift, activity, source))

        if total_timer >= 900 and (threshold_speed is not None or threshold_hr is not None):
            window = min(2_400.0, total_timer)
            step = 300.0
            position = samples[0].timer_s
            best_segment: ThresholdSegmentEvidence | None = None
            while position + 900 <= samples[-1].timer_s:
                duration = min(window, samples[-1].timer_s - position)
                if duration < 900:
                    break
                metrics = _weighted_metrics(samples, position, position + duration)
                position += step
                if metrics is None:
                    continue
                speed, heart_rate, pace_cv, elevation_gain, elevation_loss, sample_count = metrics
                speed_matches = (
                    threshold_speed is not None and 0.9 <= speed / threshold_speed <= 1.1
                )
                hr_matches = (
                    threshold_hr is not None
                    and heart_rate is not None
                    and abs(heart_rate - threshold_hr) <= 5
                )
                if not (speed_matches or hr_matches) or pace_cv > 5:
                    continue
                candidate = ThresholdSegmentEvidence(
                    activity.id,
                    activity.started_at.date(),
                    round(duration, 2),
                    round(1_000 / speed, 2),
                    round(heart_rate, 1) if heart_rate is not None else None,
                    round(pace_cv, 1),
                    round(elevation_gain, 1) if elevation_gain is not None else None,
                    round(elevation_loss, 1) if elevation_loss is not None else None,
                    source,
                    sample_count,
                    confidence,
                )
                if best_segment is None or candidate.duration_s > best_segment.duration_s:
                    best_segment = candidate
            if best_segment is not None:
                threshold_segments.append(best_segment)

    drift_sufficient = len(drift_values) >= 3
    drift_sources = {source for _, _, source in drift_values}
    drift_confidence = (
        "high"
        if drift_sufficient and drift_sources == {"fit"}
        else "medium"
        if drift_sufficient
        else "insufficient"
    )
    return DetailEvidence(
        formula_version=DETAIL_EVIDENCE_FORMULA_VERSION,
        coverage=DetailEvidenceCoverage(
            eligible_activities=len(eligible),
            analyzed_activities=analyzed,
            fit_activities=fit_count,
            sampled_detail_activities=detail_count,
            unavailable_activities=len(eligible) - analyzed,
        ),
        best_efforts=tuple(best[key] for key in ("1k", "5k", "10k") if key in best),
        heart_rate_drift=HeartRateDriftEvidence(
            sufficient=drift_sufficient,
            median_percent=(
                round(float(median(value for value, _, _ in drift_values)), 1)
                if drift_sufficient
                else None
            ),
            sample_sessions=len(drift_values),
            source_activity_ids=tuple(activity.id for _, activity, _ in drift_values),
            latest_on=(
                max(activity.started_at.date() for _, activity, _ in drift_values)
                if drift_values
                else None
            ),
            confidence=drift_confidence,
        ),
        threshold_segments=tuple(
            sorted(threshold_segments, key=lambda item: item.occurred_on, reverse=True)[:5]
        ),
    )
