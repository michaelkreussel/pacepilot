from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.models import Activity
from app.repositories.activities import (
    activities_between,
    find_activity_with_history,
    list_activities_on_or_before,
)
from app.repositories.sync_state import sync_states_for_user


@dataclass(frozen=True)
class SportVolume:
    sport: str
    workouts: int
    duration_s: float | None
    distance_m: float | None
    elevation_gain_m: float | None


@dataclass(frozen=True)
class ZoneDistribution:
    sport: str
    zone_type: str
    zone_number: int
    seconds: float


@dataclass(frozen=True)
class TrainingSummary:
    start: date
    end: date
    days: int
    workouts: int
    active_days: int
    training_frequency_per_week: float
    active_weeks: int
    weeks_in_window: int
    consistency_percent: float
    total_duration_s: float | None
    total_elevation_gain_m: float | None
    running_distance_m: float | None
    cycling_distance_m: float | None
    moderate_intensity_minutes: int | None
    vigorous_intensity_minutes: int | None
    exercise_load: float | None
    average_aerobic_training_effect: float | None
    average_anaerobic_training_effect: float | None
    hard_workouts: int
    hard_workouts_per_week: float
    volume_per_sport: tuple[SportVolume, ...]
    zone_distribution: tuple[ZoneDistribution, ...]
    zone_data_complete: bool
    data_status: str
    history_complete: bool
    oldest_synced_date: date | None
    newest_synced_date: date | None


@dataclass(frozen=True)
class WeeklyTrainingPoint:
    week_start: date
    week_end: date
    workouts: int
    duration_s: float | None
    running_distance_m: float | None
    cycling_distance_m: float | None
    exercise_load: float | None
    average_aerobic_training_effect: float | None
    average_anaerobic_training_effect: float | None
    hard_workouts: int
    longest_run_distance_m: float | None
    rolling_28d_duration_s: float | None
    rolling_28d_running_distance_m: float | None


@dataclass(frozen=True)
class TrainingTimelinePoint:
    start: date
    end: date
    partial: bool
    workouts: int
    duration_s: float | None
    running_distance_m: float | None
    cycling_distance_m: float | None
    elevation_gain_m: float | None
    exercise_load: float | None
    average_aerobic_training_effect: float | None
    average_anaerobic_training_effect: float | None
    hard_workouts: int
    longest_run_distance_m: float | None
    rolling_28d_duration_s: float | None
    rolling_28d_running_distance_m: float | None


@dataclass(frozen=True)
class RecentWorkout:
    activity_id: int
    started_at: datetime
    name: str
    sport: str
    duration_s: float | None
    distance_m: float | None
    elevation_gain_m: float | None
    average_hr: int | None
    exercise_load: float | None
    aerobic_training_effect: float | None
    anaerobic_training_effect: float | None
    workout_rpe: int | None


@dataclass(frozen=True)
class ActivityZoneDetail:
    zone_type: str
    zone_number: int
    low_boundary: float | None
    seconds: float | None


@dataclass(frozen=True)
class ActivitySplitDetail:
    split_type: str
    position: int
    intensity_type: str | None
    duration_s: float | None
    distance_m: float | None
    elevation_gain_m: float | None
    average_hr: int | None
    average_power_watts: float | None


@dataclass(frozen=True)
class ActivityExerciseSetDetail:
    position: int
    set_type: str | None
    duration_s: float | None
    repetitions: int | None
    weight_kg: float | None
    exercise_category: str | None
    exercise_name: str | None


@dataclass(frozen=True)
class ActivityDetails:
    workout: RecentWorkout
    calories: int | None
    average_power_watts: float | None
    normalized_power_watts: float | None
    vo2max: float | None
    workout_feel: int | None
    body_battery_change: int | None
    details_complete: bool
    splits_complete: bool
    zones: tuple[ActivityZoneDetail, ...]
    splits: tuple[ActivitySplitDetail, ...]
    exercise_sets: tuple[ActivityExerciseSetDetail, ...]


def _optional_sum(values: list[int | float | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    return round(sum(present), 2) if present else None


def _average(values: list[int | float | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    return round(sum(present) / len(present), 2) if present else None


def _sport_family(sport: str) -> str:
    normalized = sport.lower()
    has_run = "run" in normalized
    has_bike = "cycl" in normalized or "bik" in normalized
    if has_run and has_bike:
        return normalized
    if (
        normalized == "running"
        or normalized.endswith("_running")
        or normalized
        in {
            "trail_run",
            "ultra_run",
            "obstacle_run",
        }
    ):
        return "running"
    if (
        normalized == "cycling"
        or normalized.endswith(("_cycling", "_biking"))
        or normalized
        in {
            "bike",
            "road_bike",
        }
    ):
        return "cycling"
    return normalized


def _family_distance(activities: list[Activity], family: str) -> float | None:
    selected = [
        activity for activity in activities if _sport_family(activity.activity_type) == family
    ]
    if not selected:
        return 0.0
    return _optional_sum([activity.distance_m for activity in selected])


def _is_hard(activity: Activity) -> bool:
    return (
        (activity.aerobic_training_effect is not None and activity.aerobic_training_effect >= 3.5)
        or (
            activity.anaerobic_training_effect is not None
            and activity.anaerobic_training_effect >= 2.5
        )
        or (activity.workout_rpe is not None and activity.workout_rpe >= 7)
    )


def _recent_workout(activity: Activity) -> RecentWorkout:
    return RecentWorkout(
        activity_id=activity.id,
        started_at=activity.started_at,
        name=activity.name,
        sport=activity.activity_type,
        duration_s=activity.duration_s,
        distance_m=activity.distance_m,
        elevation_gain_m=activity.elevation_gain_m,
        average_hr=activity.average_hr,
        exercise_load=activity.exercise_load,
        aerobic_training_effect=activity.aerobic_training_effect,
        anaerobic_training_effect=activity.anaerobic_training_effect,
        workout_rpe=activity.workout_rpe,
    )


def get_training_summary(
    session: Session, user_id: int, *, days: int = 28, as_of: date | None = None
) -> TrainingSummary:
    if days < 1:
        raise ValueError("days must be at least 1")
    end = as_of or date.today()
    start = end - timedelta(days=days - 1)
    activities = activities_between(
        session,
        user_id,
        datetime.combine(start, time.min),
        datetime.combine(end + timedelta(days=1), time.min),
        include_zones=True,
    )
    sync_states = {state.resource: state for state in sync_states_for_user(session, user_id)}
    activity_state = sync_states.get("activities")
    by_sport: dict[str, list[Activity]] = defaultdict(list)
    for activity in activities:
        by_sport[activity.activity_type].append(activity)
    volumes = tuple(
        SportVolume(
            sport=sport,
            workouts=len(items),
            duration_s=_optional_sum([item.duration_s for item in items]),
            distance_m=_optional_sum([item.distance_m for item in items]),
            elevation_gain_m=_optional_sum([item.elevation_gain_m for item in items]),
        )
        for sport, items in sorted(by_sport.items())
    )
    zones: dict[tuple[str, str, int], float] = defaultdict(float)
    for activity in activities:
        for zone in activity.zones:
            if zone.seconds is not None:
                zones[(activity.activity_type, zone.zone_type, zone.zone_number)] += zone.seconds
    weeks_in_window = (days + 6) // 7
    active_weeks = len({(activity.started_at.date() - start).days // 7 for activity in activities})
    hard_workouts = sum(_is_hard(activity) for activity in activities)
    return TrainingSummary(
        start=start,
        end=end,
        days=days,
        workouts=len(activities),
        active_days=len({activity.started_at.date() for activity in activities}),
        training_frequency_per_week=round(len(activities) * 7 / days, 2),
        active_weeks=active_weeks,
        weeks_in_window=weeks_in_window,
        consistency_percent=round(active_weeks * 100 / weeks_in_window, 1),
        total_duration_s=(
            _optional_sum([item.duration_s for item in activities]) if activities else 0.0
        ),
        total_elevation_gain_m=_optional_sum([item.elevation_gain_m for item in activities]),
        running_distance_m=_family_distance(activities, "running"),
        cycling_distance_m=_family_distance(activities, "cycling"),
        moderate_intensity_minutes=(
            int(value)
            if (value := _optional_sum([item.moderate_intensity_minutes for item in activities]))
            is not None
            else None
        ),
        vigorous_intensity_minutes=(
            int(value)
            if (value := _optional_sum([item.vigorous_intensity_minutes for item in activities]))
            is not None
            else None
        ),
        exercise_load=_optional_sum([item.exercise_load for item in activities]),
        average_aerobic_training_effect=_average(
            [item.aerobic_training_effect for item in activities]
        ),
        average_anaerobic_training_effect=_average(
            [item.anaerobic_training_effect for item in activities]
        ),
        hard_workouts=hard_workouts,
        hard_workouts_per_week=round(hard_workouts * 7 / days, 2),
        volume_per_sport=volumes,
        zone_distribution=tuple(
            ZoneDistribution(sport, zone_type, zone_number, round(seconds, 2))
            for (sport, zone_type, zone_number), seconds in sorted(zones.items())
        ),
        zone_data_complete=all(activity.zones_complete for activity in activities),
        data_status=activity_state.status if activity_state else "not_synced",
        history_complete=activity_state.backfill_complete if activity_state else False,
        oldest_synced_date=activity_state.oldest_synced_date if activity_state else None,
        newest_synced_date=(
            min(activity_state.newest_synced_date, end)
            if activity_state and activity_state.newest_synced_date
            else None
        ),
    )


def get_weekly_training_trend(
    session: Session, user_id: int, *, weeks: int = 12, as_of: date | None = None
) -> tuple[WeeklyTrainingPoint, ...]:
    if weeks < 1:
        raise ValueError("weeks must be at least 1")
    end = as_of or date.today()
    current_week_start = end - timedelta(days=end.weekday())
    first_week_start = current_week_start - timedelta(weeks=weeks - 1)
    query_start = first_week_start - timedelta(days=27)
    activities = activities_between(
        session,
        user_id,
        datetime.combine(query_start, time.min),
        datetime.combine(end + timedelta(days=1), time.min),
    )
    points: list[WeeklyTrainingPoint] = []
    for offset in range(weeks):
        week_start = first_week_start + timedelta(weeks=offset)
        week_end = min(week_start + timedelta(days=6), end)
        weekly = [item for item in activities if week_start <= item.started_at.date() <= week_end]
        rolling_start = week_end - timedelta(days=27)
        rolling = [
            item for item in activities if rolling_start <= item.started_at.date() <= week_end
        ]
        runs = [item for item in weekly if _sport_family(item.activity_type) == "running"]
        points.append(
            WeeklyTrainingPoint(
                week_start=week_start,
                week_end=week_end,
                workouts=len(weekly),
                duration_s=(_optional_sum([item.duration_s for item in weekly]) if weekly else 0.0),
                running_distance_m=_family_distance(weekly, "running"),
                cycling_distance_m=_family_distance(weekly, "cycling"),
                exercise_load=_optional_sum([item.exercise_load for item in weekly]),
                average_aerobic_training_effect=_average(
                    [item.aerobic_training_effect for item in weekly]
                ),
                average_anaerobic_training_effect=_average(
                    [item.anaerobic_training_effect for item in weekly]
                ),
                hard_workouts=sum(_is_hard(item) for item in weekly),
                longest_run_distance_m=(
                    max(item.distance_m for item in runs if item.distance_m is not None)
                    if any(item.distance_m is not None for item in runs)
                    else (0.0 if not runs else None)
                ),
                rolling_28d_duration_s=(
                    _optional_sum([item.duration_s for item in rolling]) if rolling else 0.0
                ),
                rolling_28d_running_distance_m=_family_distance(rolling, "running"),
            )
        )
    return tuple(points)


def get_training_timeline(
    session: Session,
    user_id: int,
    *,
    days: int = 28,
    bucket_days: int = 7,
    as_of: date | None = None,
) -> tuple[TrainingTimelinePoint, ...]:
    if days < 1:
        raise ValueError("days must be at least 1")
    if bucket_days < 1:
        raise ValueError("bucket_days must be at least 1")
    end = as_of or date.today()
    start = end - timedelta(days=days - 1)
    activities = activities_between(
        session,
        user_id,
        datetime.combine(start - timedelta(days=27), time.min),
        datetime.combine(end + timedelta(days=1), time.min),
    )
    points: list[TrainingTimelinePoint] = []
    bucket_start = start
    while bucket_start <= end:
        bucket_end = min(bucket_start + timedelta(days=bucket_days - 1), end)
        bucket = [
            item for item in activities if bucket_start <= item.started_at.date() <= bucket_end
        ]
        rolling_start = bucket_end - timedelta(days=27)
        rolling = [
            item for item in activities if rolling_start <= item.started_at.date() <= bucket_end
        ]
        runs = [item for item in bucket if _sport_family(item.activity_type) == "running"]
        points.append(
            TrainingTimelinePoint(
                start=bucket_start,
                end=bucket_end,
                partial=(bucket_end - bucket_start).days + 1 < bucket_days,
                workouts=len(bucket),
                duration_s=(_optional_sum([item.duration_s for item in bucket]) if bucket else 0.0),
                running_distance_m=_family_distance(bucket, "running"),
                cycling_distance_m=_family_distance(bucket, "cycling"),
                elevation_gain_m=(
                    _optional_sum([item.elevation_gain_m for item in bucket]) if bucket else 0.0
                ),
                exercise_load=_optional_sum([item.exercise_load for item in bucket]),
                average_aerobic_training_effect=_average(
                    [item.aerobic_training_effect for item in bucket]
                ),
                average_anaerobic_training_effect=_average(
                    [item.anaerobic_training_effect for item in bucket]
                ),
                hard_workouts=sum(_is_hard(item) for item in bucket),
                longest_run_distance_m=(
                    max(item.distance_m for item in runs if item.distance_m is not None)
                    if any(item.distance_m is not None for item in runs)
                    else (0.0 if not runs else None)
                ),
                rolling_28d_duration_s=(
                    _optional_sum([item.duration_s for item in rolling]) if rolling else 0.0
                ),
                rolling_28d_running_distance_m=_family_distance(rolling, "running"),
            )
        )
        bucket_start = bucket_end + timedelta(days=1)
    return tuple(points)


def get_recent_workouts(
    session: Session,
    user_id: int,
    *,
    limit: int = 10,
    as_of: date | None = None,
) -> tuple[RecentWorkout, ...]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    end = as_of or date.today()
    through = datetime.combine(end, time.max)
    return tuple(
        _recent_workout(activity)
        for activity in list_activities_on_or_before(session, user_id, through, limit)
    )


def get_activity_details(
    session: Session,
    user_id: int,
    activity_id: int,
    *,
    as_of: date | None = None,
) -> ActivityDetails | None:
    activity = find_activity_with_history(session, user_id, activity_id)
    if activity is None or activity.started_at.date() > (as_of or date.today()):
        return None
    return ActivityDetails(
        workout=_recent_workout(activity),
        calories=activity.calories,
        average_power_watts=activity.average_power_watts,
        normalized_power_watts=activity.normalized_power_watts,
        vo2max=activity.vo2max,
        workout_feel=activity.workout_feel,
        body_battery_change=activity.body_battery_change,
        details_complete=activity.details_complete,
        splits_complete=activity.splits_complete,
        zones=tuple(
            ActivityZoneDetail(zone.zone_type, zone.zone_number, zone.low_boundary, zone.seconds)
            for zone in sorted(activity.zones, key=lambda item: (item.zone_type, item.zone_number))
        ),
        splits=tuple(
            ActivitySplitDetail(
                split.split_type,
                split.position,
                split.intensity_type,
                split.duration_s,
                split.distance_m,
                split.elevation_gain_m,
                split.average_hr,
                split.average_power_watts,
            )
            for split in sorted(activity.splits, key=lambda item: (item.split_type, item.position))
        ),
        exercise_sets=tuple(
            ActivityExerciseSetDetail(
                item.position,
                item.set_type,
                item.duration_s,
                item.repetitions,
                item.weight_kg,
                item.exercise_category,
                item.exercise_name,
            )
            for item in activity.exercise_sets
        ),
    )
