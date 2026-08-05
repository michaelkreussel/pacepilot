from dataclasses import dataclass
from datetime import date

SPORTS = {"running", "cycling", "walking", "hiking"}
STEP_TYPES = {"warmup", "interval", "recovery", "cooldown"}
DURATION_TYPES = {"time", "distance"}


@dataclass(frozen=True)
class StepInput:
    step_type: str
    duration_type: str
    duration_value: float | None
    repeat_count: int = 1


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
        if step.duration_type == "distance" and step.step_type != "interval":
            raise WorkoutValidationError("Distanz ist derzeit nur für Belastungsschritte möglich.")
        if not 1 <= step.repeat_count <= 50:
            raise WorkoutValidationError("Wiederholungen müssen zwischen 1 und 50 liegen.")
