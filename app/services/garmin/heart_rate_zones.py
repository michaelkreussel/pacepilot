import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)
from sqlalchemy.orm import Session

from app.models import GarminAccount
from app.models.user import utcnow
from app.services.garmin.client import message_from_exception
from app.services.garmin.health_backfill import GarminPacer


class HeartRateZoneSchemaError(ValueError):
    pass


@dataclass(frozen=True)
class HeartRateZoneSyncResult:
    status: str
    profiles: int = 0
    api_calls: int = 0
    error: str | None = None


def _positive_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return int(number) if math.isfinite(number) and number > 0 and number.is_integer() else None


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


def normalize_heart_rate_zone_profiles(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise HeartRateZoneSchemaError("unexpected heart-rate-zone payload")
    profiles: list[dict[str, Any]] = []
    sports: set[str] = set()
    for item in payload:
        if not isinstance(item, Mapping):
            raise HeartRateZoneSchemaError("invalid heart-rate-zone profile")
        sport = item.get("sport")
        method = item.get("trainingMethod")
        floors = [_positive_integer(item.get(f"zone{zone}Floor")) for zone in range(1, 6)]
        if not isinstance(sport, str) or not sport or not isinstance(method, str) or not method:
            raise HeartRateZoneSchemaError("heart-rate-zone profile identity missing")
        if any(floor is None for floor in floors):
            raise HeartRateZoneSchemaError("heart-rate-zone boundaries missing")
        normalized_floors = [floor for floor in floors if floor is not None]
        if any(
            left >= right
            for left, right in zip(normalized_floors, normalized_floors[1:], strict=False)
        ):
            raise HeartRateZoneSchemaError("heart-rate-zone boundaries are not increasing")
        if sport in sports:
            raise HeartRateZoneSchemaError("duplicate heart-rate-zone sport profile")
        sports.add(sport)
        max_hr = _positive_integer(item.get("maxHeartRateUsed"))
        resting_hr = _positive_integer(item.get("restingHeartRateUsed"))
        threshold_hr = _positive_integer(item.get("lactateThresholdHeartRateUsed"))
        if max_hr is not None and normalized_floors[-1] > max_hr:
            raise HeartRateZoneSchemaError("zone 5 exceeds the configured maximum heart rate")
        if method == "HR_MAX" and max_hr is None:
            raise HeartRateZoneSchemaError("maximum heart rate missing for HR_MAX")
        if method in {"HR_RESERVE", "HRR"} and (
            max_hr is None or resting_hr is None or resting_hr >= max_hr
        ):
            raise HeartRateZoneSchemaError("heart-rate reserve inputs are invalid")
        if method == "LACTATE_THRESHOLD" and threshold_hr is None:
            raise HeartRateZoneSchemaError("lactate-threshold heart rate missing")
        profiles.append(
            {
                "sport": sport,
                "training_method": method,
                "zone_floors": normalized_floors,
                "max_hr": max_hr,
                "resting_hr": resting_hr,
                "lactate_threshold_hr": threshold_hr,
            }
        )
    return profiles


def sync_heart_rate_zone_profiles(
    session: Session,
    client: Any,
    account: GarminAccount,
    pacer: GarminPacer,
) -> HeartRateZoneSyncResult:
    method = getattr(client, "get_heart_rate_zones", None)
    if not callable(method):
        return HeartRateZoneSyncResult(status="unsupported")
    try:
        payload = pacer.call("heart_rate_zones", method)
        profiles = normalize_heart_rate_zone_profiles(payload)
        account.heart_rate_zone_profiles = profiles
        account.heart_rate_zones_synced_at = utcnow()
        session.commit()
        return HeartRateZoneSyncResult(
            status="ok" if profiles else "empty",
            profiles=len(profiles),
            api_calls=1,
        )
    except HeartRateZoneSchemaError as exc:
        session.rollback()
        return HeartRateZoneSyncResult(status="schema_error", api_calls=1, error=str(exc))
    except GarminConnectNotFoundError as exc:
        session.rollback()
        return HeartRateZoneSyncResult(
            status="unsupported", api_calls=1, error=message_from_exception(exc)
        )
    except (GarminConnectAuthenticationError, GarminConnectTooManyRequestsError):
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        status = _http_status(exc)
        if status == 404:
            return HeartRateZoneSyncResult(
                status="unsupported", api_calls=1, error=message_from_exception(exc)
            )
        if status in {401, 403, 429}:
            raise
        return HeartRateZoneSyncResult(
            status="error", api_calls=1, error=message_from_exception(exc)
        )
