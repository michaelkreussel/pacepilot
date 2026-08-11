import logging
import random
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import partial
from typing import Any, Literal

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
    empty_data_days,
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

logger = logging.getLogger(__name__)

MIN_HISTORY_DATE = date(2005, 1, 1)
DEFAULT_OVERLAP_DAYS = 7
EMPTY_RESOURCE_REPROBE_DAYS = 28
BODY_BATTERY_RANGE_DAYS = 31

StorePayload = Callable[[Session, int, date, Any], bool]
HasData = Callable[[Any], bool]


def retry_after_seconds(value: object, default: float) -> float:
    if isinstance(value, (int, float, str)):
        try:
            return max(float(value), default)
        except ValueError:
            if isinstance(value, str):
                try:
                    retry_at = parsedate_to_datetime(value)
                except (TypeError, ValueError):
                    pass
                else:
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    remaining = (retry_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()
                    return max(remaining, default)
    return default


HealthProgressPhase = Literal[
    "resource_planning_start",
    "resource_planning_complete",
    "resource_planning_error",
    "plan",
    "day_start",
    "operation_start",
    "operation_complete",
    "operation_error",
    "operation_skipped",
    "day_complete",
    "resource_skipped",
]


@dataclass(frozen=True)
class HealthProgressEvent:
    phase: HealthProgressPhase
    resource: str | None = None
    day: date | None = None
    planned: Mapping[str, tuple[date, ...]] | None = None
    populated: bool | None = None
    record_count: int | None = None
    duration_ms: float | None = None
    reason: str | None = None


HealthProgressCallback = Callable[[HealthProgressEvent], None]


@dataclass(frozen=True)
class DailyResource:
    name: str
    fetch: Callable[[str], Any]
    has_data: HasData
    store: StorePayload
    fetch_range: Callable[[str, str], Any] | None = None


@dataclass
class ResourceSyncStats:
    api_calls: int = 0
    days_processed: int = 0
    populated_days: int = 0
    empty_days: int = 0
    earliest: date | None = None
    latest: date | None = None
    complete: bool = False
    processed_days: set[date] = field(default_factory=set, repr=False)


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
        return len(set().union(*(stats.processed_days for stats in self.resources.values())))


class GarminPacer:
    _global_lock = threading.Lock()
    _global_next_call_at = 0.0
    _global_cooldown_until = 0.0

    def __init__(
        self,
        delay: float,
        log_context: Mapping[str, Any] | None = None,
        *,
        rate_limit_cooldown: float = 300,
    ) -> None:
        self.delay = max(delay, 0)
        self.next_call_at = 0.0
        self.log_context = dict(log_context or {})
        self.rate_limit_cooldown = max(rate_limit_cooldown, 0)

    def call(self, operation: str, call: Callable[[], Any]) -> Any:
        interval = self.delay * random.uniform(1.0, 1.1) if self.delay else 0  # noqa: S311
        while True:
            with self._global_lock:
                now = time.monotonic()
                call_at = max(
                    now,
                    self.next_call_at,
                    self._global_next_call_at,
                    self._global_cooldown_until,
                )
                self.next_call_at = call_at + interval
                type(self)._global_next_call_at = call_at + interval
            wait = call_at - now
            if wait > 0:
                time.sleep(wait)
            with self._global_lock:
                if self._global_cooldown_until <= call_at:
                    break
        logger.info(
            "Garmin request start",
            extra={"garmin_operation": operation, **self.log_context},
        )
        started = time.perf_counter()
        try:
            result = call()
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            status = _status_code(exc)
            rate_limited = isinstance(exc, GarminConnectTooManyRequestsError) or status == 429
            if rate_limited:
                retry_after = getattr(getattr(exc, "response", None), "headers", {}).get(
                    "Retry-After"
                )
                cooldown = retry_after_seconds(retry_after, self.rate_limit_cooldown)
                self.defer_all(max(cooldown, self.rate_limit_cooldown))
            log = logger.warning if rate_limited else logger.error
            log(
                "Garmin request end",
                extra={
                    "garmin_operation": operation,
                    "duration_ms": duration_ms,
                    "data_returned": False,
                    "record_count": 0,
                    "http_status": status or (429 if rate_limited else None),
                    "outcome": "rate_limited" if rate_limited else "error",
                    **self.log_context,
                },
            )
            raise
        else:
            logger.info(
                "Garmin request end",
                extra={
                    "garmin_operation": operation,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "data_returned": _record_count(result) > 0,
                    "record_count": _record_count(result),
                    "outcome": "success",
                    **self.log_context,
                },
            )
            return result

    @classmethod
    def defer_all(cls, seconds: float) -> None:
        with cls._global_lock:
            cls._global_cooldown_until = max(
                cls._global_cooldown_until, time.monotonic() + max(seconds, 0)
            )


def _record_count(payload: Any) -> int:
    if payload is None:
        return 0
    if isinstance(payload, (list, tuple, set, frozenset)):
        return len(payload)
    if isinstance(payload, dict):
        return int(bool(payload))
    return 1


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
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC).replace(tzinfo=None)
            return parsed
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
    payloads: dict[date, Any],
    anchor: date | None = None,
) -> date | None:
    def populated(day: date) -> bool:
        if day not in payloads:
            payloads[day] = pacer.call(
                f"discover {resource.name} {day.isoformat()}",
                lambda: resource.fetch(day.isoformat()),
            )
            stats.api_calls += 1
        return resource.has_data(payloads[day])

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

    if anchor is not None and anchor < recent:
        scan_end = min(anchor + timedelta(days=BODY_BATTERY_RANGE_DAYS - 1), recent)
        fetch_range = resource.fetch_range
        if fetch_range is not None:
            payload = pacer.call(
                f"discover {resource.name} {anchor.isoformat()} to {scan_end.isoformat()}",
                lambda: fetch_range(anchor.isoformat(), scan_end.isoformat()),
            )
            stats.api_calls += 1
            entries = {
                str(item.get("date")): item
                for item in payload or []
                if isinstance(item, dict) and item.get("date")
            }
            for day in _dates_between(anchor, scan_end):
                entry = entries.get(day.isoformat())
                payloads[day] = [entry] if entry is not None else []
        else:
            scan_end = min(anchor + timedelta(days=13), recent)

        anchored = next(
            (day for day in _dates_between(anchor, scan_end) if populated(day)),
            None,
        )
        if anchored is not None:
            return anchored
        minimum = scan_end + timedelta(days=1)

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


@dataclass
class _ResourcePlan:
    resource: DailyResource
    state: GarminSyncState
    stats: ResourceSyncStats
    initial: bool
    days: tuple[date, ...] = ()
    skipped_days: set[date] = field(default_factory=set)
    payloads: dict[date, Any] = field(default_factory=dict)
    body_battery: bool = False
    skip_reason: str | None = None
    disabled_reason: str | None = None


def _dates_between(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _emit(
    progress: HealthProgressCallback | None,
    event: HealthProgressEvent,
) -> None:
    if progress is not None:
        progress(event)


def _unsupported_reason(exc: Exception) -> str:
    status = _status_code(exc)
    return f"resource unsupported (HTTP {status})" if status else "resource unsupported"


def _plan_resource(
    session: Session,
    user_id: int,
    resource: DailyResource,
    pacer: GarminPacer,
    *,
    today: date,
    minimum: date,
    overlap_days: int,
    body_battery: bool = False,
    discovery_anchor: date | None = None,
) -> _ResourcePlan:
    stats = ResourceSyncStats()
    state = get_or_create_sync_state(session, user_id, resource.name)
    plan = _ResourcePlan(
        resource=resource,
        state=state,
        stats=stats,
        initial=not state.backfill_complete or state.newest_synced_date is None,
        body_battery=body_battery,
    )
    if _should_skip_empty_state(state, today):
        stats.complete = True
        plan.skip_reason = (
            "resource previously marked unsupported"
            if state.status == "unsupported"
            else "empty resource is not due for reprobe"
        )
        return plan

    mark_sync_attempt(state)
    session.commit()
    try:
        if plan.initial and state.backfill_cursor_date is not None:
            start = state.backfill_cursor_date
        elif plan.initial or state.newest_synced_date is None:
            discovered = _discover_earliest(
                resource,
                pacer,
                stats,
                today,
                minimum,
                plan.payloads,
                discovery_anchor,
            )
            if discovered is None:
                state.status = "empty"
                state.backfill_complete = True
                state.last_success_at = utcnow()
                session.commit()
                stats.complete = True
                plan.skip_reason = "no populated days found"
                return plan
            start = discovered
            state.backfill_cursor_date = start
            session.commit()
        else:
            overlap_start = today - timedelta(days=overlap_days - 1)
            start = min(state.newest_synced_date + timedelta(days=1), overlap_start)

        first_day = max(start, minimum)
        plan.days = _dates_between(first_day, today)
        plan.skipped_days = empty_data_days(
            session, user_id, resource.name, first_day, today - timedelta(days=1)
        )
        return plan
    except Exception as exc:
        _sync_failure(session, user_id, resource.name, exc)
        if _unsupported(exc):
            stats.complete = True
            plan.skip_reason = _unsupported_reason(exc)
            return plan
        raise


def _body_battery_payload(
    plan: _ResourcePlan,
    client: Any,
    pacer: GarminPacer,
    day: date,
) -> Any:
    if day in plan.payloads:
        return plan.payloads[day]

    planned_days = set(plan.days)
    chunk_end = day
    while (chunk_end - day).days < BODY_BATTERY_RANGE_DAYS - 1:
        candidate = chunk_end + timedelta(days=1)
        if (
            candidate not in planned_days
            or candidate in plan.skipped_days
            or candidate in plan.payloads
        ):
            break
        chunk_end = candidate

    payload = pacer.call(
        f"body_battery {day.isoformat()} to {chunk_end.isoformat()}",
        partial(client.get_body_battery, day.isoformat(), chunk_end.isoformat()),
    )
    plan.stats.api_calls += 1
    entries = {
        str(item.get("date")): item
        for item in payload or []
        if isinstance(item, dict) and item.get("date")
    }
    for chunk_day in _dates_between(day, chunk_end):
        entry = entries.get(chunk_day.isoformat())
        plan.payloads[chunk_day] = [entry] if entry is not None else []
    return plan.payloads[day]


def _execute_operation(
    session: Session,
    client: Any,
    user_id: int,
    plan: _ResourcePlan,
    pacer: GarminPacer,
    day: date,
    today: date,
    progress: HealthProgressCallback | None,
) -> None:
    resource = plan.resource
    _emit(
        progress,
        HealthProgressEvent(phase="operation_start", resource=resource.name, day=day),
    )
    started = time.perf_counter()
    try:
        if plan.body_battery:
            payload = _body_battery_payload(plan, client, pacer, day)
        elif day in plan.payloads:
            payload = plan.payloads[day]
        else:
            payload = pacer.call(
                f"{resource.name} {day.isoformat()}",
                partial(resource.fetch, day.isoformat()),
            )
            plan.stats.api_calls += 1

        populated = resource.store(session, user_id, day, payload)
        set_daily_data_status(
            session,
            user_id,
            day,
            resource.name,
            "complete" if populated else "empty",
        )
        next_day = day + timedelta(days=1)
        cursor = plan.state.backfill_cursor_date
        advances_cursor = plan.initial and cursor == day
        mark_sync_success(
            plan.state,
            oldest_date=day,
            newest_date=day,
            backfill_cursor_date=(
                next_day
                if advances_cursor and next_day <= today
                else cursor
                if plan.initial
                else None
            ),
            backfill_complete=False if plan.initial else None,
        )
        session.commit()
    except Exception as exc:
        _sync_failure(session, user_id, resource.name, exc)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        reason = _unsupported_reason(exc) if _unsupported(exc) else message_from_exception(exc)
        _emit(
            progress,
            HealthProgressEvent(
                phase="operation_error",
                resource=resource.name,
                day=day,
                duration_ms=duration_ms,
                reason=reason,
            ),
        )
        if _unsupported(exc):
            plan.disabled_reason = reason
            plan.stats.complete = True
            _emit(
                progress,
                HealthProgressEvent(
                    phase="resource_skipped",
                    resource=resource.name,
                    day=day,
                    reason=reason,
                ),
            )
            return
        raise

    stats = plan.stats
    stats.days_processed += 1
    stats.populated_days += int(populated)
    stats.empty_days += int(not populated)
    stats.earliest = day if stats.earliest is None else min(stats.earliest, day)
    stats.latest = day if stats.latest is None else max(stats.latest, day)
    stats.processed_days.add(day)
    _emit(
        progress,
        HealthProgressEvent(
            phase="operation_complete",
            resource=resource.name,
            day=day,
            populated=populated,
            record_count=_record_count(payload),
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        ),
    )


def sync_health_history(
    session: Session,
    client: Any,
    user_id: int,
    *,
    today: date | None = None,
    minimum: date = MIN_HISTORY_DATE,
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
    delay: float = 0.75,
    pacer: GarminPacer | None = None,
    progress: HealthProgressCallback | None = None,
    log_context: Mapping[str, Any] | None = None,
) -> HealthSyncResult:
    target_day = today or date.today()
    if minimum > target_day:
        raise ValueError("minimum history date cannot be after today")
    if overlap_days < 1:
        raise ValueError("overlap_days must be positive")
    request_pacer = pacer or GarminPacer(delay, log_context)
    resources = [
        DailyResource("daily_summary", client.get_user_summary, _summary_has_data, _store_summary),
        DailyResource(
            "body_battery",
            lambda day: client.get_body_battery(day),
            _body_battery_has_data,
            _store_body_battery,
            client.get_body_battery,
        ),
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
    plans: list[_ResourcePlan] = []
    discovery_anchor: date | None = None
    for resource in resources:
        _emit(
            progress,
            HealthProgressEvent(phase="resource_planning_start", resource=resource.name),
        )
        try:
            plan = _plan_resource(
                session,
                user_id,
                resource,
                request_pacer,
                today=target_day,
                minimum=minimum,
                overlap_days=overlap_days,
                body_battery=resource.name == "body_battery",
                discovery_anchor=(
                    discovery_anchor if resource.name in {"body_battery", "sleep", "hrv"} else None
                ),
            )
        except Exception as exc:
            _emit(
                progress,
                HealthProgressEvent(
                    phase="resource_planning_error",
                    resource=resource.name,
                    reason=message_from_exception(exc),
                ),
            )
            raise
        plans.append(plan)
        result.resources[resource.name] = plan.stats
        if resource.name == "daily_summary" and plan.days:
            discovery_anchor = plan.state.oldest_synced_date or plan.days[0]
        _emit(
            progress,
            HealthProgressEvent(
                phase="resource_planning_complete",
                resource=resource.name,
                record_count=plan.stats.api_calls,
                reason=plan.skip_reason,
            ),
        )

    _emit(
        progress,
        HealthProgressEvent(
            phase="plan",
            planned={plan.resource.name: plan.days for plan in plans},
        ),
    )
    for plan in plans:
        if plan.skip_reason is not None:
            _emit(
                progress,
                HealthProgressEvent(
                    phase="resource_skipped",
                    resource=plan.resource.name,
                    reason=plan.skip_reason,
                ),
            )

    all_days = sorted({day for plan in plans for day in plan.days})
    recent_start = target_day - timedelta(days=overlap_days - 1)
    recent_days = sorted((day for day in all_days if day >= recent_start), reverse=True)
    history_days = [day for day in all_days if day < recent_start]
    all_days = recent_days + history_days
    for day in all_days:
        _emit(progress, HealthProgressEvent(phase="day_start", day=day))
        for plan in plans:
            if day not in plan.days:
                continue
            if day in plan.skipped_days:
                _emit(
                    progress,
                    HealthProgressEvent(
                        phase="operation_skipped",
                        resource=plan.resource.name,
                        day=day,
                        reason="previously confirmed empty",
                    ),
                )
                continue
            if plan.disabled_reason is not None:
                _emit(
                    progress,
                    HealthProgressEvent(
                        phase="operation_skipped",
                        resource=plan.resource.name,
                        day=day,
                        reason=plan.disabled_reason,
                    ),
                )
                continue
            _execute_operation(
                session,
                client,
                user_id,
                plan,
                request_pacer,
                day,
                target_day,
                progress,
            )
        _emit(progress, HealthProgressEvent(phase="day_complete", day=day))

    for plan in plans:
        if plan.skip_reason is None and plan.disabled_reason is None:
            if plan.initial and plan.days:
                mark_sync_success(
                    plan.state,
                    oldest_date=plan.days[0],
                    newest_date=plan.days[-1],
                    backfill_cursor_date=None,
                    backfill_complete=True,
                )
                session.commit()
            plan.stats.complete = plan.state.backfill_complete
    return result
