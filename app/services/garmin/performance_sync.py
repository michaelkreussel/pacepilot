import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)
from sqlalchemy.orm import Session

from app.models.user import utcnow
from app.repositories.fitness import get_or_create_daily_fitness
from app.repositories.sync_state import (
    get_or_create_sync_state,
    mark_sync_attempt,
    mark_sync_error,
    mark_sync_success,
)
from app.services.garmin.client import message_from_exception
from app.services.garmin.health_backfill import GarminPacer

PERFORMANCE_REPROBE_DAYS = 28


@dataclass(frozen=True)
class PerformanceSnapshot:
    day: date
    values: Mapping[str, float | int]


@dataclass(frozen=True)
class ParsedPerformance:
    snapshots: tuple[PerformanceSnapshot, ...]
    expected_values: int

    @property
    def populated_values(self) -> int:
        return sum(len(snapshot.values) for snapshot in self.snapshots)


@dataclass(frozen=True)
class PerformanceResource:
    name: str
    method_name: str
    fetch: Callable[[Any, date], Any]
    parse: Callable[[Any, date], ParsedPerformance]


@dataclass(frozen=True)
class PerformanceResourceResult:
    status: str
    stored_values: int = 0
    api_calls: int = 0
    skipped: bool = False


@dataclass
class PerformanceSyncResult:
    resources: dict[str, PerformanceResourceResult] = field(default_factory=dict)

    @property
    def api_calls(self) -> int:
        return sum(item.api_calls for item in self.resources.values())

    @property
    def stored_values(self) -> int:
        return sum(item.stored_values for item in self.resources.values())


class PerformanceSchemaError(ValueError):
    pass


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _source_day(payload: Mapping[str, Any], fallback: date) -> date:
    value = payload.get("calendarDate")
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return fallback


def _single_metric_parser(field: str, key: str) -> Callable[[Any, date], ParsedPerformance]:
    def parse(payload: Any, fallback: date) -> ParsedPerformance:
        if payload in (None, {}, []):
            return ParsedPerformance((), 1)
        if not isinstance(payload, Mapping) or key not in payload:
            raise PerformanceSchemaError(f"unexpected payload for {field}")
        value = _positive_number(payload.get(key))
        snapshots = (
            (PerformanceSnapshot(_source_day(payload, fallback), {field: value}),)
            if value is not None
            else ()
        )
        return ParsedPerformance(snapshots, 1)

    return parse


def _thresholds(payload: Any, fallback: date) -> ParsedPerformance:
    if payload in (None, {}, []):
        return ParsedPerformance((), 3)
    if not isinstance(payload, Mapping) or not {
        "speed_and_heart_rate",
        "power",
    }.intersection(payload):
        raise PerformanceSchemaError("unexpected running-threshold payload")
    grouped: dict[date, dict[str, float | int]] = {}
    speed_hr = payload.get("speed_and_heart_rate")
    if isinstance(speed_hr, Mapping):
        day = _source_day(speed_hr, fallback)
        speed = _positive_number(speed_hr.get("speed"))
        heart_rate = _positive_number(
            speed_hr.get("heartRate")
            if speed_hr.get("heartRate") is not None
            else speed_hr.get("hearRate")
        )
        if speed is not None:
            grouped.setdefault(day, {})["lactate_threshold_speed_mps"] = round(speed * 10, 4)
        if heart_rate is not None:
            grouped.setdefault(day, {})["lactate_threshold_hr"] = round(heart_rate)
    power = payload.get("power")
    if isinstance(power, Mapping):
        day = _source_day(power, fallback)
        value = next(
            (
                number
                for key in ("functionalThresholdPower", "thresholdPower", "power")
                if (number := _positive_number(power.get(key))) is not None
            ),
            None,
        )
        if value is not None:
            grouped.setdefault(day, {})["running_ftp_watts"] = value
    return ParsedPerformance(
        tuple(PerformanceSnapshot(day, values) for day, values in sorted(grouped.items())),
        3,
    )


def _cycling_ftp(payload: Any, fallback: date) -> ParsedPerformance:
    if payload in (None, {}, []):
        return ParsedPerformance((), 1)
    item = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(item, Mapping):
        raise PerformanceSchemaError("unexpected cycling-FTP payload")
    value = next(
        (
            number
            for key in ("functionalThresholdPower", "thresholdPower", "ftp")
            if (number := _positive_number(item.get(key))) is not None
        ),
        None,
    )
    if value is None:
        if item:
            raise PerformanceSchemaError("cycling-FTP value missing from payload")
        return ParsedPerformance((), 1)
    return ParsedPerformance(
        (PerformanceSnapshot(_source_day(item, fallback), {"cycling_ftp_watts": value}),),
        1,
    )


def _race_predictions(payload: Any, fallback: date) -> ParsedPerformance:
    if payload in (None, {}, []):
        return ParsedPerformance((), 4)
    item = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(item, Mapping):
        raise PerformanceSchemaError("unexpected race-prediction payload")
    fields = {
        "time5K": "race_prediction_5k_seconds",
        "time10K": "race_prediction_10k_seconds",
        "timeHalfMarathon": "race_prediction_half_seconds",
        "timeMarathon": "race_prediction_marathon_seconds",
    }
    if not any(key in item for key in fields):
        raise PerformanceSchemaError("race-prediction values missing from payload")
    values = {
        field: round(value)
        for key, field in fields.items()
        if (value := _positive_number(item.get(key))) is not None
    }
    return ParsedPerformance(
        (PerformanceSnapshot(_source_day(item, fallback), values),) if values else (),
        len(fields),
    )


RESOURCES = (
    PerformanceResource(
        "fitness_age",
        "get_fitnessage_data",
        lambda client, day: client.get_fitnessage_data(day.isoformat()),
        _single_metric_parser("fitness_age", "fitnessAge"),
    ),
    PerformanceResource(
        "endurance_score",
        "get_endurance_score",
        lambda client, day: client.get_endurance_score(day.isoformat()),
        _single_metric_parser("endurance_score", "overallScore"),
    ),
    PerformanceResource(
        "hill_score",
        "get_hill_score",
        lambda client, day: client.get_hill_score(day.isoformat()),
        _single_metric_parser("hill_score", "overallScore"),
    ),
    PerformanceResource(
        "running_thresholds",
        "get_lactate_threshold",
        lambda client, _day: client.get_lactate_threshold(),
        _thresholds,
    ),
    PerformanceResource(
        "cycling_ftp",
        "get_cycling_ftp",
        lambda client, _day: client.get_cycling_ftp(),
        _cycling_ftp,
    ),
    PerformanceResource(
        "race_predictions",
        "get_race_predictions",
        lambda client, _day: client.get_race_predictions(),
        _race_predictions,
    ),
)


def _http_status(exc: Exception) -> int | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while isinstance(current, Exception) and id(current) not in seen:
        seen.add(id(current))
        status = getattr(getattr(current, "response", None), "status_code", None)
        if isinstance(status, int):
            return status
        current = current.__cause__ or current.__context__
    return None


def _due(state: Any, today: date) -> bool:
    if state.last_attempt_at is None:
        return True
    attempted = state.last_attempt_at.date()
    if state.status in {"empty", "unsupported"}:
        return attempted <= today - timedelta(days=PERFORMANCE_REPROBE_DAYS)
    return attempted < today or state.status not in {"ok", "partial"}


def sync_performance_metrics(
    session: Session,
    client: Any,
    user_id: int,
    *,
    today: date | None = None,
    delay: float = 0.75,
    pacer: GarminPacer | None = None,
    log_context: Mapping[str, Any] | None = None,
) -> PerformanceSyncResult:
    target_day = today or date.today()
    request_pacer = pacer or GarminPacer(delay, log_context)
    result = PerformanceSyncResult()
    for resource in RESOURCES:
        state = get_or_create_sync_state(session, user_id, resource.name)
        if not hasattr(client, resource.method_name):
            state.status = "unsupported"
            state.backfill_complete = True
            state.last_attempt_at = utcnow()
            state.error = "Garmin-Client unterstützt diese Metrik nicht"
            session.commit()
            result.resources[resource.name] = PerformanceResourceResult(
                status="unsupported", skipped=True
            )
            continue
        if not _due(state, target_day):
            result.resources[resource.name] = PerformanceResourceResult(
                status=state.status, skipped=True
            )
            continue
        mark_sync_attempt(state)
        session.commit()
        try:
            payload = request_pacer.call(
                resource.name,
                lambda resource=resource: resource.fetch(client, target_day),
            )
            parsed = resource.parse(payload, target_day)
            for snapshot in parsed.snapshots:
                fitness = get_or_create_daily_fitness(session, user_id, snapshot.day)
                for field, value in snapshot.values.items():
                    setattr(fitness, field, value)
            source_days = [snapshot.day for snapshot in parsed.snapshots]
            mark_sync_success(
                state,
                oldest_date=min(source_days) if source_days else None,
                newest_date=max(source_days) if source_days else None,
                backfill_complete=True,
            )
            if not parsed.populated_values:
                state.status = "empty"
            elif parsed.populated_values < parsed.expected_values:
                state.status = "partial"
            session.commit()
            result.resources[resource.name] = PerformanceResourceResult(
                status=state.status,
                stored_values=parsed.populated_values,
                api_calls=1,
            )
        except PerformanceSchemaError as exc:
            session.rollback()
            state = get_or_create_sync_state(session, user_id, resource.name)
            mark_sync_error(state, str(exc))
            state.status = "schema_error"
            session.commit()
            result.resources[resource.name] = PerformanceResourceResult(
                status="schema_error", api_calls=1
            )
        except Exception as exc:
            session.rollback()
            state = get_or_create_sync_state(session, user_id, resource.name)
            status = _http_status(exc)
            if isinstance(exc, GarminConnectNotFoundError) or status == 404:
                state.status = "unsupported"
                state.backfill_complete = True
                state.last_attempt_at = utcnow()
                state.error = message_from_exception(exc)
                session.commit()
                result.resources[resource.name] = PerformanceResourceResult(
                    status="unsupported", api_calls=1
                )
                continue
            mark_sync_error(state, message_from_exception(exc))
            session.commit()
            if isinstance(
                exc,
                (GarminConnectAuthenticationError, GarminConnectTooManyRequestsError),
            ) or status in {401, 403, 429}:
                raise
            result.resources[resource.name] = PerformanceResourceResult(status="error", api_calls=1)
    return result
