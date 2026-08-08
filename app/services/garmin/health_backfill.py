import logging
import random
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any

from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)
from sqlalchemy.orm import Session

from app.models import GarminSyncState, SleepStage
from app.models.user import utcnow
from app.repositories.fitness import get_or_create_daily_fitness
from app.repositories.health import (
    get_or_create_health_day,
    replace_sleep_stages,
    set_daily_data_status,
)
from app.repositories.sync_state import (
    get_or_create_sync_state,
    mark_sync_attempt,
    mark_sync_error,
    mark_sync_success,
)
from app.services.garmin.client import message_from_exception

logger = logging.getLogger("uvicorn.error")

MIN_HISTORY_DATE = date(2005, 1, 1)
DEFAULT_OVERLAP_DAYS = 7
EMPTY_RESOURCE_REPROBE_DAYS = 28
BODY_BATTERY_RANGE_DAYS = 31

ProgressCallback = Callable[[str, date], None]
StorePayload = Callable[[Session, int, date, Any], bool]
HasData = Callable[[Any], bool]


@dataclass(frozen=True)
class DailyResource:
    name: str
    fetch: Callable[[str], Any]
    has_data: HasData
    store: StorePayload


@dataclass
class ResourceSyncStats:
    api_calls: int = 0
    days_processed: int = 0
    populated_days: int = 0
    empty_days: int = 0
    earliest: date | None = None
    latest: date | None = None
    complete: bool = False


@dataclass
class HealthSyncResult:
    resources: dict[str, ResourceSyncStats] = field(default_factory=dict)

    @property
    def api_calls(self) -> int:
        return sum(stats.api_calls for stats in self.resources.values())

    @property
    def days_processed(self) -> int:
        return sum(stats.days_processed for stats in self.resources.values())

    @property
    def unique_days_processed(self) -> int:
        days: set[date] = set()
        for stats in self.resources.values():
            if stats.earliest is None or stats.latest is None:
                continue
            day = stats.earliest
            while day <= stats.latest:
                days.add(day)
                day += timedelta(days=1)
        return len(days)


class GarminPacer:
    def __init__(self, delay: float) -> None:
        self.delay = max(delay, 0)
        self.last_call_at: float | None = None

    def call(self, operation: str, call: Callable[[], Any]) -> Any:
        if self.last_call_at is not None and self.delay:
            elapsed = time.monotonic() - self.last_call_at
            wait = self.delay * random.uniform(0.9, 1.1) - elapsed  # noqa: S311
            if wait > 0:
                time.sleep(wait)
        logger.info("Garmin health sync: %s", operation)
        try:
            return call()
        finally:
            self.last_call_at = time.monotonic()


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _first_number(value: Any, *keys: str) -> int | float | None:
    wanted = set(keys)
    if isinstance(value, dict):
        for key, child in value.items():
            if key in wanted and (number := _number(child)) is not None:
                return number
            if (number := _first_number(child, *keys)) is not None:
                return number
    elif isinstance(value, list):
        for child in value:
            if (number := _first_number(child, *keys)) is not None:
                return number
    return None


def _first_int(value: Any, *keys: str) -> int | None:
    number = _first_number(value, *keys)
    return round(number) if number is not None else None


def _first_text(value: Any, *keys: str) -> str | None:
    wanted = set(keys)
    if isinstance(value, dict):
        for key, child in value.items():
            if key in wanted and isinstance(child, str) and child:
                return child
            if (text := _first_text(child, *keys)) is not None:
                return text
    elif isinstance(value, list):
        for child in value:
            if (text := _first_text(child, *keys)) is not None:
                return text
    return None


def _positive(value: Any, *keys: str) -> bool:
    number = _first_number(value, *keys)
    return number is not None and number > 0


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value) / 1000 if value > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(timestamp, UTC).replace(tzinfo=None)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _duration_seconds(value: object) -> int | None:
    number = _number(value)
    if number is None or number < 0:
        return None
    return round(number)


def _sleep_need_seconds(value: object) -> int | None:
    number = _number(value)
    if number is None or number < 0:
        return None
    return round(number * 60) if number <= 24 * 60 else round(number)


def _clear_fields(record: Any, fields: Iterable[str]) -> None:
    for name in fields:
        setattr(record, name, None)


SUMMARY_FIELDS = (
    "steps",
    "distance_m",
    "total_calories",
    "active_calories",
    "resting_hr",
    "min_hr",
    "max_hr",
    "stress_average",
    "stress_max",
    "body_battery_high",
    "body_battery_low",
    "waking_respiration_average",
    "moderate_intensity_minutes",
    "vigorous_intensity_minutes",
)

SLEEP_FIELDS = (
    "sleep_seconds",
    "sleep_score",
    "sleep_score_duration",
    "sleep_score_stress",
    "sleep_score_awake_count",
    "sleep_score_rem_percentage",
    "sleep_score_restlessness",
    "sleep_score_light_percentage",
    "sleep_score_deep_percentage",
    "sleep_start_at",
    "sleep_end_at",
    "deep_sleep_seconds",
    "light_sleep_seconds",
    "rem_sleep_seconds",
    "awake_sleep_seconds",
    "nap_seconds",
    "sleep_need_seconds",
    "sleep_need_baseline_seconds",
    "next_sleep_need_seconds",
    "sleep_average_hr",
    "sleep_average_stress",
    "sleep_body_battery_change",
    "sleep_respiration_average",
)

HRV_FIELDS = (
    "hrv_average",
    "hrv_weekly_average",
    "hrv_five_min_high",
    "hrv_status",
    "hrv_baseline_low",
    "hrv_baseline_balanced_low",
    "hrv_baseline_balanced_high",
)

SPO2_FIELDS = ("spo2_average", "sleep_spo2_average", "spo2_lowest")


def _summary_has_data(payload: Any) -> bool:
    return any(
        _positive(payload, key)
        for key in (
            "totalSteps",
            "totalDistanceMeters",
            "activeKilocalories",
            "restingHeartRate",
            "minHeartRate",
            "maxHeartRate",
            "activeSeconds",
            "averageStressLevel",
            "bodyBatteryHighestValue",
        )
    )


def _store_summary(session: Session, user_id: int, day: date, payload: Any) -> bool:
    health = get_or_create_health_day(session, user_id, day)
    _clear_fields(health, SUMMARY_FIELDS)
    if not isinstance(payload, dict) or not _summary_has_data(payload):
        return False
    health.steps = _first_int(payload, "totalSteps")
    health.distance_m = _first_number(payload, "totalDistanceMeters")
    health.total_calories = _first_int(payload, "totalKilocalories")
    health.active_calories = _first_int(payload, "activeKilocalories")
    health.resting_hr = _first_int(payload, "restingHeartRate")
    health.min_hr = _first_int(payload, "minHeartRate")
    health.max_hr = _first_int(payload, "maxHeartRate")
    health.stress_average = _first_int(payload, "averageStressLevel")
    health.stress_max = _first_int(payload, "maxStressLevel")
    health.body_battery_high = _first_int(payload, "bodyBatteryHighestValue")
    health.body_battery_low = _first_int(payload, "bodyBatteryLowestValue")
    health.waking_respiration_average = _first_number(payload, "avgWakingRespirationValue")
    health.moderate_intensity_minutes = _first_int(payload, "moderateIntensityMinutes")
    health.vigorous_intensity_minutes = _first_int(payload, "vigorousIntensityMinutes")
    return True


def _score_value(scores: Any, key: str) -> int | None:
    if not isinstance(scores, dict):
        return None
    value = scores.get(key)
    if isinstance(value, dict):
        value = value.get("value") or value.get("score")
    number = _number(value)
    return round(number) if number is not None else None


def _sleep_has_data(payload: Any) -> bool:
    return _positive(payload, "sleepTimeSeconds", "deepSleepSeconds", "lightSleepSeconds")


def _sleep_stages(payload: dict[str, Any]) -> list[SleepStage]:
    levels = payload.get("sleepLevels")
    if not isinstance(levels, list):
        return []
    stage_names = {0: "deep", 1: "light", 2: "rem", 3: "awake"}
    stages: list[SleepStage] = []
    for item in levels:
        if not isinstance(item, dict):
            continue
        raw_stage = item.get("sleepLevel", item.get("activityLevel"))
        number = _number(raw_stage)
        stage = stage_names.get(round(number)) if number is not None else None
        if isinstance(raw_stage, str):
            normalized = raw_stage.casefold().replace("_sleep", "")
            stage = normalized if normalized in {"awake", "rem", "light", "deep"} else None
        started_at = _parse_datetime(item.get("startGMT") or item.get("startTimeGMT"))
        ended_at = _parse_datetime(item.get("endGMT") or item.get("endTimeGMT"))
        if stage is None or started_at is None or ended_at is None or ended_at <= started_at:
            continue
        stages.append(
            SleepStage(
                position=len(stages),
                stage=stage,
                started_at=started_at,
                ended_at=ended_at,
            )
        )
    return stages


def _store_sleep(session: Session, user_id: int, day: date, payload: Any) -> bool:
    health = get_or_create_health_day(session, user_id, day)
    _clear_fields(health, SLEEP_FIELDS)
    replace_sleep_stages(session, health, [])
    if not isinstance(payload, dict) or not _sleep_has_data(payload):
        return False
    dto = payload.get("dailySleepDTO")
    if not isinstance(dto, dict):
        return False
    scores = dto.get("sleepScores")
    raw_sleep_need = dto.get("sleepNeed")
    sleep_need: dict[str, Any] = raw_sleep_need if isinstance(raw_sleep_need, dict) else {}
    raw_next_need = dto.get("nextSleepNeed")
    next_need: dict[str, Any] = raw_next_need if isinstance(raw_next_need, dict) else {}
    health.sleep_seconds = _duration_seconds(dto.get("sleepTimeSeconds"))
    health.sleep_score = _score_value(scores, "overall")
    health.sleep_score_duration = _score_value(scores, "totalDuration")
    health.sleep_score_stress = _score_value(scores, "stress")
    health.sleep_score_awake_count = _score_value(scores, "awakeCount")
    health.sleep_score_rem_percentage = _score_value(scores, "remPercentage")
    health.sleep_score_restlessness = _score_value(scores, "restlessness")
    health.sleep_score_light_percentage = _score_value(scores, "lightPercentage")
    health.sleep_score_deep_percentage = _score_value(scores, "deepPercentage")
    health.sleep_start_at = _parse_datetime(
        dto.get("sleepStartTimestampGMT") or dto.get("sleepStartTimestampLocal")
    )
    health.sleep_end_at = _parse_datetime(
        dto.get("sleepEndTimestampGMT") or dto.get("sleepEndTimestampLocal")
    )
    health.deep_sleep_seconds = _duration_seconds(dto.get("deepSleepSeconds"))
    health.light_sleep_seconds = _duration_seconds(dto.get("lightSleepSeconds"))
    health.rem_sleep_seconds = _duration_seconds(dto.get("remSleepSeconds"))
    health.awake_sleep_seconds = _duration_seconds(dto.get("awakeSleepSeconds"))
    health.nap_seconds = _duration_seconds(dto.get("napTimeSeconds"))
    health.sleep_need_seconds = _sleep_need_seconds(sleep_need.get("actual"))
    health.sleep_need_baseline_seconds = _sleep_need_seconds(sleep_need.get("baseline"))
    health.next_sleep_need_seconds = _sleep_need_seconds(next_need.get("actual"))
    health.sleep_average_hr = _first_number(dto, "avgHeartRate")
    health.sleep_average_stress = _first_number(dto, "avgSleepStress")
    health.sleep_body_battery_change = _first_int(payload, "bodyBatteryChange")
    health.sleep_respiration_average = _first_number(
        dto, "averageRespirationValue", "avgRespirationValue"
    )
    replace_sleep_stages(session, health, _sleep_stages(payload))
    return True


def _hrv_has_data(payload: Any) -> bool:
    return _positive(payload, "lastNightAvg", "weeklyAvg", "lastNight5MinHigh")


def _store_hrv(session: Session, user_id: int, day: date, payload: Any) -> bool:
    health = get_or_create_health_day(session, user_id, day)
    _clear_fields(health, HRV_FIELDS)
    if not isinstance(payload, dict) or not _hrv_has_data(payload):
        return False
    summary = payload.get("hrvSummary")
    if not isinstance(summary, dict):
        return False
    baseline = summary.get("baseline") if isinstance(summary.get("baseline"), dict) else {}
    health.hrv_average = _first_number(summary, "lastNightAvg")
    health.hrv_weekly_average = _first_number(summary, "weeklyAvg")
    health.hrv_five_min_high = _first_number(summary, "lastNight5MinHigh")
    health.hrv_status = _first_text(summary, "status")
    health.hrv_baseline_low = _first_number(baseline, "lowUpper")
    health.hrv_baseline_balanced_low = _first_number(baseline, "balancedLow")
    health.hrv_baseline_balanced_high = _first_number(baseline, "balancedUpper")
    return True


def _spo2_has_data(payload: Any) -> bool:
    return _positive(payload, "averageSpO2", "avgSleepSpO2", "latestSpO2", "lowestSpO2")


def _store_spo2(session: Session, user_id: int, day: date, payload: Any) -> bool:
    health = get_or_create_health_day(session, user_id, day)
    _clear_fields(health, SPO2_FIELDS)
    if not _spo2_has_data(payload):
        return False
    health.spo2_average = _first_number(payload, "averageSpO2")
    health.sleep_spo2_average = _first_number(payload, "avgSleepSpO2")
    health.spo2_lowest = _first_number(payload, "lowestSpO2")
    return True


def _vo2max_has_data(payload: Any) -> bool:
    return _positive(payload, "vo2MaxPreciseValue", "vo2MaxValue", "vO2MaxValue", "vo2Max")


def _store_vo2max(session: Session, user_id: int, day: date, payload: Any) -> bool:
    fitness = get_or_create_daily_fitness(session, user_id, day)
    fitness.vo2max = None
    if not _vo2max_has_data(payload):
        return False
    fitness.vo2max = _first_number(
        payload, "vo2MaxPreciseValue", "vo2MaxValue", "vO2MaxValue", "vo2Max"
    )
    return True


def _readiness_has_data(payload: Any) -> bool:
    return _positive(payload, "score", "recoveryTime")


def _store_readiness(session: Session, user_id: int, day: date, payload: Any) -> bool:
    fitness = get_or_create_daily_fitness(session, user_id, day)
    fitness.garmin_training_readiness_score = None
    fitness.garmin_training_readiness_level = None
    fitness.recovery_time_minutes = None
    if not _readiness_has_data(payload):
        return False
    entries = payload if isinstance(payload, list) else [payload]
    entry = next(
        (
            item
            for item in entries
            if isinstance(item, dict) and item.get("inputContext") == "AFTER_WAKEUP_RESET"
        ),
        next((item for item in entries if isinstance(item, dict)), {}),
    )
    fitness.garmin_training_readiness_score = _first_int(entry, "score")
    fitness.garmin_training_readiness_level = _first_text(entry, "level")
    fitness.recovery_time_minutes = _first_int(entry, "recoveryTime")
    return True


def _training_status_has_data(payload: Any) -> bool:
    return _first_text(payload, "trainingStatus", "trainingStatusKey", "status") is not None or (
        _positive(payload, "acuteTrainingLoad", "trainingLoad", "loadRatio", "acwr")
    )


def _store_training_status(session: Session, user_id: int, day: date, payload: Any) -> bool:
    fitness = get_or_create_daily_fitness(session, user_id, day)
    fields = ("training_status", "training_load", "acute_load", "chronic_load", "load_ratio")
    _clear_fields(fitness, fields)
    if not _training_status_has_data(payload):
        return False
    fitness.training_status = _first_text(payload, "trainingStatus", "trainingStatusKey", "status")
    fitness.training_load = _first_number(payload, "trainingLoad")
    fitness.acute_load = _first_number(payload, "acuteTrainingLoad", "acuteLoad")
    fitness.chronic_load = _first_number(payload, "chronicTrainingLoad", "chronicLoad")
    fitness.load_ratio = _first_number(payload, "loadRatio", "acwr")
    return True


def _body_battery_has_data(payload: Any) -> bool:
    if not isinstance(payload, list):
        return False
    for item in payload:
        if not isinstance(item, dict):
            continue
        if _first_number(item, "charged", "drained") is not None:
            return True
        samples = item.get("bodyBatteryValuesArray")
        if isinstance(samples, list) and any(
            isinstance(sample, list) and len(sample) > 1 and _number(sample[1]) is not None
            for sample in samples
        ):
            return True
    return False


def _store_body_battery(session: Session, user_id: int, day: date, payload: Any) -> bool:
    health = get_or_create_health_day(session, user_id, day)
    fields = (
        "body_battery_high",
        "body_battery_low",
        "body_battery_charged",
        "body_battery_drained",
    )
    _clear_fields(health, fields)
    entries = payload if isinstance(payload, list) else []
    entry = next(
        (
            item
            for item in entries
            if isinstance(item, dict) and str(item.get("date") or "") == day.isoformat()
        ),
        entries[0] if len(entries) == 1 and isinstance(entries[0], dict) else {},
    )
    if not isinstance(entry, dict) or not _body_battery_has_data([entry]):
        return False
    values = [
        number
        for sample in entry.get("bodyBatteryValuesArray") or []
        if isinstance(sample, list)
        and len(sample) > 1
        and (number := _number(sample[1])) is not None
    ]
    health.body_battery_high = round(max(values)) if values else None
    health.body_battery_low = round(min(values)) if values else None
    health.body_battery_charged = _first_int(entry, "charged")
    health.body_battery_drained = _first_int(entry, "drained")
    return True


def _status_code(exc: Exception) -> int | None:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    match = re.search(r"(?:API Error|Error|HTTP|client error)\D*(\d{3})", str(exc))
    return int(match.group(1)) if match else None


def _unsupported(exc: Exception) -> bool:
    return isinstance(exc, GarminConnectNotFoundError) or _status_code(exc) == 404


def _failure_status(exc: Exception) -> str:
    status = _status_code(exc)
    if isinstance(exc, GarminConnectAuthenticationError) or status in {401, 403}:
        return "authentication_failure"
    if isinstance(exc, GarminConnectTooManyRequestsError) or status == 429:
        return "rate_limited"
    return "error"


def _discover_earliest(
    resource: DailyResource,
    pacer: GarminPacer,
    stats: ResourceSyncStats,
    end: date,
    minimum: date,
) -> date | None:
    cache: dict[date, bool] = {}

    def populated(day: date) -> bool:
        if day not in cache:
            value = pacer.call(
                f"discover {resource.name} {day.isoformat()}",
                lambda: resource.fetch(day.isoformat()),
            )
            stats.api_calls += 1
            cache[day] = resource.has_data(value)
        return cache[day]

    recent = next(
        (
            candidate
            for offset in (0, 1, 2, 3, 7, 14, 30, 60, 90)
            if (candidate := end - timedelta(days=offset)) >= minimum and populated(candidate)
        ),
        None,
    )
    if recent is None:
        return None

    oldest = recent
    older_empty: date | None = None
    step = 30
    while True:
        candidate = max(recent - timedelta(days=step), minimum)
        if populated(candidate):
            oldest = min(oldest, candidate)
            if candidate == minimum:
                return oldest
            step *= 2
            continue
        nearby = [
            day
            for offset in (1, -1, 3, -3, 7, -7)
            if minimum <= (day := candidate + timedelta(days=offset)) < oldest and populated(day)
        ]
        if nearby:
            oldest = min(oldest, *nearby)
            step *= 2
            continue
        older_empty = candidate
        break

    while (oldest - older_empty).days > 7:
        middle = older_empty + (oldest - older_empty) // 2
        if populated(middle):
            oldest = middle
        else:
            older_empty = middle
    return next(
        (
            day
            for offset in range(1, (oldest - older_empty).days + 1)
            if populated(day := older_empty + timedelta(days=offset))
        ),
        oldest,
    )


def _should_skip_empty_state(state: GarminSyncState, today: date) -> bool:
    if not state.backfill_complete or state.newest_synced_date is not None:
        return False
    if state.last_attempt_at is None:
        return False
    return state.last_attempt_at.date() > today - timedelta(days=EMPTY_RESOURCE_REPROBE_DAYS)


def _sync_failure(session: Session, user_id: int, resource: str, exc: Exception) -> None:
    session.rollback()
    state = get_or_create_sync_state(session, user_id, resource)
    if _unsupported(exc):
        state.status = "unsupported"
        state.backfill_complete = True
        state.last_attempt_at = utcnow()
        state.error = message_from_exception(exc)
    else:
        mark_sync_error(state, message_from_exception(exc))
        state.status = _failure_status(exc)
    session.commit()


def _sync_daily_resource(
    session: Session,
    user_id: int,
    resource: DailyResource,
    pacer: GarminPacer,
    *,
    today: date,
    minimum: date,
    overlap_days: int,
    progress: ProgressCallback | None,
) -> ResourceSyncStats:
    stats = ResourceSyncStats()
    state = get_or_create_sync_state(session, user_id, resource.name)
    if _should_skip_empty_state(state, today):
        stats.complete = True
        return stats
    initial = not state.backfill_complete
    mark_sync_attempt(state)
    session.commit()
    try:
        if initial and state.backfill_cursor_date is not None:
            start = state.backfill_cursor_date
        elif initial or state.newest_synced_date is None:
            discovered = _discover_earliest(resource, pacer, stats, today, minimum)
            if discovered is None:
                state.status = "empty"
                state.backfill_complete = True
                state.last_success_at = utcnow()
                session.commit()
                stats.complete = True
                return stats
            start = discovered
            state.backfill_cursor_date = start
            session.commit()
        else:
            overlap_start = today - timedelta(days=overlap_days - 1)
            start = min(state.newest_synced_date + timedelta(days=1), overlap_start)

        day = max(start, minimum)
        while day <= today:
            if progress is not None:
                progress(resource.name, day)
            payload = pacer.call(
                f"{resource.name} {day.isoformat()}",
                partial(resource.fetch, day.isoformat()),
            )
            stats.api_calls += 1
            populated = resource.store(session, user_id, day, payload)
            set_daily_data_status(
                session, user_id, day, resource.name, "complete" if populated else "empty"
            )
            next_day = day + timedelta(days=1)
            mark_sync_success(
                state,
                oldest_date=day,
                newest_date=day,
                backfill_cursor_date=next_day if initial and next_day <= today else None,
                backfill_complete=True if initial and next_day > today else None,
            )
            session.commit()
            stats.days_processed += 1
            stats.populated_days += int(populated)
            stats.empty_days += int(not populated)
            stats.earliest = day if stats.earliest is None else min(stats.earliest, day)
            stats.latest = day if stats.latest is None else max(stats.latest, day)
            day = next_day
        stats.complete = state.backfill_complete
        return stats
    except Exception as exc:
        _sync_failure(session, user_id, resource.name, exc)
        if _unsupported(exc):
            stats.complete = True
            return stats
        raise


def _sync_body_battery(
    session: Session,
    client: Any,
    user_id: int,
    pacer: GarminPacer,
    *,
    today: date,
    minimum: date,
    overlap_days: int,
    progress: ProgressCallback | None,
) -> ResourceSyncStats:
    resource = DailyResource(
        "body_battery",
        lambda day: client.get_body_battery(day),
        _body_battery_has_data,
        _store_body_battery,
    )
    stats = ResourceSyncStats()
    state = get_or_create_sync_state(session, user_id, resource.name)
    if _should_skip_empty_state(state, today):
        stats.complete = True
        return stats
    initial = not state.backfill_complete
    mark_sync_attempt(state)
    session.commit()
    try:
        if initial and state.backfill_cursor_date is not None:
            start = state.backfill_cursor_date
        elif initial or state.newest_synced_date is None:
            discovered = _discover_earliest(resource, pacer, stats, today, minimum)
            if discovered is None:
                state.status = "empty"
                state.backfill_complete = True
                state.last_success_at = utcnow()
                session.commit()
                stats.complete = True
                return stats
            start = discovered
            state.backfill_cursor_date = start
            session.commit()
        else:
            start = min(
                state.newest_synced_date + timedelta(days=1),
                today - timedelta(days=overlap_days - 1),
            )

        chunk_start = max(start, minimum)
        while chunk_start <= today:
            chunk_end = min(chunk_start + timedelta(days=BODY_BATTERY_RANGE_DAYS - 1), today)
            if progress is not None:
                progress(resource.name, chunk_start)
            payload = pacer.call(
                f"body_battery {chunk_start.isoformat()} to {chunk_end.isoformat()}",
                partial(
                    client.get_body_battery,
                    chunk_start.isoformat(),
                    chunk_end.isoformat(),
                ),
            )
            stats.api_calls += 1
            entries = {
                str(item.get("date")): item
                for item in payload or []
                if isinstance(item, dict) and item.get("date")
            }
            day = chunk_start
            while day <= chunk_end:
                day_payload = [entries[day.isoformat()]] if day.isoformat() in entries else []
                populated = _store_body_battery(session, user_id, day, day_payload)
                set_daily_data_status(
                    session,
                    user_id,
                    day,
                    resource.name,
                    "complete" if populated else "empty",
                )
                stats.days_processed += 1
                stats.populated_days += int(populated)
                stats.empty_days += int(not populated)
                day += timedelta(days=1)
            next_day = chunk_end + timedelta(days=1)
            mark_sync_success(
                state,
                oldest_date=chunk_start,
                newest_date=chunk_end,
                backfill_cursor_date=next_day if initial and next_day <= today else None,
                backfill_complete=True if initial and next_day > today else None,
            )
            session.commit()
            stats.earliest = (
                chunk_start if stats.earliest is None else min(stats.earliest, chunk_start)
            )
            stats.latest = chunk_end if stats.latest is None else max(stats.latest, chunk_end)
            chunk_start = next_day
        stats.complete = state.backfill_complete
        return stats
    except Exception as exc:
        _sync_failure(session, user_id, resource.name, exc)
        if _unsupported(exc):
            stats.complete = True
            return stats
        raise


def sync_health_history(
    session: Session,
    client: Any,
    user_id: int,
    *,
    today: date | None = None,
    minimum: date = MIN_HISTORY_DATE,
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
    delay: float = 0.75,
    progress: ProgressCallback | None = None,
) -> HealthSyncResult:
    target_day = today or date.today()
    if minimum > target_day:
        raise ValueError("minimum history date cannot be after today")
    if overlap_days < 1:
        raise ValueError("overlap_days must be positive")
    pacer = GarminPacer(delay)
    resources = [
        DailyResource("daily_summary", client.get_user_summary, _summary_has_data, _store_summary),
        DailyResource("sleep", client.get_sleep_data, _sleep_has_data, _store_sleep),
        DailyResource("hrv", client.get_hrv_data, _hrv_has_data, _store_hrv),
        DailyResource("spo2", client.get_spo2_data, _spo2_has_data, _store_spo2),
        DailyResource("vo2max", client.get_max_metrics, _vo2max_has_data, _store_vo2max),
        DailyResource(
            "training_readiness",
            client.get_training_readiness,
            _readiness_has_data,
            _store_readiness,
        ),
        DailyResource(
            "training_status",
            client.get_training_status,
            _training_status_has_data,
            _store_training_status,
        ),
    ]
    result = HealthSyncResult()
    for resource in resources[:1]:
        result.resources[resource.name] = _sync_daily_resource(
            session,
            user_id,
            resource,
            pacer,
            today=target_day,
            minimum=minimum,
            overlap_days=overlap_days,
            progress=progress,
        )
    result.resources["body_battery"] = _sync_body_battery(
        session,
        client,
        user_id,
        pacer,
        today=target_day,
        minimum=minimum,
        overlap_days=overlap_days,
        progress=progress,
    )
    for resource in resources[1:]:
        result.resources[resource.name] = _sync_daily_resource(
            session,
            user_id,
            resource,
            pacer,
            today=target_day,
            minimum=minimum,
            overlap_days=overlap_days,
            progress=progress,
        )
    return result
