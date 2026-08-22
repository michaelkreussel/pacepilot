import math
from dataclasses import dataclass
from datetime import date, timedelta

from app.models import Activity

SPORT_CLASSIFICATION_VERSION = "1.0"
HARD_ACTIVITY_RULE_VERSION = "1.0"


@dataclass(frozen=True)
class CalendarWindow:
    start: date
    end: date
    days: int


def calendar_window(days: int, *, as_of: date | None = None) -> CalendarWindow:
    if days < 1:
        raise ValueError("days must be at least 1")
    end = as_of or date.today()
    return CalendarWindow(start=end - timedelta(days=days - 1), end=end, days=days)


def sport_family(sport: str) -> str:
    normalized = sport.lower()
    has_run = "run" in normalized
    has_bike = "cycl" in normalized or "bik" in normalized
    if has_run and has_bike:
        return normalized
    if (
        normalized == "running"
        or normalized.endswith("_running")
        or normalized in {"trail_run", "ultra_run", "obstacle_run"}
    ):
        return "running"
    if (
        normalized == "cycling"
        or normalized.endswith(("_cycling", "_biking"))
        or normalized in {"bike", "road_bike"}
    ):
        return "cycling"
    return normalized


def is_running_sport(sport: str) -> bool:
    return sport_family(sport) == "running"


def hard_activity_data_available(activity: Activity) -> bool:
    return (
        _valid_training_effect(activity.aerobic_training_effect)
        or _valid_training_effect(activity.anaerobic_training_effect)
        or _valid_rpe(activity.workout_rpe)
    )


def _valid_training_effect(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and 0 <= value <= 5


def _valid_rpe(value: int | None) -> bool:
    return value is not None and 1 <= value <= 10


def _training_effect_at_least(value: float | None, threshold: float) -> bool:
    return _valid_training_effect(value) and value is not None and value >= threshold


def _rpe_at_least(value: int | None, threshold: int) -> bool:
    return _valid_rpe(value) and value is not None and value >= threshold


def is_hard_activity(activity: Activity) -> bool:
    return (
        _training_effect_at_least(activity.aerobic_training_effect, 3.5)
        or _training_effect_at_least(activity.anaerobic_training_effect, 2.5)
        or _rpe_at_least(activity.workout_rpe, 7)
    )
