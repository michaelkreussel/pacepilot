import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GarminSyncState
from app.models.user import utcnow
from app.repositories.fitness import get_or_create_daily_fitness
from app.services.garmin.health_backfill import GarminPacer

logger = logging.getLogger(__name__)
PROFILE_REFRESH_INTERVAL = timedelta(hours=24)


@dataclass(frozen=True)
class ImportedMetricValue:
    sport: str
    metric: str
    value: float
    source_day: date | None = None


@dataclass(frozen=True)
class ImportedZoneValue:
    sport: str
    zone_type: str
    zone_number: int
    lower_boundary: float
    upper_boundary: float | None


@dataclass(frozen=True)
class ParsedPerformanceResource:
    metrics: tuple[ImportedMetricValue, ...] = ()
    zones: tuple[ImportedZoneValue, ...] = ()


def _dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _dicts(item)


def _number(value: Any, *keys: str) -> float | None:
    wanted = {key.lower() for key in keys}
    for item in _dicts(value):
        for key, candidate in item.items():
            if (
                key.lower() in wanted
                and isinstance(candidate, (int, float))
                and not isinstance(candidate, bool)
                and candidate > 0
            ):
                return float(candidate)
    return None


def _source_day(value: Any) -> date | None:
    wanted = {"calendardate", "date", "recorddate", "activitydate"}
    for item in _dicts(value):
        for key, candidate in item.items():
            if key.lower() not in wanted or not isinstance(candidate, str):
                continue
            try:
                return date.fromisoformat(candidate[:10])
            except ValueError:
                continue
    return None


def _sport(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("typeKey") or value.get("sportKey") or value.get("key")
    normalized = str(value or "general").lower()
    if "run" in normalized:
        return "running"
    if "cycl" in normalized or "bike" in normalized:
        return "cycling"
    if "walk" in normalized:
        return "walking"
    if "hik" in normalized:
        return "hiking"
    return "general"


def _parse_lactate_threshold(payload: Any) -> ParsedPerformanceResource:
    if not isinstance(payload, dict):
        return ParsedPerformanceResource()
    speed_hr = payload.get("speed_and_heart_rate", {})
    power = payload.get("power", {})
    source_day = _source_day(speed_hr) or _source_day(power)
    values = []
    for metric, value in (
        ("threshold_hr", _number(speed_hr, "heartRate", "hearRate")),
        ("threshold_speed_mps", _number(speed_hr, "speed")),
        (
            "running_threshold_power_watts",
            _number(power, "functionalThresholdPower", "thresholdPower", "power", "watts"),
        ),
    ):
        if value is not None:
            values.append(ImportedMetricValue("running", metric, value, source_day))
    return ParsedPerformanceResource(metrics=tuple(values))


def _parse_cycling_ftp(payload: Any) -> ParsedPerformanceResource:
    value = _number(payload, "functionalThresholdPower", "thresholdPower", "ftp", "watts")
    return ParsedPerformanceResource(
        metrics=(ImportedMetricValue("cycling", "cycling_ftp_watts", value, _source_day(payload)),)
        if value is not None
        else ()
    )


def _race_seconds(value: float | None) -> float | None:
    if value is None:
        return None
    return value / 1_000 if value > 604_800 else value


def _parse_race_predictions(payload: Any) -> ParsedPerformanceResource:
    source_day = _source_day(payload)
    values = []
    for metric, keys in (
        ("prediction_5k_seconds", ("raceTime5K", "time5K", "prediction5K")),
        ("prediction_10k_seconds", ("raceTime10K", "time10K", "prediction10K")),
        ("prediction_half_seconds", ("raceTimeHalf", "timeHalfMarathon", "predictionHalf")),
        ("prediction_marathon_seconds", ("raceTimeMarathon", "timeMarathon", "predictionMarathon")),
    ):
        value = _race_seconds(_number(payload, *keys))
        if value is not None:
            values.append(ImportedMetricValue("running", metric, value, source_day))
    return ParsedPerformanceResource(metrics=tuple(values))


def _record_metric(item: dict[str, Any]) -> str | None:
    text = " ".join(str(value) for value in item.values() if isinstance(value, (str, int))).lower()
    if "half" in text or "halb" in text or "21k" in text:
        return "reference_half_seconds"
    if "marathon" in text and "half" not in text:
        return "reference_marathon_seconds"
    if "10k" in text or "10 km" in text:
        return "reference_10k_seconds"
    if "5k" in text or "5 km" in text:
        return "reference_5k_seconds"
    if "1k" in text or "1 km" in text:
        return "reference_1k_seconds"
    return None


def _parse_personal_records(payload: Any) -> ParsedPerformanceResource:
    values: dict[str, ImportedMetricValue] = {}
    for item in _dicts(payload):
        metric = _record_metric(item)
        if metric is None:
            continue
        seconds = _race_seconds(
            _number(
                item,
                "recordValueInSeconds",
                "recordValue",
                "value",
                "time",
                "duration",
            )
        )
        if seconds is not None:
            values[metric] = ImportedMetricValue("running", metric, seconds, _source_day(item))
    return ParsedPerformanceResource(metrics=tuple(values.values()))


def _direct_number(item: dict[str, Any], *keys: str) -> float | None:
    lowered = {str(key).lower(): value for key, value in item.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value)
    return None


def _parse_zones(payload: Any, zone_type: str) -> ParsedPerformanceResource:
    metrics: dict[tuple[str, str], ImportedMetricValue] = {}
    floors: dict[tuple[str, int], float] = {}
    maximums: dict[str, float] = {}
    for item in _dicts(payload):
        sport = _sport(
            item.get("sport")
            or item.get("sportType")
            or item.get("sportKey")
            or item.get("activityType")
        )
        max_hr = _direct_number(item, "maxHeartRate", "maximumHeartRate", "maxHr")
        if zone_type == "heart_rate" and max_hr:
            metrics[(sport, "max_hr")] = ImportedMetricValue(sport, "max_hr", max_hr)
            maximums[sport] = max_hr
        threshold_hr = _direct_number(
            item, "lactateThresholdHeartRate", "thresholdHeartRate", "lthr"
        )
        if zone_type == "heart_rate" and threshold_hr:
            metrics[(sport, "threshold_hr")] = ImportedMetricValue(
                sport, "threshold_hr", threshold_hr
            )
        for zone_number in range(1, 11):
            floor = _direct_number(
                item,
                f"zone{zone_number}Floor",
                f"zone{zone_number}Low",
                f"zone{zone_number}LowerBoundary",
            )
            if floor is not None:
                floors[(sport, zone_number)] = floor
        zone_number = _direct_number(item, "zoneNumber", "zone")
        floor = _direct_number(item, "zoneLowBoundary", "lowBoundary", "lowerBoundary")
        if zone_number is not None and floor is not None and 1 <= zone_number <= 10:
            floors[(sport, round(zone_number))] = floor

    zones = []
    for sport in sorted({sport for sport, _ in floors}):
        sport_floors = sorted(
            (zone_number, floor)
            for (floor_sport, zone_number), floor in floors.items()
            if floor_sport == sport
        )
        for index, (zone_number, floor) in enumerate(sport_floors):
            upper = (
                sport_floors[index + 1][1] - 1
                if index + 1 < len(sport_floors)
                else maximums.get(sport)
            )
            zones.append(ImportedZoneValue(sport, zone_type, zone_number, floor, upper))
    return ParsedPerformanceResource(tuple(metrics.values()), tuple(zones))


def _sync_state(session: Session, user_id: int, resource: str) -> GarminSyncState:
    state = session.scalar(
        select(GarminSyncState).where(
            GarminSyncState.user_id == user_id,
            GarminSyncState.resource == resource,
        )
    )
    if state is None:
        state = GarminSyncState(user_id=user_id, resource=resource)
        session.add(state)
    return state


def _store_resource(
    session: Session,
    user_id: int,
    resource: str,
    parsed: ParsedPerformanceResource,
    fetched_at: datetime,
) -> None:
    fitness = get_or_create_daily_fitness(session, user_id, fetched_at.date())
    metric_fields = {
        "threshold_hr": "lactate_threshold_hr",
        "threshold_speed_mps": "lactate_threshold_speed_mps",
        "running_threshold_power_watts": "running_ftp_watts",
        "cycling_ftp_watts": "cycling_ftp_watts",
        "prediction_5k_seconds": "race_prediction_5k_seconds",
        "prediction_10k_seconds": "race_prediction_10k_seconds",
        "prediction_half_seconds": "race_prediction_half_seconds",
        "prediction_marathon_seconds": "race_prediction_marathon_seconds",
        "reference_1k_seconds": "personal_record_1k_seconds",
        "reference_5k_seconds": "personal_record_5k_seconds",
        "reference_10k_seconds": "personal_record_10k_seconds",
        "reference_half_seconds": "personal_record_half_seconds",
        "reference_marathon_seconds": "personal_record_marathon_seconds",
        "max_hr": "configured_max_hr",
    }
    integer_fields = {
        "lactate_threshold_hr",
        "race_prediction_5k_seconds",
        "race_prediction_10k_seconds",
        "race_prediction_half_seconds",
        "race_prediction_marathon_seconds",
        "personal_record_1k_seconds",
        "personal_record_5k_seconds",
        "personal_record_10k_seconds",
        "personal_record_half_seconds",
        "personal_record_marathon_seconds",
        "configured_max_hr",
    }
    for item in parsed.metrics:
        field = metric_fields.get(item.metric)
        if field is not None:
            setattr(fitness, field, round(item.value) if field in integer_fields else item.value)
    if parsed.zones:
        serialized: list[dict[str, object]] = [
            {
                "sport": item.sport,
                "zone": item.zone_number,
                "lower": item.lower_boundary,
                "upper": item.upper_boundary,
            }
            for item in parsed.zones
        ]
        if parsed.zones[0].zone_type == "heart_rate":
            fitness.heart_rate_zones = serialized
        else:
            fitness.power_zones = serialized


def sync_performance_profile(
    session: Session,
    client: Any,
    user_id: int,
    *,
    pacer: GarminPacer,
    now: datetime | None = None,
) -> None:
    fetched_at = now or utcnow()
    resources: tuple[
        tuple[str, str, Callable[[], Any], Callable[[Any], ParsedPerformanceResource]], ...
    ] = (
        (
            "lactate_threshold",
            "Laktatschwelle",
            lambda: client.get_lactate_threshold(),
            _parse_lactate_threshold,
        ),
        ("cycling_ftp", "Cycling FTP", lambda: client.get_cycling_ftp(), _parse_cycling_ftp),
        (
            "race_predictions",
            "Rennprognosen",
            lambda: client.get_race_predictions(),
            _parse_race_predictions,
        ),
        (
            "personal_records",
            "Persönliche Rekorde",
            lambda: client.get_personal_record(),
            _parse_personal_records,
        ),
        (
            "heart_rate_zones",
            "Herzfrequenzzonen",
            lambda: client.get_heart_rate_zones(),
            lambda payload: _parse_zones(payload, "heart_rate"),
        ),
        (
            "power_zones",
            "Leistungszonen",
            lambda: client.get_power_zones(),
            lambda payload: _parse_zones(payload, "power"),
        ),
    )
    for resource, label, call, parser in resources:
        state = _sync_state(session, user_id, resource)
        if state.last_success_at and fetched_at - state.last_success_at < PROFILE_REFRESH_INTERVAL:
            continue
        state.last_attempt_at = fetched_at
        try:
            payload = pacer.call(resource, call)
            parsed = parser(payload)
        except (GarminConnectAuthenticationError, GarminConnectTooManyRequestsError):
            raise
        except Exception as exc:
            state.status = "error"
            state.error = str(exc)[:1_000]
            logger.warning(
                "Garmin performance resource failed",
                extra={"sync_user_id": user_id, "garmin_resource": resource},
                exc_info=True,
            )
            continue
        _store_resource(session, user_id, resource, parsed, fetched_at)
        state.status = "ok" if parsed.metrics or parsed.zones else "empty"
        state.error = None
        state.last_success_at = fetched_at
        state.backfill_complete = True
        state.newest_synced_date = fetched_at.date()
        state.oldest_synced_date = state.oldest_synced_date or fetched_at.date()
        logger.info(
            "Garmin performance resource synchronized",
            extra={
                "sync_user_id": user_id,
                "garmin_resource": resource,
                "record_count": len(parsed.metrics) + len(parsed.zones),
                "garmin_resource_label": label,
            },
        )
