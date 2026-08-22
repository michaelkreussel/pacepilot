from collections.abc import Callable
from datetime import date
from typing import Any, Protocol

from app.services.garmin.client import GarminUnavailableError
from app.services.planning.validator import WorkoutValidationError
from app.services.planning.workout_definition import (
    DefinitionValidationError,
    HeartRateRangeTarget,
    HeartRateZoneTarget,
    PaceRangeTarget,
    RepeatBlock,
    StepBlock,
    TimeEnd,
    WorkoutBlock,
    validate_definition,
)

SPORT_TYPES = {
    "running": {"sportTypeId": 1, "sportTypeKey": "running"},
    "cycling": {"sportTypeId": 2, "sportTypeKey": "cycling"},
    "walking": {"sportTypeId": 17, "sportTypeKey": "walking"},
    "hiking": {"sportTypeId": 18, "sportTypeKey": "hiking"},
}


class WorkoutContent(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def sport(self) -> str: ...

    @property
    def description(self) -> str | None: ...

    @property
    def definition_model(self) -> Any: ...


class WorkoutExecution(WorkoutContent, Protocol):
    @property
    def scheduled_for(self) -> date | None: ...

    @property
    def garmin_workout_id(self) -> str | None: ...


def compile_workout(workout: WorkoutContent) -> dict[str, Any]:
    from garminconnect.workout import (
        ConditionType,
        CyclingWorkout,
        ExecutableStep,
        HikingWorkout,
        RunningWorkout,
        StepType,
        TargetType,
        WalkingWorkout,
        WorkoutSegment,
        create_repeat_group,
    )

    model_types = {
        "running": RunningWorkout,
        "cycling": CyclingWorkout,
        "walking": WalkingWorkout,
        "hiking": HikingWorkout,
    }
    model_type = model_types.get(workout.sport)
    if model_type is None:
        raise WorkoutValidationError(
            "Diese Sportart kann nicht an Garmin übertragen werden.",
            code="garmin.sport_unsupported",
        )
    definition = workout.definition_model
    try:
        validate_definition(definition, workout.sport)
    except DefinitionValidationError as exc:
        raise WorkoutValidationError(str(exc), code=exc.code) from exc
    step_types = {
        "warmup": (StepType.WARMUP, "warmup", 1),
        "cooldown": (StepType.COOLDOWN, "cooldown", 2),
        "interval": (StepType.INTERVAL, "interval", 3),
        "recovery": (StepType.RECOVERY, "recovery", 4),
    }

    def create_step(step: StepBlock, step_order: int) -> Any:
        step_type_id, step_type_key, display_order = step_types[step.step_type]
        is_time = isinstance(step.end, TimeEnd)
        condition_id = ConditionType.TIME if is_time else ConditionType.DISTANCE
        condition_key = "time" if is_time else "distance"
        condition_order = 2 if is_time else 3
        end_value = step.end.seconds if is_time else step.end.meters
        target: dict[str, Any]
        target_values: dict[str, Any] = {}
        if isinstance(step.target, PaceRangeTarget):
            target = {
                "workoutTargetTypeId": TargetType.PACE_ZONE,
                "workoutTargetTypeKey": "pace.zone",
                "displayOrder": 6,
            }
            target_values = {
                "targetValueOne": 1000 / step.target.fastest_seconds_per_km,
                "targetValueTwo": 1000 / step.target.slowest_seconds_per_km,
            }
        elif isinstance(step.target, HeartRateRangeTarget):
            target = {
                "workoutTargetTypeId": TargetType.HEART_RATE_ZONE,
                "workoutTargetTypeKey": "heart.rate.zone",
                "displayOrder": 4,
            }
            target_values = {
                "targetValueOne": float(step.target.lower_bpm),
                "targetValueTwo": float(step.target.upper_bpm),
            }
        elif isinstance(step.target, HeartRateZoneTarget):
            target = {
                "workoutTargetTypeId": TargetType.HEART_RATE_ZONE,
                "workoutTargetTypeKey": "heart.rate.zone",
                "displayOrder": 4,
            }
            target_values = {"zoneNumber": step.target.zone}
        else:
            target = {
                "workoutTargetTypeId": TargetType.NO_TARGET,
                "workoutTargetTypeKey": "no.target",
                "displayOrder": 1,
            }
        return ExecutableStep(
            stepOrder=step_order,
            stepType={
                "stepTypeId": step_type_id,
                "stepTypeKey": step_type_key,
                "displayOrder": display_order,
            },
            endCondition={
                "conditionTypeId": condition_id,
                "conditionTypeKey": condition_key,
                "displayOrder": condition_order,
                "displayable": True,
            },
            endConditionValue=float(end_value),
            targetType=target,
            **target_values,
        )

    def create_blocks(blocks: list[WorkoutBlock]) -> list[Any]:
        output: list[Any] = []
        for block in blocks:
            if isinstance(block, StepBlock):
                output.append(create_step(block, len(output) + 1))
            elif len(block.children) == 1 and isinstance(block.children[0], StepBlock):
                for _ in range(block.iterations):
                    output.append(create_step(block.children[0], len(output) + 1))
            else:
                output.append(
                    create_repeat_group(
                        block.iterations,
                        create_blocks(block.children),
                        len(output) + 1,
                    )
                )
        return output

    output_steps = create_blocks(definition.blocks)

    default_paces = {"running": 360.0, "walking": 720.0, "hiking": 900.0, "cycling": 150.0}

    def estimate_blocks(blocks: list[WorkoutBlock]) -> float:
        seconds = 0.0
        for block in blocks:
            if isinstance(block, RepeatBlock):
                seconds += estimate_blocks(block.children) * block.iterations
            elif isinstance(block.end, TimeEnd):
                seconds += block.end.seconds
            else:
                pace = (
                    (block.target.fastest_seconds_per_km + block.target.slowest_seconds_per_km) / 2
                    if isinstance(block.target, PaceRangeTarget)
                    else default_paces[workout.sport]
                )
                seconds += block.end.meters * pace / 1000
        return seconds

    estimated_seconds = int(estimate_blocks(definition.blocks))
    sport_type = SPORT_TYPES[workout.sport]
    return model_type(
        workoutName=workout.name,
        description=workout.description,
        estimatedDurationInSecs=estimated_seconds,
        workoutSegments=[
            WorkoutSegment(segmentOrder=1, sportType=sport_type, workoutSteps=output_steps)
        ],
    ).to_dict()


def _garmin_call[T](message: str, call: Callable[..., T], *args: Any) -> T:
    try:
        return call(*args)
    except Exception as exc:
        raise GarminUnavailableError(message) from exc


def upload_workout(client: Any, workout: WorkoutContent) -> str:
    response = _garmin_call(
        "Das Workout konnte nicht bei Garmin erstellt werden.",
        client.upload_workout,
        compile_workout(workout),
    )
    if not isinstance(response, dict):
        raise GarminUnavailableError("Garmin hat eine ungültige Antwort zurückgegeben.")
    workout_id = str(response.get("workoutId") or "")
    if not workout_id:
        raise GarminUnavailableError("Garmin hat keine Workout-ID zurückgegeben.")
    return workout_id


def schedule_published_workout(
    client: Any,
    workout: WorkoutExecution,
    previous_date: date | None = None,
) -> None:
    if not workout.garmin_workout_id:
        raise WorkoutValidationError(
            "Das Workout muss zuerst veröffentlicht werden.",
            code="garmin.remote_id_required",
        )
    if previous_date != workout.scheduled_for:
        unschedule_workout_on_date(client, workout.garmin_workout_id, previous_date)
    if workout.scheduled_for is not None and not scheduled_workout_ids(
        client, workout.garmin_workout_id, workout.scheduled_for
    ):
        _garmin_call(
            "Das Workout konnte nicht im Garmin-Kalender geplant werden.",
            client.schedule_workout,
            workout.garmin_workout_id,
            workout.scheduled_for.isoformat(),
        )


def push_workout(client: Any, workout: WorkoutExecution) -> None:
    if not workout.garmin_workout_id:
        raise WorkoutValidationError(
            "Das Workout muss zuerst veröffentlicht werden.",
            code="garmin.remote_id_required",
        )
    _garmin_call(
        "Das Workout konnte nicht an das Garmin-Gerät gesendet werden.",
        client.push_workout_to_device,
        workout.garmin_workout_id,
    )


def scheduled_workout_ids(client: Any, workout_id: str, scheduled_for: date) -> list[str]:
    calendar = _garmin_call(
        "Der Garmin-Kalender konnte nicht geladen werden.",
        client.get_scheduled_workouts,
        scheduled_for.year,
        scheduled_for.month,
    )
    scheduled_ids: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if (
                str(value.get("workoutId") or "") == workout_id
                and value.get("date") == scheduled_for.isoformat()
            ):
                scheduled_id = value.get("scheduledWorkoutId") or value.get("id")
                if scheduled_id is not None and str(scheduled_id) not in scheduled_ids:
                    scheduled_ids.append(str(scheduled_id))
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(calendar)
    return scheduled_ids


def unschedule_workout_on_date(client: Any, workout_id: str, scheduled_for: date | None) -> None:
    if scheduled_for is None:
        return
    for scheduled_id in scheduled_workout_ids(client, workout_id, scheduled_for):
        _garmin_call(
            "Die bisherige Garmin-Planung konnte nicht entfernt werden.",
            client.unschedule_workout,
            scheduled_id,
        )


def update_published_workout(
    client: Any, workout: WorkoutExecution, previous_date: date | None
) -> None:
    if not workout.garmin_workout_id:
        raise WorkoutValidationError(
            "Das Workout muss zuerst veröffentlicht werden.",
            code="garmin.remote_id_required",
        )
    _garmin_call(
        "Das Workout konnte bei Garmin nicht aktualisiert werden.",
        client.update_workout,
        workout.garmin_workout_id,
        compile_workout(workout),
    )
    if previous_date != workout.scheduled_for:
        unschedule_workout_on_date(client, workout.garmin_workout_id, previous_date)
        if workout.scheduled_for is not None and not scheduled_workout_ids(
            client, workout.garmin_workout_id, workout.scheduled_for
        ):
            _garmin_call(
                "Das Workout konnte nicht im Garmin-Kalender geplant werden.",
                client.schedule_workout,
                workout.garmin_workout_id,
                workout.scheduled_for.isoformat(),
            )
    _garmin_call(
        "Das Workout konnte nicht an das Garmin-Gerät gesendet werden.",
        client.push_workout_to_device,
        workout.garmin_workout_id,
    )


def update_workout_content(client: Any, workout: WorkoutExecution) -> None:
    if not workout.garmin_workout_id:
        raise WorkoutValidationError(
            "Das Workout muss zuerst veröffentlicht werden.",
            code="garmin.remote_id_required",
        )
    _garmin_call(
        "Das Workout konnte bei Garmin nicht aktualisiert werden.",
        client.update_workout,
        workout.garmin_workout_id,
        compile_workout(workout),
    )


def delete_published_workout(
    client: Any, workout: WorkoutExecution, remote_scheduled_for: date | None
) -> None:
    if not workout.garmin_workout_id:
        return
    unschedule_workout_on_date(client, workout.garmin_workout_id, remote_scheduled_for)
    delete_remote_workout(client, workout.garmin_workout_id)


def schedule_workout_on_date(client: Any, workout_id: str, scheduled_for: date) -> None:
    if scheduled_workout_ids(client, workout_id, scheduled_for):
        return
    _garmin_call(
        "Das Workout konnte nicht im Garmin-Kalender geplant werden.",
        client.schedule_workout,
        workout_id,
        scheduled_for.isoformat(),
    )


def delete_remote_workout(client: Any, workout_id: str) -> None:
    _garmin_call(
        "Das Workout konnte bei Garmin nicht gelöscht werden.",
        client.delete_workout,
        workout_id,
    )
