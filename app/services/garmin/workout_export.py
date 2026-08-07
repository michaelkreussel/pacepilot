from datetime import date
from typing import Any

from app.models import Workout
from app.services.garmin.client import connect_garmin
from app.services.planning.validator import WorkoutValidationError

SPORT_TYPES = {
    "running": {"sportTypeId": 1, "sportTypeKey": "running"},
    "cycling": {"sportTypeId": 2, "sportTypeKey": "cycling"},
    "walking": {"sportTypeId": 4, "sportTypeKey": "walking"},
    "hiking": {"sportTypeId": 17, "sportTypeKey": "hiking"},
}


def _create_model(workout: Workout) -> Any:
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
        raise WorkoutValidationError("Diese Sportart kann nicht an Garmin übertragen werden.")
    step_types = {
        "warmup": (StepType.WARMUP, "warmup", 1),
        "cooldown": (StepType.COOLDOWN, "cooldown", 2),
        "interval": (StepType.INTERVAL, "interval", 3),
        "recovery": (StepType.RECOVERY, "recovery", 4),
    }

    def create_step(step: Any, step_order: int) -> Any:
        step_type_id, step_type_key, display_order = step_types[step.step_type]
        condition_id = (
            ConditionType.TIME if step.duration_type == "time" else ConditionType.DISTANCE
        )
        condition_key = "time" if step.duration_type == "time" else "distance"
        condition_order = 2 if step.duration_type == "time" else 3
        target: dict[str, Any]
        target_values: dict[str, float] = {}
        if step.target_type == "pace":
            if not step.target_min or not step.target_max:
                raise WorkoutValidationError("Für das Pace-Ziel fehlen Grenzen.")
            target = {
                "workoutTargetTypeId": TargetType.PACE_ZONE,
                "workoutTargetTypeKey": "pace.zone",
                "displayOrder": 6,
            }
            target_values = {
                "targetValueOne": 1000 / step.target_min,
                "targetValueTwo": 1000 / step.target_max,
            }
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
            endConditionValue=float(step.duration_value or 0),
            targetType=target,
            **target_values,
        )

    output_steps: list[Any] = []
    order = 1
    index = 0
    while index < len(workout.steps):
        step = workout.steps[index]
        repetitions = step.repeat_count or 1
        next_step = workout.steps[index + 1] if index + 1 < len(workout.steps) else None
        if (
            step.step_type == "interval"
            and repetitions > 1
            and next_step is not None
            and next_step.step_type == "recovery"
            and (next_step.repeat_count or 1) == repetitions
        ):
            output_steps.append(
                create_repeat_group(
                    repetitions,
                    [create_step(step, 1), create_step(next_step, 2)],
                    order,
                )
            )
            order += 1
            index += 2
            continue
        for _ in range(repetitions):
            output_steps.append(create_step(step, order))
            order += 1
        index += 1

    default_paces = {"running": 360.0, "walking": 720.0, "hiking": 900.0, "cycling": 150.0}

    def estimate_step_seconds(step: Any) -> float:
        value = float(step.duration_value or 0)
        if step.duration_type == "time":
            return value
        pace = (
            (step.target_min + step.target_max) / 2
            if step.target_type == "pace" and step.target_min and step.target_max
            else default_paces[workout.sport]
        )
        return value * pace / 1000

    estimated_seconds = int(
        sum(estimate_step_seconds(step) * (step.repeat_count or 1) for step in workout.steps)
    )
    sport_type = SPORT_TYPES[workout.sport]
    return model_type(
        workoutName=workout.name,
        description=workout.description,
        estimatedDurationInSecs=estimated_seconds,
        workoutSegments=[
            WorkoutSegment(segmentOrder=1, sportType=sport_type, workoutSteps=output_steps)
        ],
    )


def publish_workout(workout: Workout) -> str:
    client = connect_garmin()
    model = _create_model(workout)
    response = client.upload_workout(model.to_dict())
    workout_id = str(response.get("workoutId") or "")
    if not workout_id:
        raise RuntimeError("Garmin hat keine Workout-ID zurückgegeben.")
    if workout.scheduled_for is not None:
        client.schedule_workout(workout_id, workout.scheduled_for.isoformat())
    return workout_id


def push_workout(workout: Workout) -> None:
    if not workout.garmin_workout_id:
        raise WorkoutValidationError("Das Workout muss zuerst veröffentlicht werden.")
    connect_garmin().push_workout_to_device(workout.garmin_workout_id)


def _scheduled_workout_ids(client: Any, workout_id: str, scheduled_for: date) -> list[str]:
    calendar = client.get_scheduled_workouts(scheduled_for.year, scheduled_for.month)
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


def _unschedule_workout(client: Any, workout_id: str, scheduled_for: date | None) -> None:
    if scheduled_for is None:
        return
    for scheduled_id in _scheduled_workout_ids(client, workout_id, scheduled_for):
        client.unschedule_workout(scheduled_id)


def update_published_workout(workout: Workout, previous_date: date | None) -> None:
    if not workout.garmin_workout_id:
        raise WorkoutValidationError("Das Workout muss zuerst veröffentlicht werden.")
    client = connect_garmin()
    client.update_workout(workout.garmin_workout_id, _create_model(workout).to_dict())
    if previous_date != workout.scheduled_for:
        _unschedule_workout(client, workout.garmin_workout_id, previous_date)
        if workout.scheduled_for is not None and not _scheduled_workout_ids(
            client, workout.garmin_workout_id, workout.scheduled_for
        ):
            client.schedule_workout(workout.garmin_workout_id, workout.scheduled_for.isoformat())
    client.push_workout_to_device(workout.garmin_workout_id)


def delete_published_workout(workout: Workout) -> None:
    if not workout.garmin_workout_id:
        return
    client = connect_garmin()
    _unschedule_workout(client, workout.garmin_workout_id, workout.scheduled_for)
    client.delete_workout(workout.garmin_workout_id)
