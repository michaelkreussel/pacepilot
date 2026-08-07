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


def test_validator_accepts_distance_warmup_and_pace_target() -> None:
    workout = WorkoutInput(
        name="Distance warmup",
        sport="running",
        scheduled_for=date.today(),
        description="",
        steps=[StepInput("warmup", "distance", 2000, 1, "pace", 335, 420)],
    )

    validate_workout(workout)


def test_validator_rejects_incomplete_pace_target() -> None:
    workout = WorkoutInput(
        name="Invalid pace",
        sport="running",
        scheduled_for=date.today(),
        description="",
        steps=[StepInput("interval", "distance", 400, 1, "pace", 235, None)],
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


def test_garmin_model_builds_distance_pace_repeat_group() -> None:
    workout = Workout(
        user_id=1,
        name="8 x 400 m",
        sport="running",
        scheduled_for=date(2026, 8, 11),
        status="confirmed",
    )
    workout.steps = [
        WorkoutStep(
            position=1,
            step_type="warmup",
            duration_type="distance",
            duration_value=2000,
            target_type="no_target",
            repeat_count=1,
        ),
        WorkoutStep(
            position=2,
            step_type="recovery",
            duration_type="time",
            duration_value=90,
            target_type="no_target",
            repeat_count=1,
        ),
        WorkoutStep(
            position=3,
            step_type="interval",
            duration_type="distance",
            duration_value=400,
            target_type="pace",
            target_min=235,
            target_max=255,
            repeat_count=8,
        ),
        WorkoutStep(
            position=4,
            step_type="recovery",
            duration_type="time",
            duration_value=60,
            target_type="no_target",
            repeat_count=8,
        ),
        WorkoutStep(
            position=5,
            step_type="cooldown",
            duration_type="distance",
            duration_value=1200,
            target_type="no_target",
            repeat_count=1,
        ),
    ]

    payload = _create_model(workout).to_dict()
    steps = payload["workoutSegments"][0]["workoutSteps"]

    assert len(steps) == 4
    assert steps[0]["stepType"]["stepTypeKey"] == "warmup"
    assert steps[0]["endCondition"]["conditionTypeKey"] == "distance"
    assert steps[1]["endConditionValue"] == 90
    assert steps[2]["type"] == "RepeatGroupDTO"
    assert steps[2]["numberOfIterations"] == 8
    assert [child["stepType"]["stepTypeKey"] for child in steps[2]["workoutSteps"]] == [
        "interval",
        "recovery",
    ]
    interval = steps[2]["workoutSteps"][0]
    assert interval["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert interval["targetValueOne"] == pytest.approx(1000 / 235)
    assert interval["targetValueTwo"] == pytest.approx(1000 / 255)
    assert steps[3]["stepType"]["stepTypeKey"] == "cooldown"
    assert steps[3]["endCondition"]["conditionTypeKey"] == "distance"
