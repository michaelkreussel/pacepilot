from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy.orm import Session

from app.models import DailyFitness
from app.repositories.athlete_profile import (
    get_athlete_availability,
    get_athlete_goal,
    get_athlete_profile,
    get_manual_anchors,
)
from app.repositories.fitness import fitness_between
from app.repositories.health import find_health_day
from app.repositories.workouts import workouts_between
from app.services.analytics.automatic_profile import (
    AutomaticAthleteProfile,
    get_automatic_athlete_profile,
)
from app.services.analytics.health_trends import (
    HealthTrends,
    HrvBaseline,
    MetricTrend,
    RecoveryState,
    get_current_recovery_state,
    get_health_trends,
    get_hrv_baseline,
)
from app.services.analytics.training_trends import (
    ActivityDetails,
    RecentWorkout,
    TrainingSummary,
    TrainingTimelinePoint,
    WeeklyTrainingPoint,
    get_activity_details,
    get_recent_workouts,
    get_training_summary,
    get_training_timeline,
    get_weekly_training_trend,
)
from app.services.planning.planning_limits import (
    RunningPlanningLimits,
    derive_running_planning_limits,
)
from app.services.planning.workout_definition import workout_metrics


@dataclass(frozen=True)
class PeriodTrainingSummary:
    period: str
    summary: TrainingSummary


@dataclass(frozen=True)
class HealthDaySummary:
    day: date
    sleep_seconds: int | None
    sleep_score: int | None
    sleep_start_at: datetime | None
    sleep_end_at: datetime | None
    deep_sleep_seconds: int | None
    light_sleep_seconds: int | None
    rem_sleep_seconds: int | None
    awake_sleep_seconds: int | None
    nap_seconds: int | None
    sleep_need_seconds: int | None
    resting_hr: int | None
    min_hr: int | None
    max_hr: int | None
    hrv_average: float | None
    hrv_weekly_average: float | None
    hrv_status: str | None
    steps: int | None
    distance_m: float | None
    total_calories: int | None
    active_calories: int | None
    stress_average: int | None
    stress_max: int | None
    body_battery_high: int | None
    body_battery_low: int | None
    body_battery_charged: int | None
    body_battery_drained: int | None
    waking_respiration_average: float | None
    sleep_respiration_average: float | None
    spo2_average: float | None
    sleep_spo2_average: float | None
    spo2_lowest: float | None
    moderate_intensity_minutes: int | None
    vigorous_intensity_minutes: int | None


@dataclass(frozen=True)
class UpcomingWorkout:
    workout_id: int
    name: str
    sport: str
    scheduled_for: date
    status: str
    duration_seconds: float | None
    distance_meters: float | None


@dataclass(frozen=True)
class PlanningMetric:
    key: str
    value: float
    unit: str
    sport: str | None
    source: Literal["athlete", "garmin", "pacepilot"]
    source_day: date | None
    freshness: Literal["current", "aging", "stale", "unknown"]
    confidence: Literal["declared", "reported", "high", "medium", "low"]


@dataclass(frozen=True)
class PlanningProfileSummary:
    primary_sport: str | None
    experience_level: str | None
    experience_years: int | None
    constraint_note: str | None
    constraint_until: date | None
    updated_at: datetime | None


@dataclass(frozen=True)
class PlanningGoal:
    sport: str
    event_name: str | None
    target_date: date
    distance_m: float | None
    target_duration_s: int | None
    target_pace_s_per_km: float | None


@dataclass(frozen=True)
class PlanningAvailability:
    weekday: int
    max_duration_minutes: int


@dataclass(frozen=True)
class PlanningZone:
    sport: str
    zone_type: str
    zone_number: int
    lower_boundary: float
    upper_boundary: float | None
    source_day: date
    freshness: Literal["current", "aging", "stale", "unknown"]


@dataclass(frozen=True)
class TrainingCapacity:
    workouts_28d: int
    active_days_28d: int
    duration_28d_s: float | None
    running_distance_28d_m: float | None
    hard_workouts_28d: int
    workouts_84d: int
    duration_84d_s: float | None
    running_distance_84d_m: float | None
    longest_run_12w_m: float | None
    history_complete: bool


@dataclass(frozen=True)
class PlanningContext:
    schema_version: str
    as_of: date
    profile: PlanningProfileSummary
    goal: PlanningGoal | None
    availability: tuple[PlanningAvailability, ...]
    performance: tuple[PlanningMetric, ...]
    zones: tuple[PlanningZone, ...]
    training_capacity: TrainingCapacity
    automatic_profile: AutomaticAthleteProfile
    planning_limits: RunningPlanningLimits
    warnings: tuple[str, ...]


_METRIC_UNITS = {
    "max_hr": "bpm",
    "threshold_hr": "bpm",
    "threshold_pace_s_per_km": "s/km",
    "running_threshold_power_watts": "W",
    "cycling_ftp_watts": "W",
    "reference_1k_seconds": "s",
    "reference_5k_seconds": "s",
    "reference_10k_seconds": "s",
    "reference_half_seconds": "s",
    "reference_marathon_seconds": "s",
    "prediction_5k_seconds": "s",
    "prediction_10k_seconds": "s",
    "prediction_half_seconds": "s",
    "prediction_marathon_seconds": "s",
    "threshold_speed_mps": "m/s",
}


def _metric_freshness(
    metric: str, source_day: date | None, as_of: date
) -> Literal["current", "aging", "stale", "unknown"]:
    if source_day is None:
        return "unknown"
    age = (as_of - source_day).days
    current_days, aging_days = {
        "resting_hr_baseline": (7, 28),
        "vo2max": (90, 180),
        "max_hr": (365, 730),
        "threshold_hr": (90, 180),
        "threshold_pace_s_per_km": (90, 180),
        "running_threshold_power_watts": (90, 180),
        "cycling_ftp_watts": (90, 180),
        "prediction_5k_seconds": (30, 90),
        "prediction_10k_seconds": (30, 90),
        "prediction_half_seconds": (30, 90),
        "prediction_marathon_seconds": (30, 90),
        "zone_config": (30, 90),
    }.get(metric, (365, 730))
    if age <= current_days:
        return "current"
    if age <= aging_days:
        return "aging"
    return "stale"


def _latest_fitness_metric(
    rows: list[DailyFitness], attribute: str
) -> tuple[float | None, date | None]:
    for row in reversed(rows):
        value = getattr(row, attribute)
        if value is not None:
            return float(value), row.day
    return None, None


def _latest_zones(rows: list[DailyFitness], as_of: date) -> tuple[PlanningZone, ...]:
    selected: dict[str, tuple[list[dict[str, object]], date]] = {}
    for row in reversed(rows):
        for zone_type, values in (
            ("heart_rate", row.heart_rate_zones),
            ("power", row.power_zones),
        ):
            if zone_type not in selected and values:
                selected[zone_type] = (values, row.day)
    result = []
    for zone_type, (values, source_day) in selected.items():
        for item in values:
            sport = item.get("sport")
            number = item.get("zone")
            lower = item.get("lower")
            upper = item.get("upper")
            if (
                not isinstance(sport, str)
                or not isinstance(number, int)
                or not isinstance(lower, (int, float))
            ):
                continue
            result.append(
                PlanningZone(
                    sport=sport,
                    zone_type=zone_type,
                    zone_number=number,
                    lower_boundary=float(lower),
                    upper_boundary=(float(upper) if isinstance(upper, (int, float)) else None),
                    source_day=source_day,
                    freshness=_metric_freshness("zone_config", source_day, as_of),
                )
            )
    return tuple(result)


class AthleteDataService:
    """Compact deterministic athlete context for UI and future coach consumers."""

    def __init__(self, session: Session, user_id: int, *, as_of: date | None = None) -> None:
        self.session = session
        self.user_id = user_id
        self.as_of = as_of or date.today()

    def get_current_recovery_state(self) -> RecoveryState:
        return get_current_recovery_state(self.session, self.user_id, as_of=self.as_of)

    def get_health_trends(self, days: int = 28) -> HealthTrends:
        return get_health_trends(self.session, self.user_id, days=days, as_of=self.as_of)

    def get_training_summary(self, days: int = 28) -> TrainingSummary:
        return get_training_summary(self.session, self.user_id, days=days, as_of=self.as_of)

    def _get_training_capacity_summary(self, days: int) -> TrainingSummary:
        return get_training_summary(
            self.session,
            self.user_id,
            days=days,
            as_of=self.as_of,
            include_zones=False,
        )

    def get_standard_training_summaries(self) -> tuple[PeriodTrainingSummary, ...]:
        return tuple(
            PeriodTrainingSummary(period, self.get_training_summary(days))
            for period, days in (
                ("7_days", 7),
                ("28_days", 28),
                ("84_days", 84),
                ("6_months", 183),
                ("12_months", 365),
            )
        )

    def get_recent_workouts(self, limit: int = 10) -> tuple[RecentWorkout, ...]:
        return get_recent_workouts(self.session, self.user_id, limit=limit, as_of=self.as_of)

    def get_weekly_running_volume(self, weeks: int = 12) -> tuple[WeeklyTrainingPoint, ...]:
        return get_weekly_training_trend(self.session, self.user_id, weeks=weeks, as_of=self.as_of)

    def get_training_load_trend(self, weeks: int = 12) -> tuple[WeeklyTrainingPoint, ...]:
        return get_weekly_training_trend(self.session, self.user_id, weeks=weeks, as_of=self.as_of)

    def get_training_timeline(
        self, days: int = 28, *, bucket_days: int = 7
    ) -> tuple[TrainingTimelinePoint, ...]:
        return get_training_timeline(
            self.session,
            self.user_id,
            days=days,
            bucket_days=bucket_days,
            as_of=self.as_of,
        )

    def get_hrv_baseline(self, days: int = 28) -> HrvBaseline:
        return get_hrv_baseline(self.session, self.user_id, days=days, as_of=self.as_of)

    def get_sleep_trend(self, days: int = 28) -> MetricTrend:
        return self.get_health_trends(days).sleep_duration

    def get_vo2max_trend(self, days: int = 365) -> MetricTrend:
        return self.get_health_trends(days).vo2max

    def get_activity_details(self, activity_id: int) -> ActivityDetails | None:
        return get_activity_details(self.session, self.user_id, activity_id, as_of=self.as_of)

    def get_automatic_profile(
        self, *, include_detail_evidence: bool = True
    ) -> AutomaticAthleteProfile:
        return get_automatic_athlete_profile(
            self.session,
            self.user_id,
            as_of=self.as_of,
            include_detail_evidence=include_detail_evidence,
        )

    def get_planning_context(self, *, include_detail_evidence: bool = True) -> PlanningContext:
        profile = get_athlete_profile(self.session, self.user_id)
        goal = get_athlete_goal(self.session, self.user_id)
        availability = get_athlete_availability(self.session, self.user_id)
        anchors = get_manual_anchors(self.session, self.user_id)
        recovery = self.get_current_recovery_state()
        health = self.get_health_trends(28)
        training_28d = self._get_training_capacity_summary(28)
        training_84d = self._get_training_capacity_summary(84)
        weekly = self.get_weekly_running_volume(12)
        automatic_profile = self.get_automatic_profile(
            include_detail_evidence=include_detail_evidence
        )
        fitness_rows = fitness_between(
            self.session, self.user_id, self.as_of - timedelta(days=729), self.as_of
        )

        metrics: dict[tuple[str | None, str], PlanningMetric] = {}
        for anchor in anchors:
            metrics[(anchor.sport, anchor.metric)] = PlanningMetric(
                key=anchor.metric,
                value=anchor.value,
                unit=_METRIC_UNITS[anchor.metric],
                sport=anchor.sport,
                source="athlete",
                source_day=anchor.observed_on,
                freshness=_metric_freshness(anchor.metric, anchor.observed_on, self.as_of),
                confidence="declared",
            )

        primary_sport = profile.primary_sport if profile is not None else None

        def add_garmin(
            key: str,
            value: float | int | None,
            unit: str,
            source_day: date | None,
            sport: str | None = None,
        ) -> None:
            if value is None:
                return
            metric_key = (sport, key)
            metrics.setdefault(
                metric_key,
                PlanningMetric(
                    key=key,
                    value=float(value),
                    unit=unit,
                    sport=sport,
                    source="garmin",
                    source_day=source_day,
                    freshness=_metric_freshness(key, source_day, self.as_of),
                    confidence="reported",
                ),
            )

        resting_hr = health.resting_hr.personal_baseline or health.resting_hr.average_28d
        if resting_hr is not None:
            metrics[(None, "resting_hr_baseline")] = PlanningMetric(
                key="resting_hr_baseline",
                value=resting_hr,
                unit="bpm",
                sport=None,
                source="pacepilot",
                source_day=health.resting_hr.current_day,
                freshness=_metric_freshness(
                    "resting_hr_baseline", health.resting_hr.current_day, self.as_of
                ),
                confidence=(
                    "high"
                    if health.resting_hr.sample_count >= 14
                    else "medium"
                    if health.resting_hr.sample_count >= 7
                    else "low"
                ),
            )
        add_garmin("vo2max", recovery.vo2max, "ml/kg/min", recovery.vo2max_day, primary_sport)

        imported = (
            ("threshold_hr", "lactate_threshold_hr", "bpm", "running"),
            ("threshold_pace_s_per_km", "lactate_threshold_speed_mps", "s/km", "running"),
            ("running_threshold_power_watts", "running_ftp_watts", "W", "running"),
            ("cycling_ftp_watts", "cycling_ftp_watts", "W", "cycling"),
            ("prediction_5k_seconds", "race_prediction_5k_seconds", "s", "running"),
            ("prediction_10k_seconds", "race_prediction_10k_seconds", "s", "running"),
            ("prediction_half_seconds", "race_prediction_half_seconds", "s", "running"),
            ("prediction_marathon_seconds", "race_prediction_marathon_seconds", "s", "running"),
            ("reference_1k_seconds", "personal_record_1k_seconds", "s", "running"),
            ("reference_5k_seconds", "personal_record_5k_seconds", "s", "running"),
            ("reference_10k_seconds", "personal_record_10k_seconds", "s", "running"),
            ("reference_half_seconds", "personal_record_half_seconds", "s", "running"),
            (
                "reference_marathon_seconds",
                "personal_record_marathon_seconds",
                "s",
                "running",
            ),
            ("max_hr", "configured_max_hr", "bpm", "running"),
        )
        for key, attribute, unit, sport in imported:
            value, source_day = _latest_fitness_metric(fitness_rows, attribute)
            if key == "threshold_pace_s_per_km" and value is not None:
                value = 1_000 / value
            add_garmin(key, value, unit, source_day, sport)

        planning_goal = None
        if goal is not None:
            target_pace = (
                goal.target_duration_s / (goal.distance_m / 1_000)
                if goal.target_duration_s is not None and goal.distance_m
                else None
            )
            planning_goal = PlanningGoal(
                sport=goal.sport,
                event_name=goal.event_name,
                target_date=goal.target_date,
                distance_m=goal.distance_m,
                target_duration_s=goal.target_duration_s,
                target_pace_s_per_km=round(target_pace, 2) if target_pace is not None else None,
            )

        warnings: list[str] = []
        if profile is None or profile.primary_sport is None:
            warnings.append("profile_incomplete")
        if goal is None:
            warnings.append("goal_missing")
        if not availability:
            warnings.append("availability_missing")
        if not training_84d.history_complete:
            warnings.append("training_history_partial")
        performance_keys = {item.key for item in metrics.values()}
        if not performance_keys.intersection({"threshold_pace_s_per_km", "cycling_ftp_watts"}):
            warnings.append("threshold_missing")

        planning_zones = _latest_zones(fitness_rows, self.as_of)
        threshold_metric = next(
            (
                item
                for item in metrics.values()
                if item.key == "threshold_pace_s_per_km" and item.sport == "running"
            ),
            None,
        )
        planning_limits = derive_running_planning_limits(
            as_of=self.as_of,
            sport=primary_sport or (planning_goal.sport if planning_goal else "unknown"),
            performance_state=automatic_profile.performance_state,
            weekly_capacity=automatic_profile.weekly_capacity,
            longest_runs=automatic_profile.longest_runs,
            training_ranges=automatic_profile.training_ranges,
            recovery=recovery,
            availability=[(item.weekday, item.max_duration_minutes) for item in availability],
            constraint_note=profile.constraint_note if profile else None,
            constraint_until=profile.constraint_until if profile else None,
            goal_sport=planning_goal.sport if planning_goal else None,
            goal_date=planning_goal.target_date if planning_goal else None,
            goal_distance_m=planning_goal.distance_m if planning_goal else None,
            threshold_pace_s_per_km=threshold_metric.value if threshold_metric else None,
            threshold_freshness=threshold_metric.freshness if threshold_metric else None,
            heart_rate_zones=[
                (item.zone_number, item.lower_boundary, item.upper_boundary)
                for item in planning_zones
                if item.sport == "running"
                and item.zone_type == "heart_rate"
                and item.freshness in {"current", "aging"}
            ],
        )

        return PlanningContext(
            schema_version="planning-context.v2",
            as_of=self.as_of,
            profile=PlanningProfileSummary(
                primary_sport=profile.primary_sport if profile else None,
                experience_level=profile.experience_level if profile else None,
                experience_years=profile.experience_years if profile else None,
                constraint_note=profile.constraint_note if profile else None,
                constraint_until=profile.constraint_until if profile else None,
                updated_at=profile.updated_at if profile else None,
            ),
            goal=planning_goal,
            availability=tuple(
                PlanningAvailability(item.weekday, item.max_duration_minutes)
                for item in availability
            ),
            performance=tuple(
                sorted(metrics.values(), key=lambda item: (item.sport or "", item.key))
            ),
            zones=planning_zones,
            training_capacity=TrainingCapacity(
                workouts_28d=training_28d.workouts,
                active_days_28d=training_28d.active_days,
                duration_28d_s=training_28d.total_duration_s,
                running_distance_28d_m=training_28d.running_distance_m,
                hard_workouts_28d=training_28d.hard_workouts,
                workouts_84d=training_84d.workouts,
                duration_84d_s=training_84d.total_duration_s,
                running_distance_84d_m=training_84d.running_distance_m,
                longest_run_12w_m=max(
                    (item.longest_run_distance_m or 0 for item in weekly), default=0
                ),
                history_complete=training_84d.history_complete,
            ),
            automatic_profile=automatic_profile,
            planning_limits=planning_limits,
            warnings=tuple(dict.fromkeys((*warnings, *automatic_profile.warnings))),
        )

    def get_health_day(self, day: date) -> HealthDaySummary | None:
        health = find_health_day(self.session, self.user_id, day)
        if health is None:
            return None
        return HealthDaySummary(
            day=health.day,
            sleep_seconds=health.sleep_seconds,
            sleep_score=health.sleep_score,
            sleep_start_at=health.sleep_start_at,
            sleep_end_at=health.sleep_end_at,
            deep_sleep_seconds=health.deep_sleep_seconds,
            light_sleep_seconds=health.light_sleep_seconds,
            rem_sleep_seconds=health.rem_sleep_seconds,
            awake_sleep_seconds=health.awake_sleep_seconds,
            nap_seconds=health.nap_seconds,
            sleep_need_seconds=health.sleep_need_seconds,
            resting_hr=health.resting_hr,
            min_hr=health.min_hr,
            max_hr=health.max_hr,
            hrv_average=health.hrv_average,
            hrv_weekly_average=health.hrv_weekly_average,
            hrv_status=health.hrv_status,
            steps=health.steps,
            distance_m=health.distance_m,
            total_calories=health.total_calories,
            active_calories=health.active_calories,
            stress_average=health.stress_average,
            stress_max=health.stress_max,
            body_battery_high=health.body_battery_high,
            body_battery_low=health.body_battery_low,
            body_battery_charged=health.body_battery_charged,
            body_battery_drained=health.body_battery_drained,
            waking_respiration_average=health.waking_respiration_average,
            sleep_respiration_average=health.sleep_respiration_average,
            spo2_average=health.spo2_average,
            sleep_spo2_average=health.sleep_spo2_average,
            spo2_lowest=health.spo2_lowest,
            moderate_intensity_minutes=health.moderate_intensity_minutes,
            vigorous_intensity_minutes=health.vigorous_intensity_minutes,
        )

    def get_upcoming_workouts(self, days: int = 14) -> tuple[UpcomingWorkout, ...]:
        end = self.as_of + timedelta(days=days)
        upcoming: list[UpcomingWorkout] = []
        for workout in workouts_between(self.session, self.user_id, self.as_of, end):
            if workout.scheduled_for is None:
                continue
            metrics = workout_metrics(workout.definition_model)
            upcoming.append(
                UpcomingWorkout(
                    workout_id=workout.id,
                    name=workout.name,
                    sport=workout.sport,
                    scheduled_for=workout.scheduled_for,
                    status=workout.status,
                    duration_seconds=metrics.duration_seconds,
                    distance_meters=metrics.distance_meters,
                )
            )
        return tuple(upcoming)
