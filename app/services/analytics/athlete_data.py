from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

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


@dataclass(frozen=True)
class PeriodTrainingSummary:
    period: str
    summary: TrainingSummary


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
