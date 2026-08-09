from datetime import date
from typing import Literal

import pytest

from app.models import Workout
from app.services.garmin.workout_export import _create_model
from app.services.planning.validator import WorkoutInput, WorkoutValidationError, validate_workout
from app.services.planning.workout_definition import (
    DistanceEnd,
    HeartRateRangeTarget,
    NoTarget,
    PaceRangeTarget,
    RepeatBlock,
    StepBlock,
    TimeEnd,
    WorkoutDefinition,
    definition_to_json,
    workout_metrics,
)


def _step(
    identifier: str,
    step_type: Literal["warmup", "interval", "recovery", "cooldown"] = "interval",
    *,
    end: TimeEnd | DistanceEnd | None = None,
    target: NoTarget | PaceRangeTarget | HeartRateRangeTarget | None = None,
) -> StepBlock:
    return StepBlock(
        id=identifier,
        kind="step",
        step_type=step_type,
        end=end or TimeEnd(type="time", seconds=300),
        target=target or NoTarget(type="none"),
    )


def _input(definition: WorkoutDefinition, sport: str = "running") -> WorkoutInput:
    return WorkoutInput(
        name="Intervals",
        sport=sport,
        scheduled_for=date.today(),
        description="",
        definition=definition,
    )


def _workout(definition: WorkoutDefinition, name: str = "Intervals") -> Workout:
    return Workout(
        user_id=1,
        name=name,
        sport="running",
        scheduled_for=date.today(),
        status="confirmed",
        definition_version=1,
        definition=definition_to_json(definition),
    )


def test_validator_rejects_zero_duration() -> None:
    definition = WorkoutDefinition(blocks=[_step("interval", end=TimeEnd(type="time", seconds=0))])

    with pytest.raises(WorkoutValidationError):
        validate_workout(_input(definition))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_validator_rejects_non_finite_duration(value: float) -> None:
    definition = WorkoutDefinition(
        blocks=[_step("interval", end=TimeEnd(type="time", seconds=value))]
    )

    with pytest.raises(WorkoutValidationError):
        validate_workout(_input(definition))


def test_validator_accepts_distance_warmup_and_pace_target() -> None:
    definition = WorkoutDefinition(
        blocks=[
            _step(
                "warmup",
                "warmup",
                end=DistanceEnd(type="distance", meters=2000),
                target=PaceRangeTarget(
                    type="pace_range",
                    fastest_seconds_per_km=335,
                    slowest_seconds_per_km=420,
                ),
            )
        ]
    )

    validate_workout(_input(definition))


def test_validator_rejects_reversed_pace_target() -> None:
    definition = WorkoutDefinition(
        blocks=[
            _step(
                "interval",
                target=PaceRangeTarget(
                    type="pace_range",
                    fastest_seconds_per_km=255,
                    slowest_seconds_per_km=235,
                ),
            )
        ]
    )

    with pytest.raises(WorkoutValidationError):
        validate_workout(_input(definition))


def test_metrics_apply_repeat_iterations() -> None:
    definition = WorkoutDefinition(
        blocks=[
            RepeatBlock(
                id="repeat",
                kind="repeat",
                iterations=8,
                children=[
                    _step("interval", end=DistanceEnd(type="distance", meters=400)),
                    _step("recovery", "recovery", end=TimeEnd(type="time", seconds=60)),
                ],
            )
        ]
    )

    metrics = workout_metrics(definition)

    assert metrics.step_count == 2
    assert metrics.expanded_step_count == 16
    assert metrics.distance_meters == 3200
    assert metrics.duration_seconds == 480


def test_garmin_model_expands_single_child_repeat() -> None:
    definition = WorkoutDefinition(
        blocks=[
            RepeatBlock(
                id="repeat",
                kind="repeat",
                iterations=3,
                children=[_step("interval")],
            )
        ]
    )

    payload = _create_model(_workout(definition, "3 x 5 minutes")).to_dict()

    assert len(payload["workoutSegments"][0]["workoutSteps"]) == 3
    assert payload["estimatedDurationInSecs"] == 900


def test_garmin_model_builds_heterogeneous_repeat_with_hr_target() -> None:
    definition = WorkoutDefinition(
        blocks=[
            _step("warmup", "warmup", end=DistanceEnd(type="distance", meters=2000)),
            RepeatBlock(
                id="repeat",
                kind="repeat",
                iterations=8,
                children=[
                    _step(
                        "interval",
                        end=DistanceEnd(type="distance", meters=400),
                        target=PaceRangeTarget(
                            type="pace_range",
                            fastest_seconds_per_km=235,
                            slowest_seconds_per_km=255,
                        ),
                    ),
                    _step(
                        "recovery",
                        "recovery",
                        end=TimeEnd(type="time", seconds=60),
                        target=HeartRateRangeTarget(
                            type="heart_rate_range", lower_bpm=120, upper_bpm=145
                        ),
                    ),
                ],
            ),
            _step("cooldown", "cooldown", end=DistanceEnd(type="distance", meters=1200)),
        ]
    )

    payload = _create_model(_workout(definition, "8 x 400 m")).to_dict()
    steps = payload["workoutSegments"][0]["workoutSteps"]

    assert len(steps) == 3
    assert steps[1]["type"] == "RepeatGroupDTO"
    assert steps[1]["numberOfIterations"] == 8
    assert [child["stepType"]["stepTypeKey"] for child in steps[1]["workoutSteps"]] == [
        "interval",
        "recovery",
    ]
    interval, recovery = steps[1]["workoutSteps"]
    assert interval["targetValueOne"] == pytest.approx(1000 / 235)
    assert interval["targetValueTwo"] == pytest.approx(1000 / 255)
    assert recovery["targetType"]["workoutTargetTypeId"] == 4
    assert recovery["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert recovery["targetValueOne"] == 120
    assert recovery["targetValueTwo"] == 145
