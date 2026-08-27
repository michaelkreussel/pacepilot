from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal, get_args

from sqlalchemy.orm import Session

from app.models import DailyFitness, DailyHealth
from app.repositories.activities import activities_between
from app.repositories.fitness import fitness_between
from app.repositories.health import health_metrics_between, latest_health_on_or_before
from app.repositories.sync_state import sync_states_for_user
from app.services.analytics.subjective_feedback import effective_activity_feedback

BASELINE_DAYS = 84
BASELINE_GAP_DAYS = 7
HealthMetric = Literal[
    "resting_hr",
    "hrv",
    "sleep_duration",
    "sleep_need",
    "sleep_score",
    "stress",
    "body_battery_high",
    "body_battery_charged",
    "garmin_training_readiness",
    "recovery_time",
    "vo2max",
    "training_load",
    "acute_load",
    "chronic_load",
]
HEALTH_METRICS: tuple[HealthMetric, ...] = get_args(HealthMetric)


@dataclass(frozen=True)
class TrendPoint:
    day: date
    value: float


@dataclass(frozen=True)
class MetricTrend:
    metric: HealthMetric
    unit: str
    current: float | None
    current_day: date | None
    average_7d: float | None
    average_28d: float | None
    personal_baseline: float | None
    difference_from_baseline: float | None
    sample_count: int
    baseline_sample_count: int
    points: tuple[TrendPoint, ...]


@dataclass(frozen=True)
class ResourceCoverage:
    resource: str
    status: str
    backfill_complete: bool
    oldest_synced_date: date | None
    newest_synced_date: date | None


@dataclass(frozen=True)
class HealthTrends:
    start: date
    end: date
    resting_hr: MetricTrend
    hrv: MetricTrend
    sleep_duration: MetricTrend
    sleep_need: MetricTrend
    sleep_score: MetricTrend
    stress: MetricTrend
    body_battery_high: MetricTrend
    body_battery_charged: MetricTrend
    garmin_training_readiness: MetricTrend
    recovery_time: MetricTrend
    vo2max: MetricTrend
    training_load: MetricTrend
    acute_load: MetricTrend
    chronic_load: MetricTrend
    coverage: tuple[ResourceCoverage, ...]


@dataclass(frozen=True)
class HrvBaseline:
    trend: MetricTrend
    garmin_status: str | None
    garmin_baseline_low: float | None
    garmin_balanced_low: float | None
    garmin_balanced_high: float | None


@dataclass(frozen=True)
class ReadinessComponent:
    component: str
    score: float
    normalized_weight: float
    current: float
    baseline: float | None
    unit: str


@dataclass(frozen=True)
class RecoveryState:
    as_of: date
    health_day: date | None
    fitness_day: date | None
    resting_hr: int | None
    hrv_average: float | None
    hrv_status: str | None
    sleep_seconds: int | None
    sleep_need_seconds: int | None
    sleep_score: int | None
    stress_average: int | None
    body_battery_high: int | None
    body_battery_low: int | None
    garmin_training_readiness_score: int | None
    garmin_training_readiness_level: str | None
    garmin_training_readiness_day: date | None
    recovery_time_minutes: int | None
    recovery_time_day: date | None
    vo2max: float | None
    vo2max_day: date | None
    training_status: str | None
    training_status_day: date | None
    training_load: float | None
    training_load_day: date | None
    acute_load: float | None
    acute_load_day: date | None
    chronic_load: float | None
    chronic_load_day: date | None
    load_ratio: float | None
    load_ratio_day: date | None
    pacepilot_readiness_formula_version: str
    pacepilot_readiness_limiter: str | None
    pacepilot_readiness_score: float | None
    pacepilot_readiness_label: str | None
    pacepilot_readiness_confidence: float
    pacepilot_readiness_components: tuple[ReadinessComponent, ...]


@dataclass(frozen=True)
class PreferredReadiness:
    source: Literal["garmin", "pacepilot"]
    score: float
    label: str | None
    day: date
    confidence: float | None


def _average(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _cap_date(value: date | None, end: date) -> date | None:
    return min(value, end) if value is not None else None


def _metric_trend[Row: DailyHealth | DailyFitness](
    rows: Sequence[Row],
    *,
    metric: HealthMetric,
    unit: str,
    value: Callable[[Row], int | float | None],
    days: int,
    as_of: date,
) -> MetricTrend:
    start = as_of - timedelta(days=days - 1)
    seven_start = as_of - timedelta(days=6)
    twenty_eight_start = as_of - timedelta(days=27)
    baseline_end = as_of - timedelta(days=BASELINE_GAP_DAYS)
    baseline_start = baseline_end - timedelta(days=BASELINE_DAYS - 1)
    valued_rows = [(row.day, float(item)) for row in rows if (item := value(row)) is not None]
    current_rows = [(day, item) for day, item in valued_rows if day >= start]
    current_day, current = current_rows[-1] if current_rows else (None, None)
    baseline = _average(
        [item for day, item in valued_rows if baseline_start <= day <= baseline_end]
    )
    return MetricTrend(
        metric=metric,
        unit=unit,
        current=current,
        current_day=current_day,
        average_7d=_average([item for day, item in valued_rows if seven_start <= day <= as_of]),
        average_28d=_average(
            [item for day, item in valued_rows if twenty_eight_start <= day <= as_of]
        ),
        personal_baseline=baseline,
        difference_from_baseline=(
            round(current - baseline, 2) if current is not None and baseline is not None else None
        ),
        sample_count=len(current_rows),
        baseline_sample_count=sum(
            1 for day, _ in valued_rows if baseline_start <= day <= baseline_end
        ),
        points=tuple(TrendPoint(day, item) for day, item in current_rows),
    )


def get_health_trends(
    session: Session, user_id: int, *, days: int = 28, as_of: date | None = None
) -> HealthTrends:
    if days < 1:
        raise ValueError("days must be at least 1")
    end = as_of or date.today()
    start = end - timedelta(days=days - 1)
    query_start = min(start, end - timedelta(days=BASELINE_DAYS + BASELINE_GAP_DAYS - 1))
    health = health_metrics_between(session, user_id, query_start, end)
    fitness = fitness_between(session, user_id, query_start, end)
    states = {state.resource: state for state in sync_states_for_user(session, user_id)}
    return HealthTrends(
        start=start,
        end=end,
        resting_hr=_metric_trend(
            health,
            metric="resting_hr",
            unit="bpm",
            value=lambda row: row.resting_hr,
            days=days,
            as_of=end,
        ),
        hrv=_metric_trend(
            health, metric="hrv", unit="ms", value=lambda row: row.hrv_average, days=days, as_of=end
        ),
        sleep_duration=_metric_trend(
            health,
            metric="sleep_duration",
            unit="seconds",
            value=lambda row: row.sleep_seconds,
            days=days,
            as_of=end,
        ),
        sleep_need=_metric_trend(
            health,
            metric="sleep_need",
            unit="seconds",
            value=lambda row: row.sleep_need_seconds,
            days=days,
            as_of=end,
        ),
        sleep_score=_metric_trend(
            health,
            metric="sleep_score",
            unit="garmin_score",
            value=lambda row: row.sleep_score,
            days=days,
            as_of=end,
        ),
        stress=_metric_trend(
            health,
            metric="stress",
            unit="garmin_score",
            value=lambda row: row.stress_average,
            days=days,
            as_of=end,
        ),
        body_battery_high=_metric_trend(
            health,
            metric="body_battery_high",
            unit="garmin_score",
            value=lambda row: row.body_battery_high,
            days=days,
            as_of=end,
        ),
        body_battery_charged=_metric_trend(
            health,
            metric="body_battery_charged",
            unit="garmin_score",
            value=lambda row: row.body_battery_charged,
            days=days,
            as_of=end,
        ),
        garmin_training_readiness=_metric_trend(
            fitness,
            metric="garmin_training_readiness",
            unit="garmin_score",
            value=lambda row: row.garmin_training_readiness_score,
            days=days,
            as_of=end,
        ),
        recovery_time=_metric_trend(
            fitness,
            metric="recovery_time",
            unit="minutes",
            value=lambda row: row.recovery_time_minutes,
            days=days,
            as_of=end,
        ),
        vo2max=_metric_trend(
            fitness,
            metric="vo2max",
            unit="ml/kg/min",
            value=lambda row: row.vo2max,
            days=days,
            as_of=end,
        ),
        training_load=_metric_trend(
            fitness,
            metric="training_load",
            unit="garmin_load",
            value=lambda row: row.training_load,
            days=days,
            as_of=end,
        ),
        acute_load=_metric_trend(
            fitness,
            metric="acute_load",
            unit="garmin_load",
            value=lambda row: row.acute_load,
            days=days,
            as_of=end,
        ),
        chronic_load=_metric_trend(
            fitness,
            metric="chronic_load",
            unit="garmin_load",
            value=lambda row: row.chronic_load,
            days=days,
            as_of=end,
        ),
        coverage=tuple(
            ResourceCoverage(
                resource=resource,
                status=states[resource].status if resource in states else "not_synced",
                backfill_complete=states[resource].backfill_complete
                if resource in states
                else False,
                oldest_synced_date=(
                    states[resource].oldest_synced_date if resource in states else None
                ),
                newest_synced_date=(
                    _cap_date(states[resource].newest_synced_date, end)
                    if resource in states
                    else None
                ),
            )
            for resource in (
                "daily_summary",
                "sleep",
                "hrv",
                "body_battery",
                "training_readiness",
                "training_status",
                "vo2max",
            )
        ),
    )


def get_current_recovery_state(
    session: Session, user_id: int, *, as_of: date | None = None
) -> RecoveryState:
    end = as_of or date.today()
    health = latest_health_on_or_before(session, user_id, end)
    fitness_rows = fitness_between(session, user_id, end - timedelta(days=364), end)
    fitness = fitness_rows[-1] if fitness_rows else None
    readiness_row = next(
        (row for row in reversed(fitness_rows) if row.garmin_training_readiness_score is not None),
        None,
    )
    readiness = readiness_row.garmin_training_readiness_score if readiness_row else None
    readiness_level = readiness_row.garmin_training_readiness_level if readiness_row else None
    readiness_day = readiness_row.day if readiness_row else None
    recovery_time, recovery_time_day = _latest_fitness_value(
        fitness_rows, lambda row: row.recovery_time_minutes
    )
    vo2max, vo2max_day = _latest_fitness_value(fitness_rows, lambda row: row.vo2max)
    training_status, training_status_day = _latest_fitness_value(
        fitness_rows, lambda row: row.training_status
    )
    training_load, training_load_day = _latest_fitness_value(
        fitness_rows, lambda row: row.training_load
    )
    acute_load, acute_load_day = _latest_fitness_value(fitness_rows, lambda row: row.acute_load)
    chronic_load, chronic_load_day = _latest_fitness_value(
        fitness_rows, lambda row: row.chronic_load
    )
    load_ratio, load_ratio_day = _latest_fitness_value(fitness_rows, lambda row: row.load_ratio)
    has_current_garmin_readiness = (
        readiness is not None and 0 <= readiness <= 100 and readiness_day == end
    )
    if has_current_garmin_readiness:
        score, label, confidence, components, limiter = None, None, 0.0, (), None
    else:
        trends = get_health_trends(session, user_id, days=28, as_of=end)
        score, label, confidence, components, limiter = _pacepilot_readiness(
            session, user_id, end, health, trends
        )
    return RecoveryState(
        as_of=end,
        health_day=health.day if health else None,
        fitness_day=fitness.day if fitness else None,
        resting_hr=health.resting_hr if health else None,
        hrv_average=health.hrv_average if health else None,
        hrv_status=health.hrv_status if health else None,
        sleep_seconds=health.sleep_seconds if health else None,
        sleep_need_seconds=health.sleep_need_seconds if health else None,
        sleep_score=health.sleep_score if health else None,
        stress_average=health.stress_average if health else None,
        body_battery_high=health.body_battery_high if health else None,
        body_battery_low=health.body_battery_low if health else None,
        garmin_training_readiness_score=readiness,
        garmin_training_readiness_level=readiness_level,
        garmin_training_readiness_day=readiness_day,
        recovery_time_minutes=recovery_time,
        recovery_time_day=recovery_time_day,
        vo2max=vo2max,
        vo2max_day=vo2max_day,
        training_status=training_status,
        training_status_day=training_status_day,
        training_load=training_load,
        training_load_day=training_load_day,
        acute_load=acute_load,
        acute_load_day=acute_load_day,
        chronic_load=chronic_load,
        chronic_load_day=chronic_load_day,
        load_ratio=load_ratio,
        load_ratio_day=load_ratio_day,
        pacepilot_readiness_formula_version="2.0",
        pacepilot_readiness_limiter=limiter,
        pacepilot_readiness_score=score,
        pacepilot_readiness_label=label,
        pacepilot_readiness_confidence=confidence,
        pacepilot_readiness_components=components,
    )


def preferred_readiness(recovery: RecoveryState) -> PreferredReadiness | None:
    garmin_score = recovery.garmin_training_readiness_score
    if (
        garmin_score is not None
        and 0 <= garmin_score <= 100
        and recovery.garmin_training_readiness_day == recovery.as_of
    ):
        return PreferredReadiness(
            source="garmin",
            score=float(garmin_score),
            label=recovery.garmin_training_readiness_level,
            day=recovery.as_of,
            confidence=None,
        )
    if recovery.pacepilot_readiness_score is not None:
        return PreferredReadiness(
            source="pacepilot",
            score=recovery.pacepilot_readiness_score,
            label=recovery.pacepilot_readiness_label,
            day=recovery.as_of,
            confidence=recovery.pacepilot_readiness_confidence,
        )
    return None


def _latest_fitness_value[T](
    rows: Sequence[DailyFitness], value: Callable[[DailyFitness], T | None]
) -> tuple[T | None, date | None]:
    for row in reversed(rows):
        item = value(row)
        if item is not None:
            return item, row.day
    return None, None


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _sleep_ratio_cap(ratio: float) -> float | None:
    if ratio <= 0.65:
        return 44.0
    if ratio < 0.75:
        return 64.0
    if ratio < 0.85:
        return 79.0
    return None


def _readiness_sleep_cap(
    as_of: date, health: DailyHealth, trends: HealthTrends
) -> tuple[float | None, str | None]:
    caps: list[tuple[float, str]] = []
    sleep_baseline = health.sleep_need_seconds or trends.sleep_duration.personal_baseline
    if (
        health.sleep_seconds is not None
        and sleep_baseline
        and (cap := _sleep_ratio_cap(health.sleep_seconds / sleep_baseline)) is not None
    ):
        caps.append((cap, "sleep_duration"))
    if health.sleep_score is not None:
        if health.sleep_score < 50:
            caps.append((44.0, "garmin_sleep_score"))
        elif health.sleep_score < 60:
            caps.append((64.0, "garmin_sleep_score"))
        elif health.sleep_score < 70:
            caps.append((79.0, "garmin_sleep_score"))

    recent_start = as_of - timedelta(days=2)
    needs = {point.day: point.value for point in trends.sleep_need.points}
    recent_pairs = [
        (point.value, needs.get(point.day) or trends.sleep_duration.personal_baseline)
        for point in trends.sleep_duration.points
        if recent_start <= point.day <= as_of
        and (needs.get(point.day) or trends.sleep_duration.personal_baseline)
    ]
    if len(recent_pairs) >= 2:
        duration_total = sum(duration for duration, _ in recent_pairs)
        need_total = sum(need for _, need in recent_pairs if need is not None)
        if need_total and (cap := _sleep_ratio_cap(duration_total / need_total)) is not None:
            caps.append((cap, "recent_sleep_debt"))

    return min(caps, default=(None, None), key=lambda item: item[0])


def _pacepilot_readiness(
    session: Session,
    user_id: int,
    as_of: date,
    health: DailyHealth | None,
    trends: HealthTrends,
) -> tuple[
    float | None,
    str | None,
    float,
    tuple[ReadinessComponent, ...],
    str | None,
]:
    weighted: list[tuple[str, float, float, float, float | None, str, float]] = []
    health_is_current = health is not None and (as_of - health.day).days <= 2
    if health_is_current and health is not None:
        sleep_baseline = health.sleep_need_seconds or trends.sleep_duration.personal_baseline
        if health.sleep_seconds is not None and sleep_baseline:
            weighted.append(
                (
                    "sleep_duration",
                    _clamp(health.sleep_seconds / sleep_baseline * 100),
                    0.25,
                    float(health.sleep_seconds),
                    float(sleep_baseline),
                    "seconds",
                    (
                        1.0
                        if health.sleep_need_seconds
                        else min(trends.sleep_duration.baseline_sample_count / 28, 1)
                    ),
                )
            )
        if health.sleep_score is not None:
            weighted.append(
                (
                    "garmin_sleep_score",
                    _clamp(health.sleep_score),
                    0.15,
                    health.sleep_score,
                    None,
                    "garmin_score",
                    1.0,
                )
            )
        personal_hrv_baseline = trends.hrv.personal_baseline
        garmin_hrv_baseline = (
            (health.hrv_baseline_balanced_low + health.hrv_baseline_balanced_high) / 2
            if health.hrv_baseline_balanced_low is not None
            and health.hrv_baseline_balanced_high is not None
            else None
        )
        hrv_baseline = personal_hrv_baseline or garmin_hrv_baseline
        if health.hrv_average is not None and hrv_baseline:
            weighted.append(
                (
                    "hrv",
                    _clamp(75 + 100 * (health.hrv_average / hrv_baseline - 1)),
                    0.20,
                    health.hrv_average,
                    hrv_baseline,
                    "ms",
                    (
                        min(trends.hrv.baseline_sample_count / 28, 1)
                        if personal_hrv_baseline is not None
                        else 1.0
                    ),
                )
            )
        resting_baseline = trends.resting_hr.personal_baseline
        if health.resting_hr is not None and resting_baseline is not None:
            weighted.append(
                (
                    "resting_hr",
                    _clamp(75 - 5 * (health.resting_hr - resting_baseline)),
                    0.15,
                    health.resting_hr,
                    resting_baseline,
                    "bpm",
                    min(trends.resting_hr.baseline_sample_count / 28, 1),
                )
            )
        if health.stress_average is not None:
            weighted.append(
                (
                    "garmin_stress",
                    _clamp(100 - health.stress_average),
                    0.10,
                    health.stress_average,
                    None,
                    "garmin_score",
                    1.0,
                )
            )
        if health.body_battery_high is not None:
            weighted.append(
                (
                    "garmin_body_battery_high",
                    _clamp(health.body_battery_high),
                    0.10,
                    health.body_battery_high,
                    None,
                    "garmin_score",
                    1.0,
                )
            )

    recent = activities_between(
        session,
        user_id,
        datetime.combine(as_of - timedelta(days=6), time.min),
        datetime.combine(as_of + timedelta(days=1), time.min),
    )
    feedback = effective_activity_feedback(session, user_id, recent)
    measured = [
        activity
        for activity in recent
        if activity.aerobic_training_effect is not None
        or activity.anaerobic_training_effect is not None
        or feedback[activity.id].effort is not None
    ]
    activity_state = next(
        (
            state
            for state in sync_states_for_user(session, user_id)
            if state.resource == "activities"
        ),
        None,
    )
    training_data_complete = (
        activity_state is not None
        and activity_state.status == "ok"
        and activity_state.backfill_complete
        and len(measured) == len(recent)
    )
    if training_data_complete and measured:
        hard = [
            activity
            for activity in measured
            if (
                (activity.aerobic_training_effect or 0) >= 3.5
                or (activity.anaerobic_training_effect or 0) >= 2.5
                or (feedback[activity.id].effort or 0) >= 7
            )
        ]
        days_since_hard = (as_of - hard[-1].started_at.date()).days if hard else 7
        training_score = (35, 50, 65, 70, 75)[min(days_since_hard, 4)]
        weighted.append(
            (
                "recent_hard_training_recovery",
                float(training_score),
                0.05,
                float(days_since_hard),
                None,
                "days_since_hard_workout",
                1.0,
            )
        )

    health_weight = sum(item[2] for item in weighted if item[0] != "recent_hard_training_recovery")
    if health_weight < 0.20:
        return None, None, 0.0, (), None
    total_weight = sum(item[2] for item in weighted)
    components = tuple(
        ReadinessComponent(
            component=name,
            score=round(component_score, 1),
            normalized_weight=weight / total_weight,
            current=current,
            baseline=baseline,
            unit=unit,
        )
        for name, component_score, weight, current, baseline, unit, _ in weighted
    )
    raw_score = sum(item[1] * item[2] for item in weighted) / total_weight
    sleep_cap, limiter = (
        _readiness_sleep_cap(as_of, health, trends)
        if health_is_current and health is not None
        else (None, None)
    )
    score = round(min(raw_score, sleep_cap) if sleep_cap is not None else raw_score, 1)
    label = "high" if score >= 80 else "good" if score >= 65 else "fair" if score >= 45 else "low"
    history_factor = sum(item[2] * item[6] for item in weighted) / total_weight
    confidence = round(
        total_weight * (0.5 + 0.5 * history_factor) * 100,
        1,
    )
    return score, label, confidence, components, limiter


def get_hrv_baseline(
    session: Session, user_id: int, *, days: int = 28, as_of: date | None = None
) -> HrvBaseline:
    end = as_of or date.today()
    health = latest_health_on_or_before(session, user_id, end)
    trend = get_health_trends(session, user_id, days=days, as_of=end).hrv
    return HrvBaseline(
        trend=trend,
        garmin_status=health.hrv_status if health else None,
        garmin_baseline_low=health.hrv_baseline_low if health else None,
        garmin_balanced_low=health.hrv_baseline_balanced_low if health else None,
        garmin_balanced_high=health.hrv_baseline_balanced_high if health else None,
    )
