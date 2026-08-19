import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.models import DailyFitness
from app.repositories.fitness import fitness_on_or_before


@dataclass(frozen=True)
class FitnessMetricPoint:
    day: date
    value: float


@dataclass(frozen=True)
class GarminFitnessMetric:
    key: str
    unit: str
    latest: FitnessMetricPoint
    points: tuple[FitnessMetricPoint, ...]


@dataclass(frozen=True)
class GarminFitnessAnalytics:
    start: date
    end: date
    metrics: tuple[GarminFitnessMetric, ...]

    def get(self, key: str) -> GarminFitnessMetric | None:
        return next((metric for metric in self.metrics if metric.key == key), None)


MetricSpec = tuple[str, str, Callable[[DailyFitness], float | int | None]]
METRICS: tuple[MetricSpec, ...] = (
    ("threshold_hr", "bpm", lambda row: row.lactate_threshold_hr),
    ("threshold_speed", "m/s", lambda row: row.lactate_threshold_speed_mps),
    ("running_ftp", "W", lambda row: row.running_ftp_watts),
    ("cycling_ftp", "W", lambda row: row.cycling_ftp_watts),
    ("race_5k", "s", lambda row: row.race_prediction_5k_seconds),
    ("race_10k", "s", lambda row: row.race_prediction_10k_seconds),
    ("race_half", "s", lambda row: row.race_prediction_half_seconds),
    ("race_marathon", "s", lambda row: row.race_prediction_marathon_seconds),
    ("endurance_score", "Punkte", lambda row: row.endurance_score),
    ("hill_score", "Punkte", lambda row: row.hill_score),
    ("fitness_age", "Jahre", lambda row: row.fitness_age),
)


def _positive(value: float | int | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def get_garmin_fitness_metrics(
    session: Session,
    user_id: int,
    *,
    days: int = 365,
    as_of: date | None = None,
) -> GarminFitnessAnalytics:
    if days < 1:
        raise ValueError("days must be at least 1")
    end = as_of or date.today()
    start = end - timedelta(days=days - 1)
    rows = fitness_on_or_before(session, user_id, end)
    metrics: list[GarminFitnessMetric] = []
    for key, unit, extract in METRICS:
        available = [
            FitnessMetricPoint(row.day, value)
            for row in rows
            if (value := _positive(extract(row))) is not None
        ]
        if not available:
            continue
        metrics.append(
            GarminFitnessMetric(
                key=key,
                unit=unit,
                latest=available[-1],
                points=tuple(point for point in available if point.day >= start),
            )
        )
    return GarminFitnessAnalytics(start=start, end=end, metrics=tuple(metrics))
