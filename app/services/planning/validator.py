from dataclasses import dataclass
from datetime import date

SPORTS = {"running", "cycling", "walking", "hiking"}
STEP_TYPES = {"warmup", "interval", "recovery", "cooldown"}
DURATION_TYPES = {"time", "distance"}
TARGET_TYPES = {"no_target", "pace"}


@dataclass(frozen=True)
class StepInput:
    step_type: str
    duration_type: str
    duration_value: float | None
    repeat_count: int = 1
    target_type: str = "no_target"
    target_min: float | None = None
    target_max: float | None = None


@dataclass(frozen=True)
class WorkoutInput:
    name: str
    sport: str
    scheduled_for: date | None
    description: str
    steps: list[StepInput]


class WorkoutValidationError(ValueError):
    pass


def validate_workout(workout: WorkoutInput) -> None:
    if not workout.name.strip():
        raise WorkoutValidationError("Bitte einen Namen angeben.")
    if workout.sport not in SPORTS:
        raise WorkoutValidationError("Diese Sportart wird noch nicht unterstützt.")
    if not workout.steps:
        raise WorkoutValidationError("Mindestens ein Trainingsschritt ist erforderlich.")
    for step in workout.steps:
        if step.step_type not in STEP_TYPES:
            raise WorkoutValidationError("Ein Trainingsschritt hat einen ungültigen Typ.")
        if step.duration_type not in DURATION_TYPES:
            raise WorkoutValidationError("Ein Trainingsschritt hat eine ungültige Dauer.")
        if step.duration_value is None or step.duration_value <= 0:
            raise WorkoutValidationError("Zeit und Distanz müssen größer als null sein.")
        if not 1 <= step.repeat_count <= 50:
            raise WorkoutValidationError("Wiederholungen müssen zwischen 1 und 50 liegen.")
        if step.target_type not in TARGET_TYPES:
            raise WorkoutValidationError("Ein Trainingsschritt hat ein ungültiges Ziel.")
        if step.target_type == "pace":
            if workout.sport != "running":
                raise WorkoutValidationError("Pace-Ziele sind derzeit nur beim Laufen möglich.")
            if step.target_min is None or step.target_max is None:
                raise WorkoutValidationError("Für ein Pace-Ziel beide Grenzen angeben.")
            if step.target_min <= 0 or step.target_max <= 0:
                raise WorkoutValidationError("Pace-Grenzen müssen größer als null sein.")
            if step.target_min > step.target_max:
                raise WorkoutValidationError(
                    "Die schnelle Pace-Grenze darf nicht langsamer als die langsame sein."
                )
