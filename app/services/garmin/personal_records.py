"""Import Garmin personal bests as performance anchor candidates.

Garmin Connect exposes accepted personal records per distance through the
unofficial ``record-service`` endpoint (``Garmin.get_personal_record``).
Only running time records map onto performance anchors; everything else
(longest distance, cycling or step records) is ignored.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PerformanceAnchor
from app.services.garmin.client import message_from_exception

logger = logging.getLogger(__name__)

# Garmin record type identifiers for running time records. Verified against a
# live payload: the recorded values match 1 km, 1 mile, 5 km, 10 km and
# half marathon times of the same activities.
RUNNING_RECORD_DISTANCES: dict[int, float] = {
    1: 1000.0,
    2: 1609.34,
    3: 5000.0,
    4: 10000.0,
    5: 21097.5,
}

RECORD_TYPE_LABELS: dict[int, str] = {
    1: "1 km",
    2: "1 Meile",
    3: "5 km",
    4: "10 km",
    5: "Halbmarathon",
}


@dataclass(frozen=True)
class PersonalRecordCandidate:
    record_type_id: int
    distance_m: float
    duration_s: float
    achieved_on: date
    activity_name: str | None
    activity_id: int | None


def _record_date(record: dict[str, Any]) -> date | None:
    for key in ("activityStartDateTimeLocalFormatted", "activityStartDateTimeInGMTFormatted"):
        raw = record.get(key)
        if isinstance(raw, str) and raw:
            try:
                return datetime.fromisoformat(raw).date()
            except ValueError:
                continue
    return None


def extract_running_records(payload: Any) -> list[PersonalRecordCandidate]:
    """Map a personal-record payload onto running anchor candidates."""
    if not isinstance(payload, list):
        logger.warning("Unexpected Garmin personal record payload type=%s", type(payload).__name__)
        return []
    candidates: list[PersonalRecordCandidate] = []
    for record in payload:
        if not isinstance(record, dict):
            continue
        if record.get("status") != "ACCEPTED":
            continue
        try:
            record_type_id = int(record.get("typeId"))
        except (TypeError, ValueError):
            continue
        distance_m = RUNNING_RECORD_DISTANCES.get(record_type_id)
        if distance_m is None:
            continue
        try:
            duration_s = float(record.get("value"))
        except (TypeError, ValueError):
            continue
        if duration_s <= 0:
            continue
        achieved_on = _record_date(record)
        if achieved_on is None:
            continue
        activity_id = record.get("activityId")
        candidates.append(
            PersonalRecordCandidate(
                record_type_id=record_type_id,
                distance_m=distance_m,
                duration_s=round(duration_s),
                achieved_on=achieved_on,
                activity_name=record.get("activityName")
                if isinstance(record.get("activityName"), str)
                else None,
                activity_id=activity_id if isinstance(activity_id, int) else None,
            )
        )
    candidates.sort(key=lambda candidate: candidate.distance_m)
    return candidates


def fetch_running_records(client: Any) -> list[PersonalRecordCandidate]:
    """Fetch accepted running records through a connected Garmin client."""
    return extract_running_records(client.get_personal_record())


DISTANCE_TOLERANCE_M = 1.0
DURATION_TOLERANCE_S = 5.0


@dataclass
class PersonalRecordSyncResult:
    status: str
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    api_calls: int = 0
    error: str | None = None


def _record_notes(candidate: PersonalRecordCandidate) -> str:
    if candidate.activity_name:
        return f"Garmin-Bestzeit · {candidate.activity_name}"
    return "Garmin-Bestzeit"


def _is_same_record(anchor: PerformanceAnchor, candidate: PersonalRecordCandidate) -> bool:
    return abs(anchor.duration_s - candidate.duration_s) <= DURATION_TOLERANCE_S


def sync_personal_records(
    session: Session, client: Any, user_id: int, *, pacer: Any | None = None
) -> PersonalRecordSyncResult:
    """Mirror accepted Garmin running records into garmin-sourced anchors.

    Creates a ``garmin`` anchor per standard distance that has neither a
    garmin row nor a manually captured equivalent, and refreshes measured
    values (duration, date, activity note) of managed rows only when they
    changed. Manually captured anchors and user overrides (kind, reliable)
    are never touched.
    """
    method = getattr(client, "get_personal_record", None)
    if not callable(method):
        return PersonalRecordSyncResult(status="unsupported")
    try:
        payload = pacer.call("personal_records", method) if pacer is not None else method()
    except (GarminConnectAuthenticationError, GarminConnectTooManyRequestsError):
        raise
    except GarminConnectNotFoundError as exc:
        return PersonalRecordSyncResult(
            status="unsupported", api_calls=1, error=message_from_exception(exc)
        )
    except Exception as exc:
        return PersonalRecordSyncResult(
            status="error", api_calls=1, error=message_from_exception(exc)
        )
    candidates = extract_running_records(payload)
    if not candidates:
        return PersonalRecordSyncResult(status="empty", api_calls=1)
    anchors = list(
        session.scalars(select(PerformanceAnchor).where(PerformanceAnchor.user_id == user_id)).all()
    )
    result = PersonalRecordSyncResult(status="ok", api_calls=1)
    try:
        for candidate in candidates:
            managed = next(
                (
                    anchor
                    for anchor in anchors
                    if anchor.source == "garmin"
                    and abs(anchor.distance_m - candidate.distance_m) <= DISTANCE_TOLERANCE_M
                ),
                None,
            )
            if managed is not None:
                if (
                    _is_same_record(managed, candidate)
                    and managed.achieved_on == candidate.achieved_on
                ):
                    result.unchanged += 1
                    continue
                managed.duration_s = candidate.duration_s
                managed.achieved_on = candidate.achieved_on
                managed.notes = _record_notes(candidate)
                result.updated += 1
                continue
            manual_match = any(
                anchor.source == "manual"
                and abs(anchor.distance_m - candidate.distance_m) <= DISTANCE_TOLERANCE_M
                and _is_same_record(anchor, candidate)
                for anchor in anchors
            )
            if manual_match:
                result.unchanged += 1
                continue
            anchor = PerformanceAnchor(
                user_id=user_id,
                kind="manual",
                source="garmin",
                distance_m=candidate.distance_m,
                duration_s=candidate.duration_s,
                achieved_on=candidate.achieved_on,
                notes=_record_notes(candidate),
            )
            session.add(anchor)
            anchors.append(anchor)
            result.created += 1
        session.commit()
    except Exception as exc:
        session.rollback()
        return PersonalRecordSyncResult(
            status="error", api_calls=1, error=message_from_exception(exc)
        )
    return result
