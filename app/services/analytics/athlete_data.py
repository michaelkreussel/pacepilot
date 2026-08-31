from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.repositories.activities import list_activities_on_or_before
from app.repositories.health import find_health_day
from app.repositories.workouts import workouts_between
from app.services.analytics.fitness_trends import (
    GarminFitnessAnalytics,
    get_garmin_fitness_metrics,
)
from app.services.analytics.health_trends import (
    HealthMetric,
    HealthTrends,
    HrvBaseline,
    MetricTrend,
    RecoveryState,
    get_current_recovery_state,
    get_health_trends,
    get_hrv_baseline,
)
from app.services.analytics.progress import ProgressResult, get_progress
from app.services.analytics.running_baseline import RunningBaseline, get_running_baseline
from app.services.analytics.running_intensity import (
    PerformanceAnchorLike,
    RunningShadowAnalysis,
    build_running_shadow_analysis,
)
from app.services.analytics.subjective_feedback import effective_activity_feedback
from app.services.analytics.training_trends import (
    ActivityDetails,
    RecentWorkout,
    TrainingSummary,
    TrainingTimelinePoint,
    get_activity_details,
    get_recent_workouts,
    get_training_summary,
    get_training_timeline,
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
class ActivityFeedbackSummary:
    activity_id: int
    started_at: datetime
    name: str
    effort: float | None
    effort_source: str | None
    feel: int | None
    feel_source: str | None


@dataclass(frozen=True)
class SubjectiveContext:
    as_of: date
    recent_activity_feedback: tuple[ActivityFeedbackSummary, ...]


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

    def get_health_trends_payload(
        self, days: int, metrics: tuple[HealthMetric, ...]
    ) -> dict[str, Any]:
        trends = self.get_health_trends(days)
        return {
            "start": trends.start,
            "end": trends.end,
            "metrics": {
                name: _bounded_metric_trend_payload(getattr(trends, name)) for name in metrics
            },
            "coverage": tuple(asdict(item) for item in trends.coverage),
        }

    def get_training_summary(self, days: int = 28) -> TrainingSummary:
        return get_training_summary(self.session, self.user_id, days=days, as_of=self.as_of)

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

    def get_garmin_fitness_metrics(self, days: int = 365) -> GarminFitnessAnalytics:
        return get_garmin_fitness_metrics(self.session, self.user_id, days=days, as_of=self.as_of)

    def get_activity_details(self, activity_id: int) -> ActivityDetails | None:
        return get_activity_details(self.session, self.user_id, activity_id, as_of=self.as_of)

    def get_running_baseline(self) -> RunningBaseline:
        return get_running_baseline(self.session, self.user_id, as_of=self.as_of)

    def get_running_shadow_analysis(
        self, *, performance_anchors: tuple[PerformanceAnchorLike, ...] = ()
    ) -> RunningShadowAnalysis:
        baseline = self.get_running_baseline()
        return build_running_shadow_analysis(
            self.session,
            self.user_id,
            baseline,
            performance_anchors=performance_anchors,
        )

    def get_subjective_context(self, *, activity_limit: int = 5) -> SubjectiveContext:
        through = datetime.combine(self.as_of, datetime.max.time())
        activities = list_activities_on_or_before(
            self.session, self.user_id, through, activity_limit
        )
        feedback = effective_activity_feedback(self.session, self.user_id, activities)
        recent = tuple(
            ActivityFeedbackSummary(
                activity.id,
                activity.started_at,
                activity.name,
                feedback[activity.id].effort,
                feedback[activity.id].effort_source,
                feedback[activity.id].feel,
                feedback[activity.id].feel_source,
            )
            for activity in activities
            if feedback[activity.id].effort is not None or feedback[activity.id].feel is not None
        )
        return SubjectiveContext(self.as_of, recent)

    def get_progress(self, *, days: int = 28, goal_id: int | None = None) -> ProgressResult:
        return get_progress(
            self.session,
            self.user_id,
            as_of=self.as_of,
            days=days,
            goal_id=goal_id,
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
            metrics = workout_metrics(workout.definition)
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


def _bounded_metric_trend_payload(trend: MetricTrend) -> dict[str, Any]:
    payload = asdict(trend)
    baseline = payload.get("personal_baseline")
    difference = payload.get("difference_from_baseline")
    payload["difference_from_baseline_percent"] = (
        round(float(difference) / float(baseline) * 100, 1)
        if isinstance(baseline, (int, float))
        and baseline != 0
        and isinstance(difference, (int, float))
        else None
    )
    payload["points"] = payload["points"][-31:]
    return payload
