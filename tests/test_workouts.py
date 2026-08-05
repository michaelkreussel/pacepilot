from datetime import date

import pytest

from app.models import Workout, WorkoutStep
from app.services.garmin.workout_export import _create_model
from app.services.planning.validator import (
    StepInput,
    WorkoutInput,
    WorkoutValidationError,
    validate_workout,
)


def test_validator_rejects_zero_duration() -> None:
    workout = WorkoutInput(
        name="Intervals",
        sport="running",
        scheduled_for=date.today(),
        description="",
        steps=[StepInput("interval", "time", 0)],
    )
    with pytest.raises(WorkoutValidationError):
        validate_workout(workout)


def test_validator_rejects_distance_recovery() -> None:
    workout = WorkoutInput(
        name="Invalid recovery",
        sport="running",
        scheduled_for=date.today(),
        description="",
        steps=[StepInput("recovery", "distance", 400)],
    )
    with pytest.raises(WorkoutValidationError):
        validate_workout(workout)


def test_garmin_workout_model_expands_repetitions() -> None:
    workout = Workout(
        user_id=1,
        name="3 x 5 minutes",
        sport="running",
        scheduled_for=date.today(),
        status="confirmed",
    )
    workout.steps = [
        WorkoutStep(
            position=1,
            step_type="interval",
            duration_type="time",
            duration_value=300,
            target_type="no_target",
            repeat_count=3,
        )
    ]

    payload = _create_model(workout).to_dict()

    assert payload["workoutName"] == "3 x 5 minutes"
    assert len(payload["workoutSegments"][0]["workoutSteps"]) == 3
    assert payload["estimatedDurationInSecs"] == 900
