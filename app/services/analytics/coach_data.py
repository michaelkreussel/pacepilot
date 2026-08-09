from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.repositories.workouts import workouts_between
from app.services.analytics.athlete_data import AthleteDataService
from app.services.analytics.health_trends import HealthTrends, RecoveryState
from app.services.analytics.training_trends import (
    ActivityDetails,
    RecentWorkout,
    TrainingSummary,
    TrainingTimelinePoint,
)
from app.services.planning.workout_definition import workout_metrics


@dataclass(frozen=True)
class AthleteProfileContext:
    as_of: date
    display_name: str
    available_fields: tuple[str, ...]


@dataclass(frozen=True)
class PlannedWorkoutContext:
    name: str
    sport: str
    scheduled_for: date
    status: str
    description: str | None
    duration_s: float | None
    distance_m: float | None
    step_count: int


class CoachDataService:
    """Read-only domain boundary for Coach capabilities."""

    def __init__(
        self,
        session: Session,
        user_id: int,
        display_name: str,
        *,
        as_of: date | None = None,
    ) -> None:
        self._session = session
        self._user_id = user_id
        self._display_name = display_name
        self.as_of = as_of or date.today()
        self._athlete_data = AthleteDataService(session, user_id, as_of=self.as_of)

    def get_profile_context(self) -> AthleteProfileContext:
        return AthleteProfileContext(
            as_of=self.as_of,
            display_name=self._display_name,
            available_fields=("display_name",),
        )

    def get_recovery_state(self) -> RecoveryState:
        return self._athlete_data.get_current_recovery_state()

    def get_health_trends(self, days: int) -> HealthTrends:
        return self._athlete_data.get_health_trends(days)

    def get_training_summary(self, days: int) -> TrainingSummary:
        return self._athlete_data.get_training_summary(days)

    def get_training_timeline(
        self, days: int, *, bucket_days: int
    ) -> tuple[TrainingTimelinePoint, ...]:
        return self._athlete_data.get_training_timeline(days, bucket_days=bucket_days)

    def get_recent_workouts(self, limit: int) -> tuple[RecentWorkout, ...]:
        return self._athlete_data.get_recent_workouts(limit)

    def get_activity_details(self, activity_id: int) -> ActivityDetails | None:
        return self._athlete_data.get_activity_details(activity_id)

    def get_planned_workouts(self, days: int) -> tuple[PlannedWorkoutContext, ...]:
        end = self.as_of + timedelta(days=days)
        planned: list[PlannedWorkoutContext] = []
        for workout in workouts_between(self._session, self._user_id, self.as_of, end):
            if workout.scheduled_for is None:
                continue
            metrics = workout_metrics(workout.definition_model)
            planned.append(
                PlannedWorkoutContext(
                    name=workout.name,
                    sport=workout.sport,
                    scheduled_for=workout.scheduled_for,
                    status=workout.status,
                    description=workout.description,
                    duration_s=metrics.duration_seconds,
                    distance_m=metrics.distance_meters,
                    step_count=metrics.step_count,
                )
            )
        return tuple(planned)
