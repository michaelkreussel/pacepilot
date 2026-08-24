from dataclasses import dataclass
from datetime import date

from app.services.planning.workout_definition import (
    DefinitionValidationError,
    WorkoutDefinition,
    WorkoutDefinitionModel,
    WorkoutDefinitionV2,
    validate_definition,
)

SPORTS = {"running", "cycling", "walking", "hiking"}


@dataclass(frozen=True)
class WorkoutInput:
    name: str
    sport: str
    scheduled_for: date | None
    description: str
    definition: WorkoutDefinitionModel
    definition_version: int = 1


class WorkoutValidationError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def validate_workout(workout: WorkoutInput) -> None:
    if not workout.name.strip():
        raise WorkoutValidationError("Bitte einen Namen angeben.", code="workout.name_required")
    if len(workout.name) > 200:
        raise WorkoutValidationError(
            "Der Name darf höchstens 200 Zeichen lang sein.",
            code="workout.name_too_long",
        )
    if workout.sport not in SPORTS:
        raise WorkoutValidationError(
            "Diese Sportart wird noch nicht unterstützt.",
            code="workout.sport_unsupported",
        )
    if workout.definition_version not in {1, 2} or (
        workout.definition_version == 1
        and not isinstance(workout.definition, WorkoutDefinition)
        or workout.definition_version == 2
        and not isinstance(workout.definition, WorkoutDefinitionV2)
    ):
        raise WorkoutValidationError(
            "Workout-Struktur und Formatversion passen nicht zusammen.",
            code="definition.version_mismatch",
        )
    try:
        validate_definition(workout.definition, workout.sport)
    except DefinitionValidationError as exc:
        raise WorkoutValidationError(str(exc), code=exc.code) from exc
