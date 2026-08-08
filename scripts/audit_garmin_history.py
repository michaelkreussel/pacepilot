"""Audit Garmin Connect data availability without persisting personal payloads.

Run from the repository root:

    uv run python scripts/audit_garmin_history.py

The JSON report contains dates, response shapes, and field names, but no metric
values, activity names, coordinates, device IDs, or profile identifiers.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any

from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.garmin.client import connect_garmin  # noqa: E402

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
PresenceCheck = Callable[[Any], bool]
DateCall = Callable[[str], Any]

SENSITIVE_PATH_PARTS = {
    "activityname",
    "description",
    "devicedisplayname",
    "displayname",
    "fullname",
    "gearname",
    "latitude",
    "longitude",
    "ownerdisplayname",
    "profileid",
    "userprofileid",
    "userprofilepk",
}


class AuditHalted(RuntimeError):
    """Raised when continuing would risk authentication or rate-limit problems."""


@dataclass(frozen=True)
class ProbeResult:
    status: str
    has_data: bool
    shape: str
    field_paths: list[str]
    item_count: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class MetricSpec:
    name: str
    method: str
    call: DateCall
    present: PresenceCheck
    granularity: str = "daily"


def _status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    match = re.search(r"(?:API Error|Error|HTTP|client error)\D*(\d{3})", str(exc))
    return int(match.group(1)) if match else None


def classify_exception(exc: Exception) -> str:
    status = _status_code(exc)
    if isinstance(exc, GarminConnectAuthenticationError) or status in {401, 403}:
        return "authentication_failure"
    if isinstance(exc, GarminConnectTooManyRequestsError) or status == 429:
        return "rate_limited"
    if isinstance(exc, GarminConnectNotFoundError) or status == 404:
        return "unsupported"
    return "api_failure"


def _safe_error(exc: Exception) -> str:
    return (str(exc).strip() or exc.__class__.__name__)[:500]


def field_paths(value: Any, *, limit: int = 300) -> list[str]:
    """Return structural field paths while excluding identifiers and personal text."""
    paths: set[str] = set()

    def walk(item: Any, prefix: str, depth: int) -> None:
        if len(paths) >= limit or depth > 8:
            return
        if isinstance(item, dict):
            for raw_key, child in item.items():
                key = str(raw_key)
                normalized = key.casefold()
                if normalized in SENSITIVE_PATH_PARTS or normalized.endswith("uuid"):
                    continue
                path = f"{prefix}.{key}" if prefix else key
                paths.add(path)
                walk(child, path, depth + 1)
        elif isinstance(item, list):
            path = f"{prefix}[]" if prefix else "[]"
            paths.add(path)
            for child in item[:5]:
                walk(child, path, depth + 1)

    walk(value, "", 0)
    return sorted(paths)[:limit]


def response_shape(value: Any) -> tuple[str, int | None]:
    if isinstance(value, dict):
        return "object", len(value)
    if isinstance(value, list):
        return "array", len(value)
    if value is None:
        return "null", None
    return type(value).__name__, None


def _numbers_for_keys(value: Any, keys: set[str]) -> Iterable[float]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and isinstance(child, (int, float)) and not isinstance(child, bool):
                yield float(child)
            yield from _numbers_for_keys(child, keys)
    elif isinstance(value, list):
        for child in value:
            yield from _numbers_for_keys(child, keys)


def has_positive_key(value: Any, *keys: str) -> bool:
    return any(number > 0 for number in _numbers_for_keys(value, set(keys)))


def has_nonempty_collection(value: Any, *keys: str) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and isinstance(child, (dict, list)) and bool(child):
                return True
            if has_nonempty_collection(child, *keys):
                return True
    elif isinstance(value, list):
        return any(has_nonempty_collection(child, *keys) for child in value)
    return False


def has_non_null_samples(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        for child_key, child in value.items():
            if (
                child_key == key
                and isinstance(child, list)
                and any(
                    isinstance(row, list) and len(row) > 1 and row[1] is not None for row in child
                )
            ):
                return True
            if has_non_null_samples(child, key):
                return True
    elif isinstance(value, list):
        return any(has_non_null_samples(child, key) for child in value)
    return False


def generic_metric_data(value: Any) -> bool:
    """Detect metric payloads while ignoring date/profile metadata and zero placeholders."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.casefold()
            if (
                normalized.endswith(("id", "pk"))
                or "date" in normalized
                or "timestamp" in normalized
            ):
                continue
            if isinstance(child, (int, float)) and not isinstance(child, bool) and child != 0:
                return True
            if isinstance(child, (dict, list)) and generic_metric_data(child):
                return True
        return False
    if isinstance(value, list):
        return any(generic_metric_data(child) for child in value)
    return False


def daily_summary_data(value: Any) -> bool:
    return has_positive_key(
        value,
        "totalSteps",
        "totalDistanceMeters",
        "activeKilocalories",
        "restingHeartRate",
        "minHeartRate",
        "maxHeartRate",
        "activeSeconds",
        "highlyActiveSeconds",
        "averageStressLevel",
        "bodyBatteryHighestValue",
        "moderateIntensityMinutes",
        "vigorousIntensityMinutes",
    )


def heart_rate_data(value: Any) -> bool:
    return has_positive_key(value, "restingHeartRate", "minHeartRate", "maxHeartRate") or (
        has_nonempty_collection(value, "heartRateValues")
    )


def steps_data(value: Any) -> bool:
    return has_positive_key(value, "steps", "totalSteps")


def stress_data(value: Any) -> bool:
    return has_positive_key(
        value, "overallStressLevel", "avgStressLevel", "maxStressLevel", "stressDuration"
    ) or has_nonempty_collection(value, "stressValuesArray", "stressChartValueOffset")


def body_battery_data(value: Any) -> bool:
    return has_positive_key(value, "charged", "drained") or has_non_null_samples(
        value, "bodyBatteryValuesArray"
    )


def sleep_data(value: Any) -> bool:
    return has_positive_key(
        value,
        "sleepTimeSeconds",
        "deepSleepSeconds",
        "lightSleepSeconds",
        "remSleepSeconds",
    )


def hrv_data(value: Any) -> bool:
    return has_positive_key(value, "lastNightAvg", "weeklyAvg", "lastNight5MinHigh") or (
        has_nonempty_collection(value, "hrvReadings")
    )


def respiration_data(value: Any) -> bool:
    return has_positive_key(
        value,
        "avgSleepRespirationValue",
        "avgWakingRespirationValue",
        "highestRespirationValue",
        "lowestRespirationValue",
    ) or has_non_null_samples(value, "respirationValuesArray")


def spo2_data(value: Any) -> bool:
    return has_positive_key(
        value, "averageSpO2", "avgSleepSpO2", "latestSpO2", "lowestSpO2"
    ) or has_nonempty_collection(value, "continuousReadingDTOList", "spO2SingleValues")


def intensity_minutes_data(value: Any) -> bool:
    return has_positive_key(
        value, "moderateMinutes", "vigorousMinutes", "weeklyModerate", "weeklyVigorous"
    ) or has_non_null_samples(value, "imValuesArray")


def readiness_data(value: Any) -> bool:
    return has_positive_key(value, "score", "recoveryTime")


class AuditRunner:
    def __init__(self, *, delay: float, max_calls: int) -> None:
        self.delay = delay
        self.max_calls = max_calls
        self.calls = 0
        self._last_call_at: float | None = None

    def _wait(self) -> None:
        if self._last_call_at is None or self.delay <= 0:
            return
        elapsed = time.monotonic() - self._last_call_at
        wait = self.delay * random.uniform(0.9, 1.1) - elapsed  # noqa: S311
        if wait > 0:
            time.sleep(wait)

    def call(self, operation: str, call: Callable[[], Any]) -> tuple[Any, ProbeResult]:
        if self.calls >= self.max_calls:
            raise AuditHalted(f"maximum of {self.max_calls} Garmin calls reached")
        self._wait()
        self.calls += 1
        print(f"[{self.calls}/{self.max_calls}] {operation}", file=sys.stderr)
        try:
            value = call()
        except Exception as exc:
            status = classify_exception(exc)
            result = ProbeResult(
                status=status,
                has_data=False,
                shape="error",
                field_paths=[],
                error=_safe_error(exc),
            )
            if status in {"authentication_failure", "rate_limited"}:
                raise AuditHalted(f"{status}: {_safe_error(exc)}") from exc
            return None, result
        finally:
            self._last_call_at = time.monotonic()
        shape, count = response_shape(value)
        return value, ProbeResult(
            status="available" if bool(value) else "empty",
            has_data=bool(value),
            shape=shape,
            item_count=count,
            field_paths=field_paths(value),
        )

    def probe(self, operation: str, call: Callable[[], Any], present: PresenceCheck) -> ProbeResult:
        value, result = self.call(operation, call)
        if result.status not in {"available", "empty"}:
            return result
        has_data = present(value)
        return ProbeResult(
            status="available" if has_data else "empty",
            has_data=has_data,
            shape=result.shape,
            item_count=result.item_count,
            field_paths=result.field_paths,
        )


def find_earliest_date(
    spec: MetricSpec,
    runner: AuditRunner,
    *,
    end_date: date,
    min_date: date,
) -> dict[str, Any]:
    """Find an observed boundary with logarithmic probes and a short final scan."""
    cache: dict[date, ProbeResult] = {}

    def probe(day: date) -> ProbeResult:
        if day not in cache:
            value = day.isoformat()
            cache[day] = runner.probe(
                f"{spec.name} {value}", lambda: spec.call(value), spec.present
            )
        return cache[day]

    recent_offsets = (0, 1, 2, 3, 7, 14, 30, 60, 90)
    latest = next(
        (
            candidate
            for offset in recent_offsets
            if (candidate := end_date - timedelta(days=offset)) >= min_date
            and probe(candidate).has_data
        ),
        None,
    )
    if latest is None:
        statuses = sorted({result.status for result in cache.values()})
        return {
            "method": spec.method,
            "granularity": spec.granularity,
            "available": False,
            "earliest_observed": None,
            "latest_observed": None,
            "boundary_confirmed": False,
            "probe_count": len(cache),
            "statuses": statuses,
            "notes": "No populated response in the most recent 90-day probe set.",
        }

    oldest_present = latest
    older_empty: date | None = None
    step = 30
    while True:
        candidate = latest - timedelta(days=step)
        if candidate <= min_date:
            candidate = min_date
        result = probe(candidate)
        if result.has_data:
            oldest_present = min(oldest_present, candidate)
            if candidate == min_date:
                break
            step *= 2
            continue

        # Confirm a milestone is not merely a one-day recording gap.
        nearby_present = []
        for offset in (1, -1, 3, -3, 7, -7):
            nearby = candidate + timedelta(days=offset)
            if min_date <= nearby < oldest_present and probe(nearby).has_data:
                nearby_present.append(nearby)
        if nearby_present:
            oldest_present = min(oldest_present, *nearby_present)
            if candidate == min_date:
                break
            step *= 2
            continue
        older_empty = candidate
        break

    if older_empty is None:
        return {
            "method": spec.method,
            "granularity": spec.granularity,
            "available": True,
            "earliest_observed": oldest_present.isoformat(),
            "latest_observed": latest.isoformat(),
            "boundary_confirmed": False,
            "probe_count": len(cache),
            "statuses": sorted({result.status for result in cache.values()}),
            "notes": f"Data exists at the configured lower bound {min_date.isoformat()}.",
        }

    # Daily streams are usually continuous after device adoption. Narrow to one week,
    # then inspect each remaining day so an API range limit is never mistaken for retention.
    while (oldest_present - older_empty).days > 7:
        middle = older_empty + (oldest_present - older_empty) // 2
        if probe(middle).has_data:
            oldest_present = middle
        else:
            older_empty = middle
    first = next(
        (
            day
            for offset in range(1, (oldest_present - older_empty).days + 1)
            if probe(day := older_empty + timedelta(days=offset)).has_data
        ),
        oldest_present,
    )
    return {
        "method": spec.method,
        "granularity": spec.granularity,
        "available": True,
        "earliest_observed": first.isoformat(),
        "latest_observed": latest.isoformat(),
        "boundary_confirmed": True,
        "last_confirmed_empty": older_empty.isoformat(),
        "probe_count": len(cache),
        "statuses": sorted({result.status for result in cache.values()}),
        "notes": (
            "Boundary inferred from a normally continuous daily stream; isolated gaps may exist."
        ),
    }


def _metric_specs(client: Any) -> list[MetricSpec]:
    return [
        MetricSpec("daily_health", "get_user_summary", client.get_user_summary, daily_summary_data),
        MetricSpec("steps", "get_steps_data", client.get_steps_data, steps_data),
        MetricSpec("heart_rate", "get_heart_rates", client.get_heart_rates, heart_rate_data),
        MetricSpec("stress", "get_all_day_stress", client.get_all_day_stress, stress_data),
        MetricSpec(
            "body_battery",
            "get_body_battery",
            lambda day: client.get_body_battery(day),
            body_battery_data,
        ),
        MetricSpec(
            "respiration",
            "get_respiration_data",
            client.get_respiration_data,
            respiration_data,
        ),
        MetricSpec("spo2", "get_spo2_data", client.get_spo2_data, spo2_data),
        MetricSpec(
            "intensity_minutes",
            "get_intensity_minutes_data",
            client.get_intensity_minutes_data,
            intensity_minutes_data,
        ),
        MetricSpec("sleep", "get_sleep_data", client.get_sleep_data, sleep_data),
        MetricSpec("hrv", "get_hrv_data", client.get_hrv_data, hrv_data),
        MetricSpec(
            "training_readiness",
            "get_training_readiness",
            client.get_training_readiness,
            readiness_data,
        ),
        MetricSpec("vo2max", "get_max_metrics", client.get_max_metrics, generic_metric_data),
        MetricSpec(
            "training_status_load",
            "get_training_status",
            client.get_training_status,
            generic_metric_data,
        ),
    ]


def audit_endpoint_inventory(client: Any, runner: AuditRunner, day: date) -> dict[str, Any]:
    day_string = day.isoformat()
    start = (day - timedelta(days=27)).isoformat()
    endpoint_calls: list[tuple[str, str, Callable[[], Any], PresenceCheck, str]] = [
        (
            "daily_summary",
            "get_user_summary",
            lambda: client.get_user_summary(day_string),
            daily_summary_data,
            "daily",
        ),
        (
            "steps_intraday",
            "get_steps_data",
            lambda: client.get_steps_data(day_string),
            generic_metric_data,
            "15-minute epochs",
        ),
        (
            "heart_rate",
            "get_heart_rates",
            lambda: client.get_heart_rates(day_string),
            heart_rate_data,
            "daily summary + samples",
        ),
        (
            "resting_hr",
            "get_rhr_day",
            lambda: client.get_rhr_day(day_string),
            generic_metric_data,
            "daily",
        ),
        (
            "stress",
            "get_all_day_stress",
            lambda: client.get_all_day_stress(day_string),
            stress_data,
            "daily summary + samples",
        ),
        (
            "body_battery",
            "get_body_battery",
            lambda: client.get_body_battery(day_string),
            body_battery_data,
            "daily samples",
        ),
        (
            "body_battery_events",
            "get_body_battery_events",
            lambda: client.get_body_battery_events(day_string),
            generic_metric_data,
            "events",
        ),
        (
            "respiration",
            "get_respiration_data",
            lambda: client.get_respiration_data(day_string),
            generic_metric_data,
            "daily summary + samples",
        ),
        (
            "spo2",
            "get_spo2_data",
            lambda: client.get_spo2_data(day_string),
            generic_metric_data,
            "daily summary + samples",
        ),
        (
            "intensity_minutes",
            "get_intensity_minutes_data",
            lambda: client.get_intensity_minutes_data(day_string),
            generic_metric_data,
            "daily",
        ),
        (
            "sleep",
            "get_sleep_data",
            lambda: client.get_sleep_data(day_string),
            sleep_data,
            "sleep session + samples",
        ),
        (
            "hrv",
            "get_hrv_data",
            lambda: client.get_hrv_data(day_string),
            hrv_data,
            "nightly summary + samples",
        ),
        (
            "training_readiness",
            "get_training_readiness",
            lambda: client.get_training_readiness(day_string),
            readiness_data,
            "snapshots",
        ),
        (
            "morning_training_readiness",
            "get_morning_training_readiness",
            lambda: client.get_morning_training_readiness(day_string),
            readiness_data,
            "morning snapshot",
        ),
        (
            "vo2max",
            "get_max_metrics",
            lambda: client.get_max_metrics(day_string),
            generic_metric_data,
            "daily",
        ),
        (
            "training_status_load",
            "get_training_status",
            lambda: client.get_training_status(day_string),
            generic_metric_data,
            "daily aggregate",
        ),
        (
            "endurance_score",
            "get_endurance_score",
            lambda: client.get_endurance_score(day_string),
            generic_metric_data,
            "daily",
        ),
        (
            "hill_score",
            "get_hill_score",
            lambda: client.get_hill_score(day_string),
            generic_metric_data,
            "daily",
        ),
        (
            "running_tolerance",
            "get_running_tolerance",
            lambda: client.get_running_tolerance(start, day_string, "daily"),
            generic_metric_data,
            "daily range",
        ),
        (
            "race_predictions",
            "get_race_predictions",
            client.get_race_predictions,
            generic_metric_data,
            "latest",
        ),
        (
            "lactate_threshold",
            "get_lactate_threshold",
            client.get_lactate_threshold,
            generic_metric_data,
            "latest",
        ),
        ("cycling_ftp", "get_cycling_ftp", client.get_cycling_ftp, generic_metric_data, "latest"),
        (
            "fitness_age",
            "get_fitnessage_data",
            lambda: client.get_fitnessage_data(day_string),
            generic_metric_data,
            "daily",
        ),
        (
            "heart_rate_zones",
            "get_heart_rate_zones",
            client.get_heart_rate_zones,
            generic_metric_data,
            "current configuration",
        ),
        (
            "power_zones",
            "get_power_zones",
            client.get_power_zones,
            generic_metric_data,
            "current configuration",
        ),
        (
            "all_day_events",
            "get_all_day_events",
            lambda: client.get_all_day_events(day_string),
            generic_metric_data,
            "events",
        ),
        (
            "hydration",
            "get_hydration_data",
            lambda: client.get_hydration_data(day_string),
            generic_metric_data,
            "daily",
        ),
        (
            "body_composition",
            "get_body_composition",
            lambda: client.get_body_composition(day_string),
            generic_metric_data,
            "measurements",
        ),
        (
            "blood_pressure",
            "get_blood_pressure",
            lambda: client.get_blood_pressure(day_string),
            generic_metric_data,
            "measurements",
        ),
        (
            "personal_records",
            "get_personal_record",
            client.get_personal_record,
            generic_metric_data,
            "current records",
        ),
    ]
    inventory: dict[str, Any] = {}
    for name, method, call, present, granularity in endpoint_calls:
        result = runner.probe(f"inventory {name}", call, present)
        inventory[name] = {"method": method, "granularity": granularity, **asdict(result)}
    return inventory


def _activity_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict) and isinstance(value.get("activityList"), list):
        return [item for item in value["activityList"] if isinstance(item, dict)]
    return []


def _activity_date(activity: dict[str, Any]) -> str | None:
    value = activity.get("startTimeLocal") or activity.get("startTimeGMT")
    return str(value)[:10] if isinstance(value, str) else None


def _activity_type(activity: dict[str, Any]) -> str | None:
    value = activity.get("activityType")
    return str(value.get("typeKey")) if isinstance(value, dict) and value.get("typeKey") else None


def _descriptor_keys(value: Any) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get("metricDescriptors"), list):
        return []
    return sorted(
        {
            str(item["key"])
            for item in value["metricDescriptors"]
            if isinstance(item, dict) and item.get("key")
        }
    )


def _field_groups(paths: Iterable[str], descriptors: Iterable[str] = ()) -> list[str]:
    text = " ".join([*paths, *descriptors]).casefold()
    groups = {
        "heart_rate": ("heartrate", "heart_rate"),
        "hr_zones": ("hrtimeinzone", "heart rate zone"),
        "power": ("power", "ftp"),
        "training_effect_load": ("trainingeffect", "trainingload", "exercise load"),
        "cadence": ("cadence",),
        "running_dynamics": (
            "groundcontact",
            "verticaloscillation",
            "verticalratio",
            "stridelength",
        ),
        "elevation": ("elevation", "altitude"),
        "pace_speed": ("speed", "pace"),
        "laps_splits_intervals": ("lap", "split", "interval"),
        "workout_structure": ("workout", "exercise", "set"),
        "perceived_effort": ("perceived", "selfevaluation", "feel"),
    }
    return sorted(name for name, needles in groups.items() if any(item in text for item in needles))


def audit_activity(client: Any, runner: AuditRunner) -> dict[str, Any]:
    count_value, count_probe = runner.call("activity count", client.count_activities)
    count = count_value if isinstance(count_value, int) else None
    result: dict[str, Any] = {"count": count, "count_probe": asdict(count_probe)}
    if not count:
        return result

    samples: dict[str, Any] = {}
    for label, offset in (("recent", 0), ("oldest", count - 1)):
        list_value, list_probe = runner.call(
            f"{label} activity list entry", lambda offset=offset: client.get_activities(offset, 1)
        )
        activities = _activity_list(list_value)
        if not activities:
            samples[label] = {"list": asdict(list_probe), "error": "No activity returned."}
            continue
        activity = activities[0]
        activity_id = activity.get("activityId")
        sample: dict[str, Any] = {
            "date": _activity_date(activity),
            "activity_type": _activity_type(activity),
            "list_fields": field_paths(activity),
            "list_field_groups": _field_groups(field_paths(activity)),
            "endpoints": {},
        }
        if not activity_id:
            sample["error"] = "Activity has no activityId."
            samples[label] = sample
            continue
        endpoint_calls: list[tuple[str, Callable[[], Any]]] = [
            ("summary", lambda activity_id=activity_id: client.get_activity(activity_id)),
            (
                "details",
                lambda activity_id=activity_id: client.get_activity_details(
                    activity_id, maxchart=200, maxpoly=0
                ),
            ),
            ("splits", lambda activity_id=activity_id: client.get_activity_splits(activity_id)),
            (
                "typed_splits",
                lambda activity_id=activity_id: client.get_activity_typed_splits(activity_id),
            ),
            (
                "split_summaries",
                lambda activity_id=activity_id: client.get_activity_split_summaries(activity_id),
            ),
            (
                "hr_zones",
                lambda activity_id=activity_id: client.get_activity_hr_in_timezones(activity_id),
            ),
            (
                "power_zones",
                lambda activity_id=activity_id: client.get_activity_power_in_timezones(activity_id),
            ),
            (
                "exercise_sets",
                lambda activity_id=activity_id: client.get_activity_exercise_sets(activity_id),
            ),
        ]
        combined_paths = list(sample["list_fields"])
        descriptors: list[str] = []
        for endpoint, call in endpoint_calls:
            value, probe = runner.call(f"{label} activity {endpoint}", call)
            endpoint_result = asdict(probe)
            if endpoint == "details":
                endpoint_result["metric_descriptor_keys"] = _descriptor_keys(value)
                descriptors.extend(endpoint_result["metric_descriptor_keys"])
            sample["endpoints"][endpoint] = endpoint_result
            combined_paths.extend(probe.field_paths)
        sample["all_field_groups"] = _field_groups(combined_paths, descriptors)
        samples[label] = sample
    result["samples"] = samples
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=date.today() - timedelta(days=1),
        help="recent complete day to inspect (default: yesterday)",
    )
    parser.add_argument(
        "--min-date",
        type=date.fromisoformat,
        default=date(2005, 1, 1),
        help="oldest date the progressive search may inspect",
    )
    parser.add_argument("--delay", type=float, default=0.75, help="minimum delay between calls")
    parser.add_argument("--max-calls", type=int, default=300, help="hard Garmin call budget")
    parser.add_argument(
        "--output", type=Path, default=Path("data/garmin-audit.json"), help="JSON report path"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.min_date > args.date:
        raise SystemExit("--min-date must not be after --date")
    if args.delay < 0 or args.max_calls < 1:
        raise SystemExit("--delay must be non-negative and --max-calls must be positive")

    report: dict[str, Any] = {
        "audit_date": date.today().isoformat(),
        "sample_date": args.date.isoformat(),
        "minimum_probe_date": args.min_date.isoformat(),
        "garminconnect_version": version("garminconnect"),
        "privacy": "No metric values, names, coordinates, device IDs, or profile IDs are stored.",
        "status": "running",
    }
    runner = AuditRunner(delay=args.delay, max_calls=args.max_calls)
    exit_code = 0
    try:
        client = connect_garmin()
        report["endpoint_inventory"] = audit_endpoint_inventory(client, runner, args.date)
        report["history"] = {}
        for spec in _metric_specs(client):
            report["history"][spec.name] = find_earliest_date(
                spec, runner, end_date=args.date, min_date=args.min_date
            )
        report["activities"] = audit_activity(client, runner)
        report["status"] = "complete"
    except AuditHalted as exc:
        report["status"] = "halted"
        report["error"] = str(exc)
        exit_code = 2
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = _safe_error(exc)
        exit_code = 1
    finally:
        report["garmin_calls"] = runner.calls
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"Audit report written to {args.output} ({report['status']}).")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
