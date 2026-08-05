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
        CyclingWorkout,
        HikingWorkout,
        RunningWorkout,
        WalkingWorkout,
        WorkoutSegment,
        create_cooldown_step,
        create_distance_interval_step,
        create_interval_step,
        create_recovery_step,
        create_warmup_step,
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
    output_steps: list[Any] = []
    order = 1
    for step in workout.steps:
        repetitions = step.repeat_count or 1
        for _ in range(repetitions):
            value = float(step.duration_value or 0)
            if step.step_type == "warmup":
                output_steps.append(create_warmup_step(value, order))
            elif step.step_type == "cooldown":
                output_steps.append(create_cooldown_step(value, order))
            elif step.step_type == "recovery":
                output_steps.append(create_recovery_step(value, order))
            elif step.duration_type == "distance":
                output_steps.append(create_distance_interval_step(value, order))
            else:
                output_steps.append(create_interval_step(value, order))
            order += 1
    estimated_seconds = int(
        sum(
            (step.duration_value or 0) * (step.repeat_count or 1)
            for step in workout.steps
            if step.duration_type == "time"
        )
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
