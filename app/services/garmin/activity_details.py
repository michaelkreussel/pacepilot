import gzip
import json
import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

from garminconnect.activity_details import parse_activity_detail_metrics

from app.config import get_settings

MAX_DETAIL_BYTES = 25 * 1024 * 1024


def activity_details_path(started_at: datetime, activity_id: str) -> Path:
    if not activity_id.isdecimal():
        raise ValueError("Garmin activity ID must be numeric")
    return (
        get_settings().data_dir
        / "raw"
        / "activities"
        / str(started_at.year)
        / f"{activity_id}.details.json.gz"
    )


def write_activity_details(path: Path, details: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    with gzip.open(temporary_path, "wt", encoding="utf-8") as raw_file:
        json.dump(details, raw_file, ensure_ascii=False)
    temporary_path.replace(path)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _time_source(samples: list[dict[str, Any]]) -> tuple[str, str]:
    for key, label in (
        ("sumMovingDuration", "Bewegungszeit"),
        ("sumDuration", "Timerzeit"),
        ("sumElapsedDuration", "Gesamtzeit"),
    ):
        values = [_number(sample.get(key)) for sample in samples]
        valid_values = [value for value in values if value is not None]
        if valid_values and max(valid_values) > min(valid_values):
            return key, label
    return "directTimestamp", "Trainingszeit"


def _elapsed_seconds(
    sample: dict[str, Any], time_source: str, first_timestamp: float | None
) -> float | None:
    if time_source != "directTimestamp":
        value = _number(sample.get(time_source))
        return value if value is not None and value >= 0 else None
    timestamp = _number(sample.get("directTimestamp"))
    if timestamp is None or first_timestamp is None:
        return None
    return max(0.0, (timestamp - first_timestamp) / 1000)


def _coordinate(point: object) -> list[float] | None:
    if not isinstance(point, dict) or point.get("valid") is False:
        return None
    latitude = _number(point.get("lat"))
    longitude = _number(point.get("lon"))
    if latitude is None or longitude is None:
        return None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    if latitude == 0 and longitude == 0:
        return None
    return [latitude, longitude]


def normalize_activity_details(details: dict[str, Any], activity_type: str) -> dict[str, Any]:
    samples = parse_activity_detail_metrics(details)
    time_source, time_axis_label = _time_source(samples)
    timestamps = [
        timestamp
        for sample in samples
        if (timestamp := _number(sample.get("directTimestamp"))) is not None
    ]
    first_timestamp = min(timestamps) if timestamps else None
    series: dict[str, list[list[float]]] = {
        "heart_rate": [],
        "speed": [],
        "pace": [],
        "cadence": [],
    }
    direct_route: list[list[float]] = []
    is_cycling = "cycling" in activity_type or "biking" in activity_type
    cadence_values: list[float] = []
    power_values: list[float] = []
    stride_length_values: list[float] = []
    ground_contact_values: list[float] = []
    vertical_oscillation_values: list[float] = []
    vertical_ratio_values: list[float] = []

    for sample in samples:
        elapsed = _elapsed_seconds(sample, time_source, first_timestamp)
        if elapsed is None:
            continue

        heart_rate = _number(sample.get("directHeartRate"))
        if heart_rate is not None and heart_rate > 0:
            series["heart_rate"].append([elapsed, heart_rate])

        speed = _number(sample.get("directSpeed"))
        if speed is not None and speed > 0:
            series["speed"].append([elapsed, speed * 3.6])
            series["pace"].append([elapsed, 1000 / speed])

        if is_cycling:
            cadence = _number(sample.get("directBikeCadence"))
        else:
            cadence = _number(sample.get("directDoubleCadence"))
            if cadence is None:
                run_cadence = _number(sample.get("directRunCadence"))
                fractional = _number(sample.get("directFractionalCadence")) or 0
                cadence = (run_cadence + fractional) * 2 if run_cadence is not None else None
        if cadence is not None and cadence > 0:
            series["cadence"].append([elapsed, cadence])
            cadence_values.append(cadence)

        for key, values in (
            ("directPower", power_values),
            ("directStrideLength", stride_length_values),
            ("directGroundContactTime", ground_contact_values),
            ("directVerticalOscillation", vertical_oscillation_values),
            ("directVerticalRatio", vertical_ratio_values),
        ):
            value = _number(sample.get(key))
            if value is not None and value > 0:
                values.append(value)

        coordinate = _coordinate(
            {"lat": sample.get("directLatitude"), "lon": sample.get("directLongitude")}
        )
        if coordinate is not None and (not direct_route or coordinate != direct_route[-1]):
            direct_route.append(coordinate)

    polyline = details.get("geoPolylineDTO")
    polyline = polyline.get("polyline") if isinstance(polyline, dict) else []
    route: list[list[float]] = []
    if isinstance(polyline, list):
        for point in polyline:
            coordinate = _coordinate(point)
            if coordinate is not None and (not route or coordinate != route[-1]):
                route.append(coordinate)

    if series["pace"]:
        pace_values = sorted(point[1] for point in series["pace"])
        median_pace = statistics.median(pace_values)
        percentile_99 = pace_values[int((len(pace_values) - 1) * 0.99)]
        maximum_pace = max(percentile_99 * 1.1, median_pace * 1.35)
        series["pace"] = [point for point in series["pace"] if point[1] <= maximum_pace]

    def maximum(key: str) -> float | None:
        values = [_number(sample.get(key)) for sample in samples]
        return (
            max(value for value in values if value is not None)
            if any(value is not None for value in values)
            else None
        )

    def average(values: list[float]) -> float | None:
        return statistics.fmean(values) if values else None

    moving_time = maximum("sumMovingDuration")
    distance = maximum("sumDistance")
    average_pace = (
        moving_time * 1000 / distance
        if moving_time is not None and distance is not None and distance > 0
        else None
    )

    return {
        "series": series,
        "route": route or direct_route,
        "is_running": "running" in activity_type,
        "is_cycling": is_cycling,
        "is_strength": "strength" in activity_type,
        "time_axis_label": time_axis_label,
        "summary": {
            "moving_time": moving_time,
            "timer_time": maximum("sumDuration"),
            "elapsed_time": maximum("sumElapsedDuration"),
            "average_pace": average_pace,
            "average_cadence": average(cadence_values),
            "average_power": average(power_values),
            "stride_length_cm": average(stride_length_values),
            "ground_contact_ms": average(ground_contact_values),
            "vertical_oscillation_cm": average(vertical_oscillation_values),
            "vertical_ratio": average(vertical_ratio_values),
        },
    }


def empty_activity_details(activity_type: str) -> dict[str, Any]:
    return normalize_activity_details({}, activity_type)


def load_activity_details(
    started_at: datetime, activity_id: str, activity_type: str
) -> dict[str, Any]:
    try:
        path = activity_details_path(started_at, activity_id)
        if not path.is_file() or path.stat().st_size > MAX_DETAIL_BYTES:
            return empty_activity_details(activity_type)
        with gzip.open(path, "rb") as raw_file:
            payload = raw_file.read(MAX_DETAIL_BYTES + 1)
        if len(payload) > MAX_DETAIL_BYTES:
            return empty_activity_details(activity_type)
        details = json.loads(payload)
        if not isinstance(details, dict):
            return empty_activity_details(activity_type)
        return normalize_activity_details(details, activity_type)
    except (OSError, ValueError, json.JSONDecodeError):
        return empty_activity_details(activity_type)
