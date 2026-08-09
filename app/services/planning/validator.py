from dataclasses import dataclass
from datetime import date

from app.services.planning.workout_definition import (
    DefinitionValidationError,
    WorkoutDefinition,
    validate_definition,
)

SPORTS = {"running", "cycling", "walking", "hiking"}


@dataclass(frozen=True)
class WorkoutInput:
    name: str
    sport: str
    scheduled_for: date | None
    description: str
    definition: WorkoutDefinition


class WorkoutValidationError(ValueError):
    pass


def validate_workout(workout: WorkoutInput) -> None:
    if not workout.name.strip():
        raise WorkoutValidationError("Bitte einen Namen angeben.")
    if len(workout.name) > 200:
        raise WorkoutValidationError("Der Name darf höchstens 200 Zeichen lang sein.")
    if workout.sport not in SPORTS:
        raise WorkoutValidationError("Diese Sportart wird noch nicht unterstützt.")
    try:
        validate_definition(workout.definition, workout.sport)
    except DefinitionValidationError as exc:
        raise WorkoutValidationError(str(exc)) from exc
