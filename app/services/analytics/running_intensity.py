import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.models import DailyFitness
from app.repositories.fitness import fitness_on_or_before
from app.services.analytics.running_baseline import RunningBaseline

RUNNING_INTENSITY_VERSION = "1.0"
THRESHOLD_FRESH_DAYS = 56
PERFORMANCE_ANCHOR_FRESH_DAYS = 180
MIN_RUNNING_SPEED_MPS = 1.5
MAX_RUNNING_SPEED_MPS = 8.0


@dataclass(frozen=True)
class RpeTalkTestBand:
    key: str
    rpe_min: int
    rpe_max: int
    talk_test: str


@dataclass(frozen=True)
class IntensitySource:
    key: str
    source: str
    source_day: date
    age_days: int
    value: float
    unit: str
    role: str


@dataclass(frozen=True)
class PerformanceAnchorInput:
    kind: str
    achieved_on: date
    distance_m: float
    duration_s: float
    reliable: bool = True


class PerformanceAnchorLike(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def achieved_on(self) -> date: ...

    @property
    def distance_m(self) -> float: ...

    @property
    def duration_s(self) -> float: ...

    @property
    def reliable(self) -> bool: ...


@dataclass(frozen=True)
class PaceAnchor:
    kind: str
    source: str
    source_day: date
    age_days: int
    speed_mps: float
    pace_seconds_per_km: float
    reference_distance_m: float | None
    reference_duration_s: float | None


@dataclass(frozen=True)
class CriticalSpeedResult:
    speed_mps: float | None
    d_prime_m: float | None
    available: bool
    reason: str


@dataclass(frozen=True)
class RunningIntensityGuidance:
    as_of: date
    intensity_version: str
    mode: str
    confidence: str
    primary_source: str
    pace_anchor: PaceAnchor | None
    rpe_talk_test_bands: tuple[RpeTalkTestBand, ...]
    secondary_context: tuple[IntensitySource, ...]
    critical_speed: CriticalSpeedResult
    warnings: tuple[str, ...]
    input_fingerprint: str


@dataclass(frozen=True)
class RunningShadowAnalysis:
    baseline: RunningBaseline
    intensity: RunningIntensityGuidance
    generation_context: dict[str, Any]
    context_fingerprint: str


RPE_TALK_TEST_BANDS = (
    RpeTalkTestBand("easy", 2, 3, "Vollstaendige Saetze sind problemlos moeglich."),
    RpeTalkTestBand("steady", 4, 5, "Kurze Unterhaltung ist weiterhin moeglich."),
    RpeTalkTestBand("threshold", 6, 7, "Nur kurze Saetze oder einzelne Phrasen sind moeglich."),
    RpeTalkTestBand("hard", 8, 9, "Nur einzelne Worte sind moeglich."),
)


SecondarySpec = tuple[str, str, Callable[[DailyFitness], float | int | None]]
SECONDARY_SPECS: tuple[SecondarySpec, ...] = (
    ("race_5k", "s", lambda row: row.race_prediction_5k_seconds),
    ("race_10k", "s", lambda row: row.race_prediction_10k_seconds),
    ("race_half", "s", lambda row: row.race_prediction_half_seconds),
    ("race_marathon", "s", lambda row: row.race_prediction_marathon_seconds),
    ("vo2max", "ml/kg/min", lambda row: row.vo2max),
    ("endurance_score", "points", lambda row: row.endurance_score),
    ("hill_score", "points", lambda row: row.hill_score),
)


def _positive(value: float | int | None) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) and result > 0 else None


def _plausible_running_speed(value: float | int | None) -> float | None:
    speed = _positive(value)
    if speed is None or not MIN_RUNNING_SPEED_MPS <= speed <= MAX_RUNNING_SPEED_MPS:
        return None
    return speed


def _latest_value(
    rows: list[DailyFitness], extract: Callable[[DailyFitness], float | int | None]
) -> tuple[DailyFitness, float] | None:
    for row in reversed(rows):
        if (value := _positive(extract(row))) is not None:
            return row, value
    return None


def _latest_threshold(rows: list[DailyFitness]) -> tuple[DailyFitness, float] | None:
    for row in reversed(rows):
        if (value := _plausible_running_speed(row.lactate_threshold_speed_mps)) is not None:
            return row, value
    return None


def _valid_performance_anchors(
    anchors: tuple[PerformanceAnchorLike, ...], as_of: date
) -> tuple[list[tuple[PerformanceAnchorLike, float]], tuple[str, ...]]:
    valid: list[tuple[PerformanceAnchorLike, float]] = []
    warnings: list[str] = []
    allowed_kinds = {"manual", "race", "time_trial"}
    for anchor in anchors:
        distance = _positive(anchor.distance_m)
        duration = _positive(anchor.duration_s)
        if anchor.kind not in allowed_kinds or distance is None or duration is None:
            warnings.append("invalid_performance_anchor")
            continue
        if not anchor.reliable:
            warnings.append("unreliable_performance_anchor_ignored")
            continue
        age = (as_of - anchor.achieved_on).days
        if age < 0 or age > PERFORMANCE_ANCHOR_FRESH_DAYS:
            warnings.append("performance_anchor_outside_180_day_window")
            continue
        speed = _plausible_running_speed(distance / duration)
        if speed is None:
            warnings.append("implausible_performance_anchor_ignored")
            continue
        valid.append((anchor, speed))
    kind_priority = {"race": 0, "time_trial": 1, "manual": 2}
    valid.sort(key=lambda item: (-item[0].achieved_on.toordinal(), kind_priority[item[0].kind]))
    return valid, tuple(dict.fromkeys(warnings))


def _critical_speed(
    anchors: list[tuple[PerformanceAnchorLike, float]], baseline_confidence: str
) -> CriticalSpeedResult:
    if baseline_confidence not in {"high", "medium"}:
        return CriticalSpeedResult(
            speed_mps=None,
            d_prime_m=None,
            available=False,
            reason="running_baseline_confidence_too_low",
        )
    by_duration = sorted(anchors, key=lambda item: item[0].duration_s)
    if len(by_duration) < 2:
        return CriticalSpeedResult(
            speed_mps=None,
            d_prime_m=None,
            available=False,
            reason="suitable_multi_distance_performance_anchors_unavailable",
        )
    short = by_duration[0][0]
    long = by_duration[-1][0]
    duration_delta = long.duration_s - short.duration_s
    distance_delta = long.distance_m - short.distance_m
    if duration_delta <= 0 or distance_delta <= 0:
        return CriticalSpeedResult(None, None, False, "performance_anchors_inconsistent")
    speed = distance_delta / duration_delta
    d_prime = short.distance_m - speed * short.duration_s
    if _plausible_running_speed(speed) is None or not math.isfinite(d_prime) or d_prime < 0:
        return CriticalSpeedResult(None, None, False, "performance_anchors_inconsistent")
    return CriticalSpeedResult(
        speed_mps=round(speed, 3),
        d_prime_m=round(d_prime, 1),
        available=True,
        reason="reliable_multi_distance_performance_anchors",
    )


def _secondary_context(rows: list[DailyFitness], as_of: date) -> tuple[IntensitySource, ...]:
    context: list[IntensitySource] = []
    for key, unit, extract in SECONDARY_SPECS:
        latest = _latest_value(rows, extract)
        if latest is None:
            continue
        row, value = latest
        context.append(
            IntensitySource(
                key=key,
                source="garmin_wearable",
                source_day=row.day,
                age_days=(as_of - row.day).days,
                value=round(value, 2),
                unit=unit,
                role="secondary_context_only",
            )
        )
    return tuple(context)


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_safe(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload, allow_nan=False, default=_json_default))


def get_running_intensity_guidance(
    session: Session,
    user_id: int,
    baseline: RunningBaseline,
    *,
    as_of: date | None = None,
    performance_anchors: tuple[PerformanceAnchorLike, ...] = (),
) -> RunningIntensityGuidance:
    end = as_of or baseline.as_of
    if end != baseline.as_of:
        raise ValueError("intensity guidance and running baseline must use the same as_of date")
    rows = fitness_on_or_before(session, user_id, end)
    threshold = _latest_threshold(rows)
    secondary = _secondary_context(rows, end)
    baseline_confidence = baseline.window(56).quality.confidence
    valid_anchors, anchor_warnings = _valid_performance_anchors(performance_anchors, end)
    warnings = list(anchor_warnings)
    pace_anchor: PaceAnchor | None = None

    if valid_anchors and baseline_confidence in {"high", "medium"}:
        anchor, speed = valid_anchors[0]
        pace_anchor = PaceAnchor(
            kind=anchor.kind,
            source=f"{anchor.kind}_performance",
            source_day=anchor.achieved_on,
            age_days=(end - anchor.achieved_on).days,
            speed_mps=round(speed, 3),
            pace_seconds_per_km=round(1000 / speed, 1),
            reference_distance_m=round(anchor.distance_m, 1),
            reference_duration_s=round(anchor.duration_s, 1),
        )
    elif valid_anchors:
        warnings.append("running_baseline_confidence_too_low_for_pace")
    elif threshold is not None:
        threshold_row, threshold_speed = threshold
        threshold_age = (end - threshold_row.day).days
        if threshold_age > THRESHOLD_FRESH_DAYS:
            warnings.append("lactate_threshold_older_than_56_days")
        elif baseline_confidence in {"high", "medium"}:
            pace_anchor = PaceAnchor(
                kind="lactate_threshold",
                source="garmin_lactate_threshold",
                source_day=threshold_row.day,
                age_days=threshold_age,
                speed_mps=round(threshold_speed, 3),
                pace_seconds_per_km=round(1000 / threshold_speed, 1),
                reference_distance_m=None,
                reference_duration_s=None,
            )
        else:
            warnings.append("running_baseline_confidence_too_low_for_pace")
    else:
        warnings.append("lactate_threshold_unavailable")
    if not valid_anchors:
        warnings.append("manual_or_race_anchor_unavailable")

    if secondary:
        warnings.append("wearable_predictions_and_scores_are_secondary_only")
    if pace_anchor is not None:
        mode = "pace_anchor"
        confidence = (
            "high" if baseline_confidence == "high" and pace_anchor.age_days <= 28 else "medium"
        )
        primary_source = pace_anchor.source
    elif baseline_confidence == "insufficient":
        mode = "clarify"
        confidence = "insufficient"
        primary_source = "athlete_clarification"
    else:
        mode = "rpe_talk_test"
        confidence = "low" if baseline_confidence == "low" else "medium"
        primary_source = "rpe_talk_test"

    input_payload = {
        "as_of": end,
        "intensity_version": RUNNING_INTENSITY_VERSION,
        "baseline_input_fingerprint": baseline.input_fingerprint,
        "baseline_confidence_56d": baseline_confidence,
        "threshold": (
            {
                "day": threshold[0].day,
                "speed_mps": threshold[1],
            }
            if threshold
            else None
        ),
        "performance_anchors": [
            {
                "kind": anchor.kind,
                "achieved_on": anchor.achieved_on,
                "distance_m": (
                    anchor.distance_m if math.isfinite(anchor.distance_m) else "non_finite"
                ),
                "duration_s": (
                    anchor.duration_s if math.isfinite(anchor.duration_s) else "non_finite"
                ),
                "reliable": anchor.reliable,
            }
            for anchor in sorted(
                performance_anchors,
                key=lambda item: (
                    item.kind,
                    item.achieved_on,
                    repr(item.distance_m),
                    repr(item.duration_s),
                    item.reliable,
                ),
            )
        ],
        "secondary_context": [asdict(source) for source in secondary],
    }
    return RunningIntensityGuidance(
        as_of=end,
        intensity_version=RUNNING_INTENSITY_VERSION,
        mode=mode,
        confidence=confidence,
        primary_source=primary_source,
        pace_anchor=pace_anchor,
        rpe_talk_test_bands=RPE_TALK_TEST_BANDS,
        secondary_context=secondary,
        critical_speed=_critical_speed(valid_anchors, baseline_confidence),
        warnings=tuple(warnings),
        input_fingerprint=_fingerprint(input_payload),
    )


def _generation_context(
    baseline: RunningBaseline, intensity: RunningIntensityGuidance
) -> dict[str, Any]:
    return _json_safe(
        {
            "schema_version": "running_generation_context.v1",
            "as_of": baseline.as_of,
            "input_fingerprint": intensity.input_fingerprint,
            "baseline": {
                "version": baseline.baseline_version,
                "windows": {
                    str(window.days): {
                        "runs": window.runs,
                        "frequency_per_week": window.frequency_per_week,
                        "total_duration_s": window.total_duration_s,
                        "total_distance_m": window.total_distance_m,
                        "weekly_runs": asdict(window.weekly_runs),
                        "weekly_duration_s": asdict(window.weekly_duration_s),
                        "weekly_distance_m": asdict(window.weekly_distance_m),
                        "weekly_longest_duration_s": asdict(window.weekly_longest_duration_s),
                        "weekly_longest_distance_m": asdict(window.weekly_longest_distance_m),
                        "longest_duration": asdict(window.longest_duration),
                        "longest_distance": asdict(window.longest_distance),
                        "hard_days": window.hard_days,
                        "quality_density_percent": window.quality_density_percent,
                        "total_srpe": window.total_srpe,
                        "confidence": window.quality.confidence,
                        "confidence_reasons": window.quality.confidence_reasons,
                        "data_quality": {
                            "source": window.quality.source,
                            "sync_status": window.quality.sync_status,
                            "history_complete": window.quality.history_complete,
                            "history_coverage_percent": (window.quality.history_coverage_percent),
                            "sync_age_days": window.quality.sync_age_days,
                            "latest_run_age_days": window.quality.latest_run_age_days,
                            "duration": asdict(window.quality.duration),
                            "distance": asdict(window.quality.distance),
                            "rpe": asdict(window.quality.rpe),
                            "srpe": asdict(window.quality.srpe),
                            "hard_classification": asdict(window.quality.hard_classification),
                        },
                    }
                    for window in baseline.windows
                },
                "interruptions": [asdict(item) for item in baseline.interruptions],
                "reentry": asdict(baseline.reentry),
                "latest_distance_spike": (
                    asdict(baseline.latest_distance_spike)
                    if baseline.latest_distance_spike
                    else None
                ),
            },
            "intensity": {
                "version": intensity.intensity_version,
                "mode": intensity.mode,
                "confidence": intensity.confidence,
                "primary_source": intensity.primary_source,
                "pace_anchor": asdict(intensity.pace_anchor) if intensity.pace_anchor else None,
                "secondary_context": [asdict(item) for item in intensity.secondary_context],
                "critical_speed": asdict(intensity.critical_speed),
                "warnings": intensity.warnings,
            },
        }
    )


def build_running_shadow_analysis(
    session: Session,
    user_id: int,
    baseline: RunningBaseline,
    *,
    performance_anchors: tuple[PerformanceAnchorLike, ...] = (),
) -> RunningShadowAnalysis:
    intensity = get_running_intensity_guidance(
        session,
        user_id,
        baseline,
        as_of=baseline.as_of,
        performance_anchors=performance_anchors,
    )
    context = _generation_context(baseline, intensity)
    return RunningShadowAnalysis(
        baseline=baseline,
        intensity=intensity,
        generation_context=context,
        context_fingerprint=_fingerprint(context),
    )
