import gzip
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Any

from garminconnect import Garmin
from garminconnect.exceptions import GarminConnectNotFoundError
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Activity, ActivityExerciseSet, ActivitySplit, ActivityZone, Workout
from app.models.user import utcnow
from app.repositories.activities import (
    find_activity_by_garmin_id,
    get_or_create_activity,
    replace_activity_exercise_sets,
    replace_activity_splits,
    replace_activity_zones,
)
from app.repositories.sync_state import (
    get_or_create_sync_state,
    mark_sync_attempt,
    mark_sync_error,
    mark_sync_success,
)
from app.services.garmin.activity_details import activity_details_path, write_activity_details
from app.services.garmin.activity_fit import (
    FIT_RUNNING_TYPES,
    activity_fit_path,
    extract_original_fit,
    fit_eligible_activity_type,
    write_activity_fit,
)
from app.services.garmin.client import message_from_exception
from app.services.garmin.health_backfill import GarminPacer

logger = logging.getLogger(__name__)

ACTIVITY_RESOURCE = "activities"
DEFAULT_PAGE_SIZE = 100
ProgressCallback = Callable[[str, int, int], None]
CompletionCallback = Callable[[str, int, int, str, date | None, float], None]

FINGERPRINT_FIELDS = (
    "activityId",
    "activityName",
    "activityType",
    "startTimeLocal",
    "startTimeGMT",
    "duration",
    "elapsedDuration",
    "movingDuration",
    "distance",
    "averageSpeed",
    "maxSpeed",
    "minHR",
    "averageHR",
    "maxHR",
    "calories",
    "elevationGain",
    "elevationLoss",
    "averageRunningCadenceInStepsPerMinute",
    "maxRunningCadenceInStepsPerMinute",
    "averageBikingCadenceInRevPerMinute",
    "maxBikingCadenceInRevPerMinute",
    "avgPower",
    "maxPower",
    "normPower",
    "aerobicTrainingEffect",
    "anaerobicTrainingEffect",
    "trainingEffectLabel",
    "activityTrainingLoad",
    "vO2MaxValue",
    "avgStrideLength",
    "avgGroundContactTime",
    "avgVerticalOscillation",
    "avgVerticalRatio",
    "moderateIntensityMinutes",
    "vigorousIntensityMinutes",
    "differenceBodyBattery",
    "directWorkoutRpe",
    "directWorkoutFeel",
    "hasSplits",
    "hasIntensityIntervals",
    "hrTimeInZone_1",
    "hrTimeInZone_2",
    "hrTimeInZone_3",
    "hrTimeInZone_4",
    "hrTimeInZone_5",
    "powerTimeInZone_1",
    "powerTimeInZone_2",
    "powerTimeInZone_3",
    "powerTimeInZone_4",
    "powerTimeInZone_5",
    "splitSummaries",
)


@dataclass
class ActivitySyncResult:
    remote_count: int = 0
    api_calls: int = 0
    activities_seen: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    details_stored: int = 0
    activities_enriched: int = 0
    enrichment_deferred: int = 0
    oldest: date | None = None
    newest: date | None = None
    backfill_complete: bool = False


def _number(data: Any, *keys: str) -> float | None:
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _integer(data: Any, *keys: str) -> int | None:
    value = _number(data, *keys)
    return round(value) if value is not None else None


def _workout_rpe(data: Any) -> int | None:
    value = _integer(data, "directWorkoutRpe")
    if value is None:
        return None
    # Garmin usually exposes perceived effort as 10-100 despite presenting it as 1-10.
    return round(value / 10) if value > 10 else value


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            timestamp = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(timestamp, UTC).replace(tzinfo=None)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


def _activity_type(item: dict[str, Any]) -> str:
    value = item.get("activityType") or item.get("activityTypeDTO")
    if isinstance(value, dict):
        return str(value.get("typeKey") or "other")
    return str(value or "other")


def activity_fingerprint(item: dict[str, Any]) -> str:
    relevant = {key: item.get(key) for key in FINGERPRINT_FIELDS if key in item}
    serialized = json.dumps(relevant, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()


def _raw_activity_path(user_id: int, started_at: datetime, activity_id: str) -> Path:
    return (
        get_settings().data_dir
        / "raw"
        / "activities"
        / f"user-{user_id}"
        / str(started_at.year)
        / f"{activity_id}.json.gz"
    )


def _write_raw_activity(
    user_id: int, started_at: datetime, activity_id: str, item: dict[str, Any]
) -> str:
    path = _raw_activity_path(user_id, started_at, activity_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as raw_file:
        json.dump(item, raw_file, ensure_ascii=False)
    temporary.replace(path)
    return str(path)


def _status_code(exc: Exception) -> int | None:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    match = re.search(r"(?:API Error|Error|HTTP|client error)\D*(\d{3})", str(exc))
    return int(match.group(1)) if match else None


def _optional_call(
    pacer: GarminPacer,
    result: ActivitySyncResult,
    operation: str,
    call: Any,
    empty: Any,
) -> Any:
    try:
        value = pacer.call(operation, call)
        result.api_calls += 1
        return value
    except Exception as exc:
        if isinstance(exc, GarminConnectNotFoundError) or _status_code(exc) == 404:
            result.api_calls += 1
            logger.info(
                "Optional Garmin activity resource skipped",
                extra={
                    "garmin_operation": operation,
                    "skip_reason": "not_found",
                    "http_status": 404,
                },
            )
            return empty
        raise


def _map_activity_summary(activity: Activity, item: dict[str, Any]) -> None:
    activity.name = str(item.get("activityName") or activity.name or "Garmin-Aktivität")
    activity.activity_type = _activity_type(item)
    activity.distance_m = _number(item, "distance")
    activity.duration_s = _number(item, "duration")
    activity.elapsed_duration_s = _number(item, "elapsedDuration")
    activity.moving_duration_s = _number(item, "movingDuration")
    activity.average_speed_mps = _number(item, "averageSpeed", "averageMovingSpeed")
    activity.max_speed_mps = _number(item, "maxSpeed")
    activity.min_hr = _integer(item, "minHR")
    activity.average_hr = _integer(item, "averageHR")
    activity.max_hr = _integer(item, "maxHR")
    activity.calories = _integer(item, "calories")
    activity.elevation_gain_m = _number(item, "elevationGain")
    activity.elevation_loss_m = _number(item, "elevationLoss")
    activity.average_cadence = _number(
        item,
        "averageRunningCadenceInStepsPerMinute",
        "averageBikingCadenceInRevPerMinute",
        "averageRunCadence",
    )
    activity.max_cadence = _number(
        item,
        "maxRunningCadenceInStepsPerMinute",
        "maxBikingCadenceInRevPerMinute",
        "maxRunCadence",
    )
    activity.average_power_watts = _number(item, "avgPower", "averagePower")
    activity.max_power_watts = _number(item, "maxPower")
    activity.normalized_power_watts = _number(item, "normPower", "normalizedPower")
    activity.aerobic_training_effect = _number(item, "aerobicTrainingEffect", "trainingEffect")
    activity.anaerobic_training_effect = _number(item, "anaerobicTrainingEffect")
    label = item.get("trainingEffectLabel")
    activity.training_effect_label = str(label) if label is not None else None
    activity.exercise_load = _number(item, "activityTrainingLoad", "exerciseLoad")
    activity.vo2max = _number(item, "vO2MaxValue", "vo2MaxValue")
    activity.stride_length_cm = _number(item, "avgStrideLength", "strideLength")
    activity.ground_contact_time_ms = _number(item, "avgGroundContactTime", "groundContactTime")
    activity.vertical_oscillation_cm = _number(
        item, "avgVerticalOscillation", "verticalOscillation"
    )
    activity.vertical_ratio = _number(item, "avgVerticalRatio", "verticalRatio")
    activity.workout_rpe = _workout_rpe(item)
    activity.workout_feel = _integer(item, "directWorkoutFeel")
    activity.moderate_intensity_minutes = _integer(item, "moderateIntensityMinutes")
    activity.vigorous_intensity_minutes = _integer(item, "vigorousIntensityMinutes")
    activity.body_battery_change = _integer(item, "differenceBodyBattery")


def _map_detail_summary(session: Session, activity: Activity, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    summary = payload.get("summaryDTO")
    if isinstance(summary, dict):
        for attribute, keys, integer in (
            ("workout_feel", ("directWorkoutFeel",), True),
            ("aerobic_training_effect", ("trainingEffect",), False),
            ("anaerobic_training_effect", ("anaerobicTrainingEffect",), False),
            ("average_power_watts", ("averagePower",), False),
            ("max_power_watts", ("maxPower",), False),
            ("normalized_power_watts", ("normalizedPower",), False),
        ):
            value = _integer(summary, *keys) if integer else _number(summary, *keys)
            if value is not None:
                setattr(activity, attribute, value)
        workout_rpe = _workout_rpe(summary)
        if workout_rpe is not None:
            activity.workout_rpe = workout_rpe
    metadata = payload.get("metadataDTO")
    if not isinstance(metadata, dict):
        metadata = {}
    associated = metadata.get("associatedWorkoutId")
    activity.associated_garmin_workout_id = str(associated) if associated is not None else None
    activity.workout_id = None
    activity.source_updated_at = _parse_datetime(metadata.get("lastUpdateDate"))
    if activity.associated_garmin_workout_id is not None:
        workout_id = session.scalar(
            select(Workout.id).where(
                Workout.user_id == activity.user_id,
                Workout.garmin_workout_id == activity.associated_garmin_workout_id,
            )
        )
        activity.workout_id = workout_id
    return metadata


def _map_zone(item: Any, zone_type: str) -> ActivityZone | None:
    if not isinstance(item, dict):
        return None
    zone_number = _integer(item, "zoneNumber")
    if zone_number is None:
        return None
    return ActivityZone(
        zone_type=zone_type,
        zone_number=zone_number,
        low_boundary=_number(item, "zoneLowBoundary"),
        seconds=_number(item, "secsInZone"),
    )


def _map_split(item: Any, split_type: str, position: int) -> ActivitySplit | None:
    if not isinstance(item, dict):
        return None
    return ActivitySplit(
        split_type=split_type[:30],
        position=position,
        intensity_type=(str(item["intensityType"])[:30] if item.get("intensityType") else None),
        started_at=_parse_datetime(item.get("startTimeGMT") or item.get("startTimeLocal")),
        duration_s=_number(item, "duration"),
        elapsed_duration_s=_number(item, "elapsedDuration"),
        moving_duration_s=_number(item, "movingDuration"),
        distance_m=_number(item, "distance"),
        elevation_gain_m=_number(item, "elevationGain"),
        elevation_loss_m=_number(item, "elevationLoss"),
        average_hr=_integer(item, "averageHR"),
        max_hr=_integer(item, "maxHR"),
        average_speed_mps=_number(item, "averageSpeed", "averageMovingSpeed"),
        max_speed_mps=_number(item, "maxSpeed"),
        average_cadence=_number(item, "averageRunCadence", "avgStepFrequency"),
        max_cadence=_number(item, "maxRunCadence"),
        average_power_watts=_number(item, "averagePower"),
        max_power_watts=_number(item, "maxPower"),
        normalized_power_watts=_number(item, "normalizedPower"),
        stride_length_cm=_number(item, "strideLength", "avgStepLength"),
        ground_contact_time_ms=_number(item, "groundContactTime", "avgGroundContactTime"),
        vertical_oscillation_cm=_number(item, "verticalOscillation"),
        vertical_ratio=_number(item, "verticalRatio"),
    )


def _split_rows(laps_payload: Any, typed_payload: Any) -> list[ActivitySplit]:
    rows: list[ActivitySplit] = []
    laps = laps_payload.get("lapDTOs") if isinstance(laps_payload, dict) else []
    if isinstance(laps, list):
        rows.extend(
            split
            for position, item in enumerate(laps)
            if (split := _map_split(item, "lap", position)) is not None
        )
    typed = typed_payload.get("splits") if isinstance(typed_payload, dict) else []
    if isinstance(typed, list):
        positions: dict[str, int] = {}
        for item in typed:
            raw_type = str(item.get("type") or "split") if isinstance(item, dict) else "split"
            split_type = f"typed_{raw_type}"[:30]
            position = positions.get(split_type, 0)
            split = _map_split(item, split_type, position)
            if split is not None:
                rows.append(split)
                positions[split_type] = position + 1
    return rows


def _exercise_rows(payload: Any) -> list[ActivityExerciseSet]:
    raw_sets = payload.get("exerciseSets") if isinstance(payload, dict) else []
    if not isinstance(raw_sets, list):
        return []
    rows: list[ActivityExerciseSet] = []
    for item in raw_sets:
        if not isinstance(item, dict):
            continue
        exercises = item.get("exercises")
        exercise = exercises[0] if isinstance(exercises, list) and exercises else {}
        exercise = exercise if isinstance(exercise, dict) else {}
        rows.append(
            ActivityExerciseSet(
                position=len(rows),
                set_type=str(item.get("setType"))[:30] if item.get("setType") else None,
                started_at=_parse_datetime(item.get("startTime")),
                duration_s=_number(item, "duration"),
                repetitions=_integer(item, "repetitionCount"),
                weight_kg=_number(item, "weight"),
                exercise_category=(
                    str(exercise.get("category"))[:100] if exercise.get("category") else None
                ),
                exercise_name=(str(exercise.get("name"))[:150] if exercise.get("name") else None),
                workout_step_index=_integer(item, "wktStepIndex"),
            )
        )
    return rows


def _files_complete(activity: Activity) -> bool:
    if activity.raw_file is None or not Path(activity.raw_file).is_file():
        return False
    if activity.details_file is not None and not Path(activity.details_file).is_file():
        return False
    return activity.fit_file is None or Path(activity.fit_file).is_file()


def _fit_eligible(activity: Activity) -> bool:
    return (
        fit_eligible_activity_type(activity.activity_type)
        and (activity.distance_m or 0) >= 1_000
        and (activity.duration_s or 0) >= 600
    )


def _fit_complete(activity: Activity) -> bool:
    if not _fit_eligible(activity):
        return True
    if activity.fit_import_status == "available":
        return activity.fit_file is not None and Path(activity.fit_file).is_file()
    return activity.fit_import_status == "unavailable"


def _import_original_fit(
    client: Any,
    activity: Activity,
    pacer: GarminPacer,
    result: ActivitySyncResult,
) -> None:
    if not _fit_eligible(activity) or _fit_complete(activity):
        return
    download = getattr(client, "download_activity", None)
    payload = (
        _optional_call(
            pacer,
            result,
            f"activity original {activity.garmin_activity_id}",
            partial(
                download,
                activity.garmin_activity_id,
                Garmin.ActivityDownloadFormat.ORIGINAL,
            ),
            b"",
        )
        if callable(download)
        else b""
    )
    fit_data = extract_original_fit(payload) if isinstance(payload, bytes) else None
    path = activity_fit_path(
        activity.started_at,
        activity.garmin_activity_id,
        activity.user_id,
        get_settings().data_dir,
    )
    if fit_data is None:
        path.unlink(missing_ok=True)
        activity.fit_file = None
        activity.fit_import_status = "unavailable"
    else:
        write_activity_fit(path, fit_data)
        activity.fit_file = str(path)
        activity.fit_import_status = "available"
    activity.fit_synced_at = utcnow()


def _process_activity(
    session: Session,
    client: Any,
    user_id: int,
    item: dict[str, Any],
    pacer: GarminPacer,
    result: ActivitySyncResult,
    *,
    enrich: bool,
) -> tuple[str, bool]:
    activity_id = str(item.get("activityId") or "")
    started_at = _parse_datetime(item.get("startTimeLocal") or item.get("startTimeGMT"))
    if not activity_id or started_at is None:
        logger.warning(
            "Malformed Garmin activity skipped",
            extra={
                "garmin_activity_id": activity_id or None,
                "has_activity_id": bool(activity_id),
                "has_valid_start_time": started_at is not None,
            },
        )
        return "skipped", False
    fingerprint = activity_fingerprint(item)
    existing = find_activity_by_garmin_id(session, user_id, activity_id)
    summary_unchanged = (
        existing is not None
        and existing.source_fingerprint == fingerprint
        and existing.raw_file is not None
        and Path(existing.raw_file).is_file()
    )
    enrichment_complete = (
        existing is not None
        and existing.details_complete
        and existing.splits_complete
        and _fit_complete(existing)
        and _files_complete(existing)
    )
    if summary_unchanged:
        if enrichment_complete:
            return "skipped", False
        if not enrich:
            result.enrichment_deferred += 1
            return "skipped", False

    outcome = "inserted" if existing is None else "updated"
    activity = get_or_create_activity(
        session,
        user_id,
        activity_id,
        name=str(item.get("activityName") or "Garmin-Aktivität"),
        activity_type=_activity_type(item),
        started_at=started_at,
    )
    if not summary_unchanged:
        activity.started_at = started_at
        _map_activity_summary(activity, item)
        activity.raw_file = _write_raw_activity(user_id, started_at, activity_id, item)

    activity.source_fingerprint = fingerprint
    activity.synced_at = utcnow()
    if not enrich:
        if existing is not None and not summary_unchanged:
            if activity.fit_file is not None:
                Path(activity.fit_file).unlink(missing_ok=True)
            activity.details_file = None
            activity.fit_file = None
            activity.fit_import_status = None
            activity.fit_synced_at = None
            activity.details_complete = False
            activity.splits_complete = False
            replace_activity_splits(session, activity, [])
            replace_activity_zones(session, activity, [])
            replace_activity_exercise_sets(session, activity, [])
        result.enrichment_deferred += 1
        return outcome, False

    summary_payload = _optional_call(
        pacer,
        result,
        f"activity summary {activity_id}",
        partial(client.get_activity, activity_id),
        {},
    )
    metadata = _map_detail_summary(session, activity, summary_payload)

    details = _optional_call(
        pacer,
        result,
        f"activity details {activity_id}",
        partial(client.get_activity_details, activity_id, maxchart=2000, maxpoly=2000),
        {},
    )
    details_path = activity_details_path(started_at, activity_id, user_id)
    if isinstance(details, dict) and details.get("detailsAvailable") is not False and details:
        write_activity_details(details_path, details)
        activity.details_file = str(details_path)
        result.details_stored += 1
    else:
        details_path.unlink(missing_ok=True)
        activity.details_file = None
    activity.details_complete = True

    laps_payload = _optional_call(
        pacer,
        result,
        f"activity laps {activity_id}",
        partial(client.get_activity_splits, activity_id),
        {},
    )
    typed_payload = _optional_call(
        pacer,
        result,
        f"activity typed splits {activity_id}",
        partial(client.get_activity_typed_splits, activity_id),
        {},
    )
    replace_activity_splits(session, activity, _split_rows(laps_payload, typed_payload))

    zones: list[ActivityZone] = []
    has_hr_zones = bool(metadata.get("hasHrTimeInZones") or item.get("hasHrTimeInZones"))
    if has_hr_zones:
        hr_payload = _optional_call(
            pacer,
            result,
            f"activity HR zones {activity_id}",
            partial(client.get_activity_hr_in_timezones, activity_id),
            [],
        )
        zones.extend(
            zone for raw in hr_payload or [] if (zone := _map_zone(raw, "heart_rate")) is not None
        )
    has_power_zones = bool(metadata.get("hasPowerTimeInZones") or item.get("hasPowerTimeInZones"))
    if has_power_zones:
        power_payload = _optional_call(
            pacer,
            result,
            f"activity power zones {activity_id}",
            partial(client.get_activity_power_in_timezones, activity_id),
            [],
        )
        zones.extend(
            zone for raw in power_payload or [] if (zone := _map_zone(raw, "power")) is not None
        )
    replace_activity_zones(session, activity, zones)

    exercise_payload: Any = {}
    if "strength" in activity.activity_type:
        exercise_payload = _optional_call(
            pacer,
            result,
            f"activity exercise sets {activity_id}",
            partial(client.get_activity_exercise_sets, activity_id),
            {},
        )
    replace_activity_exercise_sets(session, activity, _exercise_rows(exercise_payload))
    _import_original_fit(client, activity, pacer, result)

    activity.details_synced_at = utcnow()
    activity.details_complete = True
    activity.splits_complete = True
    result.activities_enriched += 1
    return outcome, True


def _activity_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("activityList"), list):
        return [item for item in payload["activityList"] if isinstance(item, dict)]
    return []


def sync_activity_history(
    session: Session,
    client: Any,
    user_id: int,
    *,
    delay: float = 0.75,
    page_size: int = DEFAULT_PAGE_SIZE,
    initial_enrichment_limit: int = 0,
    incremental_enrichment_limit: int = 5,
    pacer: GarminPacer | None = None,
    progress: ProgressCallback | None = None,
    completion: CompletionCallback | None = None,
    log_context: Mapping[str, Any] | None = None,
) -> ActivitySyncResult:
    if not 1 <= page_size <= 1000:
        raise ValueError("page_size must be between 1 and 1000")
    if initial_enrichment_limit < 0 or incremental_enrichment_limit < 0:
        raise ValueError("activity enrichment limits cannot be negative")
    result = ActivitySyncResult()
    request_pacer = pacer or GarminPacer(delay, log_context)
    state = get_or_create_sync_state(session, user_id, ACTIVITY_RESOURCE)
    initial = not state.backfill_complete
    mark_sync_attempt(state)
    session.commit()
    try:
        result.remote_count = int(request_pacer.call("activity count", client.count_activities))
        result.api_calls += 1
        offset = int(state.cursor or 0) if initial else 0
        enrichment_remaining = initial_enrichment_limit if initial else incremental_enrichment_limit
        search_deferred = (
            not initial
            and enrichment_remaining > 0
            and session.scalar(
                select(Activity.id)
                .where(
                    Activity.user_id == user_id,
                    or_(
                        Activity.details_complete.is_(False),
                        Activity.splits_complete.is_(False),
                        and_(
                            Activity.fit_import_status.is_(None),
                            Activity.activity_type.in_(FIT_RUNNING_TYPES),
                            Activity.distance_m >= 1_000,
                            Activity.duration_s >= 600,
                        ),
                    ),
                )
                .limit(1)
            )
            is not None
        )
        while offset < result.remote_count or (not initial and offset == 0):
            payload = request_pacer.call(
                f"activities {offset} to {offset + page_size - 1}",
                partial(client.get_activities, offset, page_size),
            )
            result.api_calls += 1
            items = _activity_items(payload)
            if not items:
                break
            changed_on_page = 0
            for item in items:
                activity_id = str(item.get("activityId") or "")
                if progress is not None:
                    progress(activity_id, result.activities_seen + 1, result.remote_count)
                operation_started = time.perf_counter()
                outcome, enriched = _process_activity(
                    session,
                    client,
                    user_id,
                    item,
                    request_pacer,
                    result,
                    enrich=enrichment_remaining > 0,
                )
                enrichment_remaining -= int(enriched)
                result.activities_seen += 1
                if outcome == "inserted":
                    result.inserted += 1
                    changed_on_page += 1
                elif outcome == "updated":
                    result.updated += 1
                    changed_on_page += 1
                else:
                    result.skipped += 1
                started_at = _parse_datetime(item.get("startTimeLocal") or item.get("startTimeGMT"))
                if started_at is not None:
                    day = started_at.date()
                    result.oldest = day if result.oldest is None else min(result.oldest, day)
                    result.newest = day if result.newest is None else max(result.newest, day)
                session.commit()
                if completion is not None:
                    completion(
                        activity_id,
                        result.activities_seen,
                        result.remote_count,
                        outcome,
                        started_at.date() if started_at is not None else None,
                        (time.perf_counter() - operation_started) * 1000,
                    )

            offset += len(items)
            mark_sync_success(
                state,
                oldest_date=result.oldest,
                newest_date=result.newest,
                cursor=str(offset) if initial else None,
            )
            session.commit()
            if len(items) < page_size:
                break
            if (
                not initial
                and changed_on_page < len(items)
                and (not search_deferred or enrichment_remaining <= 0)
            ):
                break

        mark_sync_success(
            state,
            oldest_date=result.oldest,
            newest_date=result.newest,
            cursor=None,
            backfill_complete=True,
        )
        session.commit()
        result.backfill_complete = True
        return result
    except Exception as exc:
        session.rollback()
        failed_state = get_or_create_sync_state(session, user_id, ACTIVITY_RESOURCE)
        mark_sync_error(failed_state, message_from_exception(exc))
        session.commit()
        raise
