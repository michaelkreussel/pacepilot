import json
from datetime import date
from typing import Literal

import pytest
from pydantic import ValidationError

from app.models import Workout
from app.services.garmin.workout_export import compile_workout
from app.services.planning.validator import WorkoutInput, WorkoutValidationError, validate_workout
from app.services.planning.workout_definition import (
    DistanceEnd,
    HeartRateRangeTarget,
    HeartRateZoneTarget,
    NoTarget,
    PaceRangeTarget,
    RepeatBlock,
    StepBlock,
    TimeEnd,
    WorkoutDefinition,
    definition_to_json,
    parse_definition,
    workout_metrics,
)


def _step(
    identifier: str,
    step_type: Literal["warmup", "interval", "recovery", "cooldown"] = "interval",
    *,
    end: TimeEnd | DistanceEnd | None = None,
    target: NoTarget | PaceRangeTarget | HeartRateRangeTarget | HeartRateZoneTarget | None = None,
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


def _workout(
    definition: WorkoutDefinition, name: str = "Intervals", sport: str = "running"
) -> Workout:
    return Workout(
        user_id=1,
        name=name,
        sport=sport,
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


def test_validation_errors_have_stable_codes() -> None:
    with pytest.raises(WorkoutValidationError) as name_error:
        validate_workout(
            WorkoutInput(
                name="",
                sport="running",
                scheduled_for=None,
                description="",
                definition=WorkoutDefinition(blocks=[_step("step")]),
            )
        )

    definition = WorkoutDefinition(
        blocks=[
            _step(
                "pace",
                target=PaceRangeTarget(
                    type="pace_range",
                    fastest_seconds_per_km=300,
                    slowest_seconds_per_km=280,
                ),
            )
        ]
    )
    with pytest.raises(WorkoutValidationError) as definition_error:
        validate_workout(_input(definition))

    assert name_error.value.code == "workout.name_required"
    assert str(name_error.value) == "Bitte einen Namen angeben."
    assert definition_error.value.code == "definition.pace_order_invalid"
    assert "schnelle Pace-Grenze" in str(definition_error.value)


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

    payload = compile_workout(_workout(definition, "3 x 5 minutes"))

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

    payload = compile_workout(_workout(definition, "8 x 400 m"))
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


def test_workout_definition_v1_round_trip_preserves_complete_structure() -> None:
    definition = WorkoutDefinition(
        blocks=[
            _step("warmup", "warmup", target=NoTarget(type="none")),
            RepeatBlock(
                id="repeat",
                kind="repeat",
                iterations=4,
                children=[
                    _step(
                        "pace",
                        end=DistanceEnd(type="distance", meters=800),
                        target=PaceRangeTarget(
                            type="pace_range",
                            fastest_seconds_per_km=240,
                            slowest_seconds_per_km=260,
                        ),
                    ),
                    _step(
                        "heart-rate",
                        "recovery",
                        target=HeartRateRangeTarget(
                            type="heart_rate_range", lower_bpm=120, upper_bpm=145
                        ),
                    ),
                ],
            ),
            _step(
                "cooldown",
                "cooldown",
                target=HeartRateZoneTarget(type="heart_rate_zone", zone=2),
            ),
        ]
    )

    serialized = definition_to_json(definition)
    parsed = parse_definition(json.loads(json.dumps(serialized)))
    workout = _workout(parsed)

    assert parsed == definition
    assert workout.definition_model == definition
    assert workout.definition_version == 1
    assert "definition_version" not in serialized


@pytest.mark.parametrize(
    "raw",
    [
        {
            "blocks": [
                {
                    "id": "step",
                    "kind": "step",
                    "step_type": "interval",
                    "end": {"type": "time", "seconds": 60},
                    "target": {"type": "none"},
                }
            ],
            "extra": True,
        },
        {
            "blocks": [
                {
                    "id": "step",
                    "kind": "step",
                    "step_type": "interval",
                    "end": {"type": "time", "seconds": 60},
                    "target": {"type": "none"},
                    "extra": True,
                }
            ]
        },
        {
            "blocks": [
                {
                    "id": "step",
                    "kind": "step",
                    "step_type": "interval",
                    "end": {"type": "time", "seconds": 60, "extra": True},
                    "target": {"type": "none"},
                }
            ]
        },
        {
            "blocks": [
                {
                    "id": "step",
                    "kind": "step",
                    "step_type": "interval",
                    "end": {"type": "time", "seconds": 60},
                    "target": {"type": "none", "extra": True},
                }
            ]
        },
        {
            "blocks": [
                {
                    "id": "repeat",
                    "kind": "repeat",
                    "iterations": 2,
                    "children": [],
                    "extra": True,
                }
            ]
        },
    ],
)
def test_workout_definition_v1_rejects_unknown_fields(raw: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as exc_info:
        parse_definition(raw)

    assert any(error["type"] == "extra_forbidden" for error in exc_info.value.errors())


@pytest.mark.parametrize(
    ("definition", "sport", "message"),
    [
        (
            WorkoutDefinition(blocks=[_step("")]),
            "running",
            "eindeutige ID",
        ),
        (
            WorkoutDefinition(
                blocks=[
                    _step("same"),
                    RepeatBlock(
                        id="repeat",
                        kind="repeat",
                        iterations=2,
                        children=[_step("same")],
                    ),
                ]
            ),
            "running",
            "eindeutige ID",
        ),
        (
            WorkoutDefinition(
                blocks=[RepeatBlock(id="repeat", kind="repeat", iterations=2, children=[])]
            ),
            "running",
            "mindestens einen Schritt",
        ),
        (
            WorkoutDefinition(
                blocks=[
                    RepeatBlock(id="repeat", kind="repeat", iterations=1, children=[_step("a")])
                ]
            ),
            "running",
            "zwischen 2 und 50",
        ),
        (
            WorkoutDefinition(
                blocks=[
                    RepeatBlock(
                        id="outer",
                        kind="repeat",
                        iterations=2,
                        children=[
                            RepeatBlock(
                                id="inner",
                                kind="repeat",
                                iterations=2,
                                children=[_step("a")],
                            )
                        ],
                    )
                ]
            ),
            "running",
            "Verschachtelte Wiederholungen",
        ),
        (
            WorkoutDefinition(
                blocks=[
                    _step(
                        "pace",
                        target=PaceRangeTarget(
                            type="pace_range",
                            fastest_seconds_per_km=240,
                            slowest_seconds_per_km=260,
                        ),
                    )
                ]
            ),
            "cycling",
            "nur beim Laufen",
        ),
        (
            WorkoutDefinition(
                blocks=[
                    _step(
                        "heart-rate",
                        target=HeartRateRangeTarget(
                            type="heart_rate_range", lower_bpm=20, upper_bpm=145
                        ),
                    )
                ]
            ),
            "running",
            "zwischen 30 und 250",
        ),
        (
            WorkoutDefinition(
                blocks=[
                    _step(
                        "heart-rate-zone",
                        target=HeartRateZoneTarget(type="heart_rate_zone", zone=6),
                    )
                ]
            ),
            "running",
            "zwischen 1 und 5",
        ),
    ],
)
def test_workout_definition_v1_validation_contract(
    definition: WorkoutDefinition, sport: str, message: str
) -> None:
    with pytest.raises(WorkoutValidationError, match=message):
        validate_workout(_input(definition, sport))


@pytest.mark.parametrize(
    ("sport", "sport_id", "estimated_seconds"),
    [
        ("running", 1, 360),
        ("cycling", 2, 150),
        ("walking", 17, 720),
        ("hiking", 18, 900),
    ],
)
def test_garmin_v1_golden_payload_for_each_supported_sport(
    sport: str, sport_id: int, estimated_seconds: int
) -> None:
    definition = WorkoutDefinition(
        blocks=[_step("step", end=DistanceEnd(type="distance", meters=1000))]
    )
    workout = _workout(definition, name="Golden", sport=sport)
    workout.description = "Description"

    payload = compile_workout(workout)

    assert payload == {
        "workoutName": "Golden",
        "sportType": {
            "sportTypeId": sport_id,
            "sportTypeKey": sport,
            "displayOrder": sport_id,
        },
        "estimatedDurationInSecs": estimated_seconds,
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": {"sportTypeId": sport_id, "sportTypeKey": sport},
                "workoutSteps": [
                    {
                        "type": "ExecutableStepDTO",
                        "stepOrder": 1,
                        "stepType": {
                            "stepTypeId": 3,
                            "stepTypeKey": "interval",
                            "displayOrder": 3,
                        },
                        "endCondition": {
                            "conditionTypeId": 3,
                            "conditionTypeKey": "distance",
                            "displayOrder": 3,
                            "displayable": True,
                        },
                        "endConditionValue": 1000.0,
                        "targetType": {
                            "workoutTargetTypeId": 1,
                            "workoutTargetTypeKey": "no.target",
                            "displayOrder": 1,
                        },
                    }
                ],
            }
        ],
        "author": {},
        "description": "Description",
    }


def test_garmin_v1_compiles_heart_rate_zone_target() -> None:
    definition = WorkoutDefinition(
        blocks=[
            _step(
                "zone",
                target=HeartRateZoneTarget(type="heart_rate_zone", zone=3),
            )
        ]
    )

    step = compile_workout(_workout(definition))["workoutSegments"][0]["workoutSteps"][0]

    assert step["targetType"] == {
        "workoutTargetTypeId": 4,
        "workoutTargetTypeKey": "heart.rate.zone",
        "displayOrder": 4,
    }
    assert step["zoneNumber"] == 3
    assert "targetValueOne" not in step
    assert "targetValueTwo" not in step
