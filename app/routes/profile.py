from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.auth import CurrentUser
from app.database import SessionDep
from app.onboarding import require_data_access
from app.repositories.users import get_or_create_garmin_account
from app.services.analytics import AthleteDataService
from app.services.analytics.health_trends import MetricTrend
from app.services.analytics.training_trends import RecentWorkout, TrainingTimelinePoint
from app.web import context, format_activity_type, format_distance, format_duration, templates

router = APIRouter(prefix="/profile", dependencies=[Depends(require_data_access)])

Period = Literal["day", "week", "month", "3m", "year"]
PERIODS: tuple[tuple[Period, str, int], ...] = (
    ("day", "Tag", 1),
    ("week", "7 Tage", 7),
    ("month", "28 Tage", 28),
    ("3m", "12 Wochen", 84),
    ("year", "Jahr", 365),
)

READINESS_LABELS = {"low": "Niedrig", "fair": "Solide", "good": "Gut", "high": "Hoch"}
GARMIN_STATUS_LABELS = {
    "PRODUCTIVE": "Produktiv",
    "MAINTAINING": "Form erhaltend",
    "RECOVERY": "Erholung",
    "PEAKING": "Höchstform",
    "STRAINED": "Überlastet",
    "UNPRODUCTIVE": "Unproduktiv",
    "BALANCED": "Ausgeglichen",
    "LOW": "Niedrig",
    "HIGH": "Hoch",
}
RESOURCE_LABELS = {
    "daily_summary": "Tageswerte",
    "sleep": "Schlaf",
    "hrv": "HRV",
    "body_battery": "Body Battery",
    "training_readiness": "Garmin Trainingsbereitschaft",
    "training_status": "Garmin Trainingsstatus",
    "vo2max": "VO2max",
}
COMPONENT_LABELS = {
    "sleep_duration": "Schlafdauer",
    "garmin_sleep_score": "Garmin Schlafwert",
    "hrv": "HRV",
    "resting_hr": "Ruhepuls",
    "garmin_stress": "Garmin Stress",
    "garmin_body_battery_high": "Body Battery",
    "recent_hard_training_recovery": "Erholung nach Belastung",
}


def _display_number(value: float, decimals: int = 0) -> str:
    return f"{value:.{decimals}f}".replace(".", ",")


def _sport_label(sport: str) -> str:
    return format_activity_type(sport)


def _fresh(source_day: date | None, as_of: date, max_age_days: int) -> bool:
    return source_day is not None and 0 <= (as_of - source_day).days <= max_age_days


def _difference(
    current: float | int | None,
    baseline: float | None,
    unit: str,
    *,
    lower_is_better: bool = False,
) -> tuple[str | None, str]:
    if current is None or baseline is None:
        return None, "neutral"
    delta = float(current) - baseline
    if abs(delta) < 0.05:
        return "Auf persönlicher Basis", "neutral"
    direction = "über" if delta > 0 else "unter"
    favorable = delta < 0 if lower_is_better else delta > 0
    text = f"{_display_number(abs(delta), 1)} {unit} {direction} Basis"
    return text, "positive" if favorable else "caution"


def _metric_card(
    key: str,
    label: str,
    value: str,
    source_day: date | None,
    *,
    note: str | None = None,
    tone: str = "neutral",
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "value": value,
        "source_day": source_day,
        "note": note,
        "tone": tone,
    }


def _full_values(
    trend: MetricTrend, start: date, end: date, factor: float = 1
) -> list[float | None]:
    values = {point.day: round(point.value / factor, 2) for point in trend.points}
    return [values.get(start + timedelta(days=offset)) for offset in range((end - start).days + 1)]


def _health_chart(
    chart_id: str,
    kicker: str,
    title: str,
    unit: str,
    labels: list[str],
    links: list[str],
    datasets: list[dict[str, Any]],
) -> dict[str, Any] | None:
    datasets = [
        dataset for dataset in datasets if any(value is not None for value in dataset["data"])
    ]
    if not datasets:
        return None
    return {
        "id": chart_id,
        "kicker": kicker,
        "title": title,
        "unit": unit,
        "type": "line",
        "span_gaps": True,
        "labels": labels,
        "links": links,
        "datasets": datasets,
    }


def _line_dataset(
    label: str,
    data: list[float | None],
    color: str,
    *,
    fill: bool = False,
    dashed: bool = False,
    axis: str = "y",
) -> dict[str, Any]:
    return {
        "label": label,
        "data": data,
        "color": color,
        "fill": fill,
        "dashed": dashed,
        "axis": axis,
    }


def _health_charts(health: Any) -> list[dict[str, Any]]:
    days = (health.end - health.start).days + 1
    dates = [health.start + timedelta(days=offset) for offset in range(days)]
    labels = [day.strftime("%d.%m.") for day in dates]
    links = [f"/profile?period=day&end={day.isoformat()}#gesundheit" for day in dates]
    charts = [
        _health_chart(
            "resting-hr-chart",
            "Herz-Kreislauf",
            "Ruhepuls",
            "bpm",
            labels,
            links,
            [
                _line_dataset(
                    "Ruhepuls",
                    _full_values(health.resting_hr, health.start, health.end),
                    "#e24b3b",
                    fill=True,
                ),
                _line_dataset(
                    "Persönliche Basis",
                    [health.resting_hr.personal_baseline] * days,
                    "#68756f",
                    dashed=True,
                ),
            ],
        ),
        _health_chart(
            "hrv-chart",
            "Erholung",
            "HRV",
            "ms",
            labels,
            links,
            [
                _line_dataset(
                    "HRV",
                    _full_values(health.hrv, health.start, health.end),
                    "#ff5c35",
                    fill=True,
                ),
                _line_dataset(
                    "Persönliche Basis",
                    [health.hrv.personal_baseline] * days,
                    "#68756f",
                    dashed=True,
                ),
            ],
        ),
        _health_chart(
            "sleep-chart",
            "Schlaf",
            "Dauer und Bedarf",
            "h",
            labels,
            links,
            [
                _line_dataset(
                    "Schlaf",
                    _full_values(health.sleep_duration, health.start, health.end, 3600),
                    "#6757a8",
                    fill=True,
                ),
                _line_dataset(
                    "Schlafbedarf",
                    _full_values(health.sleep_need, health.start, health.end, 3600),
                    "#ff5c35",
                    dashed=True,
                ),
            ],
        ),
        _health_chart(
            "sleep-score-chart",
            "Garmin",
            "Schlafwert",
            "Punkte",
            labels,
            links,
            [
                _line_dataset(
                    "Schlafwert",
                    _full_values(health.sleep_score, health.start, health.end),
                    "#6757a8",
                    fill=True,
                )
            ],
        ),
        _health_chart(
            "recovery-chart",
            "Tagesverlauf",
            "Stress und Body Battery",
            "Punkte",
            labels,
            links,
            [
                _line_dataset(
                    "Stress",
                    _full_values(health.stress, health.start, health.end),
                    "#e24b3b",
                ),
                _line_dataset(
                    "Body Battery hoch",
                    _full_values(health.body_battery_high, health.start, health.end),
                    "#1d5a48",
                ),
            ],
        ),
        _health_chart(
            "vo2max-chart",
            "Leistungsentwicklung",
            "VO2max",
            "ml/kg/min",
            labels,
            links,
            [
                _line_dataset(
                    "VO2max",
                    _full_values(health.vo2max, health.start, health.end),
                    "#1d5a48",
                    fill=True,
                )
            ],
        ),
        _health_chart(
            "garmin-readiness-chart",
            "Garmin",
            "Trainingsbereitschaft",
            "Punkte",
            labels,
            links,
            [
                _line_dataset(
                    "Garmin Trainingsbereitschaft",
                    _full_values(health.garmin_training_readiness, health.start, health.end),
                    "#1d5a48",
                    fill=True,
                )
            ],
        ),
        _health_chart(
            "garmin-training-load-chart",
            "Garmin",
            "Trainingslast",
            "Load",
            labels,
            links,
            [
                _line_dataset(
                    "Trainingslast",
                    _full_values(health.training_load, health.start, health.end),
                    "#6757a8",
                ),
                _line_dataset(
                    "Akut",
                    _full_values(health.acute_load, health.start, health.end),
                    "#ff5c35",
                ),
                _line_dataset(
                    "Chronisch",
                    _full_values(health.chronic_load, health.start, health.end),
                    "#1d5a48",
                ),
            ],
        ),
    ]
    return [chart for chart in charts if chart is not None]


def _timeline_labels(points: tuple[TrainingTimelinePoint, ...]) -> list[str]:
    return [
        point.start.strftime("%d.%m.")
        if point.start == point.end
        else f"{point.start:%d.%m.}–{point.end:%d.%m.}"
        for point in points
    ]


def _training_charts(
    summary: Any, points: tuple[TrainingTimelinePoint, ...]
) -> list[dict[str, Any]]:
    labels = _timeline_labels(points)
    links = [
        f"/activities?from={point.start.isoformat()}&to={point.end.isoformat()}" for point in points
    ]
    charts: list[dict[str, Any]] = []

    def add_chart(
        chart_id: str,
        kicker: str,
        title: str,
        unit: str,
        chart_type: str,
        datasets: list[dict[str, Any]],
    ) -> None:
        datasets = [
            dataset for dataset in datasets if any(value is not None for value in dataset["data"])
        ]
        if not datasets:
            return
        charts.append(
            {
                "id": chart_id,
                "kicker": kicker,
                "title": title,
                "unit": unit,
                "type": chart_type,
                "labels": labels,
                "links": links,
                "datasets": datasets,
            }
        )

    if summary.running_distance_m not in (None, 0):
        add_chart(
            "running-volume-chart",
            "Laufen",
            "Laufumfang",
            "km",
            "bar",
            [
                {
                    **_line_dataset(
                        "Umfang",
                        [
                            None
                            if point.running_distance_m is None
                            else round(point.running_distance_m / 1000, 2)
                            for point in points
                        ],
                        "#ff5c35",
                    ),
                    "type": "bar",
                },
                {
                    **_line_dataset(
                        "Rollierende 28 Tage",
                        [
                            None
                            if point.rolling_28d_running_distance_m is None
                            else round(point.rolling_28d_running_distance_m / 1000, 2)
                            for point in points
                        ],
                        "#1d5a48",
                    ),
                    "type": "line",
                },
            ],
        )
    if summary.total_duration_s not in (None, 0):
        add_chart(
            "training-duration-chart",
            "Gesamtvolumen",
            "Trainingsdauer",
            "h",
            "bar",
            [
                {
                    **_line_dataset(
                        "Dauer",
                        [
                            None if point.duration_s is None else round(point.duration_s / 3600, 2)
                            for point in points
                        ],
                        "#1d5a48",
                    ),
                    "type": "bar",
                }
            ],
        )
    if summary.workouts:
        add_chart(
            "workout-frequency-chart",
            "Routine",
            "Workout-Häufigkeit",
            "Einheiten",
            "bar",
            [
                {
                    **_line_dataset("Einheiten", [point.workouts for point in points], "#d8f36a"),
                    "type": "bar",
                }
            ],
        )
    if (
        summary.average_aerobic_training_effect is not None
        or summary.average_anaerobic_training_effect is not None
    ):
        add_chart(
            "training-effect-chart",
            "Trainingsreiz",
            "Training Effect",
            "Garmin Skala",
            "line",
            [
                _line_dataset(
                    "Aerob",
                    [point.average_aerobic_training_effect for point in points],
                    "#1d5a48",
                ),
                _line_dataset(
                    "Anaerob",
                    [point.average_anaerobic_training_effect for point in points],
                    "#ff5c35",
                ),
            ],
        )
    if summary.exercise_load is not None:
        add_chart(
            "exercise-load-chart",
            "Garmin",
            "Exercise Load",
            "Load",
            "bar",
            [
                {
                    **_line_dataset(
                        "Exercise Load",
                        [point.exercise_load for point in points],
                        "#6757a8",
                    ),
                    "type": "bar",
                }
            ],
        )
    return charts


def _zone_chart(summary: Any) -> dict[str, Any] | None:
    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for item in summary.zone_distribution:
        groups[(item.sport, item.zone_type)].append(item)
    if not groups:
        return None
    sport, zone_type = min(
        groups,
        key=lambda key: (
            0 if "run" in key[0] and key[1] == "heart_rate" else 1,
            0 if key[1] == "heart_rate" else 1,
            key,
        ),
    )
    zones = sorted(groups[(sport, zone_type)], key=lambda item: item.zone_number)
    return {
        "id": "zone-chart",
        "kicker": _sport_label(sport),
        "title": "Herzfrequenzzonen" if zone_type == "heart_rate" else "Leistungszonen",
        "unit": "min",
        "type": "bar",
        "labels": [f"Zone {item.zone_number}" for item in zones],
        "links": [],
        "datasets": [
            {
                **_line_dataset(
                    "Zeit",
                    [round(item.seconds / 60, 1) for item in zones],
                    "#ff5c35",
                ),
                "type": "bar",
            }
        ],
    }


def _recent_workout(item: RecentWorkout) -> dict[str, Any]:
    hard_reasons = []
    if (item.aerobic_training_effect or 0) >= 3.5:
        hard_reasons.append("aerober TE ≥ 3,5")
    if (item.anaerobic_training_effect or 0) >= 2.5:
        hard_reasons.append("anaerober TE ≥ 2,5")
    if (item.workout_rpe or 0) >= 7:
        hard_reasons.append("RPE ≥ 7")
    has_load_data = any(
        value is not None
        for value in (
            item.aerobic_training_effect,
            item.anaerobic_training_effect,
            item.workout_rpe,
        )
    )
    return {
        "id": item.activity_id,
        "name": item.name,
        "sport": _sport_label(item.sport),
        "started_at": item.started_at,
        "duration": format_duration(item.duration_s),
        "distance": format_distance(item.distance_m) if item.distance_m is not None else None,
        "aerobic_training_effect": (
            _display_number(item.aerobic_training_effect, 1)
            if item.aerobic_training_effect is not None
            else None
        ),
        "anaerobic_training_effect": (
            _display_number(item.anaerobic_training_effect, 1)
            if item.anaerobic_training_effect is not None
            else None
        ),
        "rpe": str(item.workout_rpe) if item.workout_rpe is not None else None,
        "has_load_data": has_load_data,
        "hard": bool(hard_reasons),
        "hard_reasons": hard_reasons,
    }


def _decorate_charts(charts: list[dict[str, Any]]) -> None:
    for chart in charts:
        if chart["type"] == "doughnut":
            chart["summary"] = [
                {"label": label, "value": value, "unit": chart["unit"]}
                for label, value in zip(chart["labels"], chart["datasets"][0]["data"], strict=True)
                if value is not None
            ]
        else:
            summary = []
            for dataset in chart["datasets"]:
                latest = next(
                    (value for value in reversed(dataset["data"]) if value is not None), None
                )
                if latest is not None:
                    summary.append(
                        {"label": dataset["label"], "value": latest, "unit": chart["unit"]}
                    )
            chart["summary"] = summary
        chart["drilldown_url"] = chart["links"][-1] if chart["links"] else None


@router.get("", response_class=HTMLResponse)
def profile(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    period: Period = "month",
    end: date | None = None,
) -> HTMLResponse:
    today = date.today()
    end_date = end or today
    if end_date > today:
        raise HTTPException(status_code=400, detail="Das Enddatum darf nicht in der Zukunft liegen")
    if end_date < date(2, 1, 1):
        raise HTTPException(
            status_code=400, detail="Das Enddatum liegt außerhalb des gültigen Bereichs"
        )
    days = next(days for key, _, days in PERIODS if key == period)
    start = end_date - timedelta(days=days - 1)
    account = get_or_create_garmin_account(session, user)
    analytics = AthleteDataService(session, user.id, as_of=end_date)
    recovery = analytics.get_current_recovery_state()
    health = analytics.get_health_trends(days)
    training = analytics.get_training_summary(days)
    timeline = analytics.get_training_timeline(days, bucket_days=1 if days <= 7 else 7)

    cards: list[dict[str, Any]] = []
    health_is_fresh = _fresh(recovery.health_day, end_date, 2)
    if health_is_fresh and recovery.resting_hr is not None:
        note, tone = _difference(
            recovery.resting_hr,
            health.resting_hr.personal_baseline,
            "bpm",
            lower_is_better=True,
        )
        cards.append(
            _metric_card(
                "resting_hr",
                "Ruhepuls",
                f"{recovery.resting_hr} bpm",
                recovery.health_day,
                note=note,
                tone=tone,
            )
        )
    if health_is_fresh and recovery.hrv_average is not None:
        note, tone = _difference(recovery.hrv_average, health.hrv.personal_baseline, "ms")
        cards.append(
            _metric_card(
                "hrv",
                "HRV",
                f"{_display_number(recovery.hrv_average)} ms",
                recovery.health_day,
                note=note
                or GARMIN_STATUS_LABELS.get(recovery.hrv_status or "", recovery.hrv_status),
                tone=tone,
            )
        )
    if health_is_fresh and recovery.sleep_seconds is not None:
        sleep_note = None
        if recovery.sleep_need_seconds:
            difference = recovery.sleep_seconds - recovery.sleep_need_seconds
            sleep_note = (
                f"{format_duration(abs(difference))} "
                f"{'über' if difference >= 0 else 'unter'} Bedarf"
            )
        cards.append(
            _metric_card(
                "sleep",
                "Schlaf",
                format_duration(recovery.sleep_seconds),
                recovery.health_day,
                note=sleep_note,
                tone="positive" if sleep_note and "über" in sleep_note else "neutral",
            )
        )
    if health_is_fresh and recovery.body_battery_high is not None:
        cards.append(
            _metric_card(
                "body_battery",
                "Body Battery",
                str(recovery.body_battery_high),
                recovery.health_day,
                note=(
                    f"Tagestief {recovery.body_battery_low}"
                    if recovery.body_battery_low is not None
                    else None
                ),
            )
        )
    if health_is_fresh and recovery.stress_average is not None:
        cards.append(
            _metric_card(
                "stress",
                "Stress",
                str(recovery.stress_average),
                recovery.health_day,
                note="Garmin Tagesdurchschnitt",
            )
        )
    if recovery.garmin_training_readiness_score is not None and _fresh(
        recovery.garmin_training_readiness_day, end_date, 7
    ):
        cards.append(
            _metric_card(
                "garmin_readiness",
                "Garmin Bereitschaft",
                str(recovery.garmin_training_readiness_score),
                recovery.garmin_training_readiness_day,
                note=GARMIN_STATUS_LABELS.get(
                    recovery.garmin_training_readiness_level or "",
                    recovery.garmin_training_readiness_level,
                ),
            )
        )
    if recovery.recovery_time_minutes is not None and _fresh(
        recovery.recovery_time_day, end_date, 7
    ):
        cards.append(
            _metric_card(
                "recovery_time",
                "Erholungszeit",
                format_duration(recovery.recovery_time_minutes * 60),
                recovery.recovery_time_day,
            )
        )
    if recovery.vo2max is not None and _fresh(recovery.vo2max_day, end_date, 90):
        cards.append(
            _metric_card(
                "vo2max",
                "VO2max",
                _display_number(recovery.vo2max, 1),
                recovery.vo2max_day,
                note="ml/kg/min",
                tone="neutral",
            )
        )
    if recovery.training_status is not None and _fresh(recovery.training_status_day, end_date, 28):
        cards.append(
            _metric_card(
                "training_status",
                "Garmin Trainingsstatus",
                GARMIN_STATUS_LABELS.get(
                    recovery.training_status,
                    recovery.training_status.replace("_", " ").title(),
                ),
                recovery.training_status_day,
            )
        )
    current_load = (
        recovery.acute_load if recovery.acute_load is not None else recovery.training_load
    )
    current_load_day = (
        recovery.acute_load_day if recovery.acute_load is not None else recovery.training_load_day
    )
    if current_load is not None and _fresh(current_load_day, end_date, 28):
        cards.append(
            _metric_card(
                "training_load",
                "Garmin Akutlast" if recovery.acute_load is not None else "Garmin Trainingslast",
                _display_number(current_load),
                current_load_day,
                note=(
                    f"Verhältnis {_display_number(recovery.load_ratio, 2)}"
                    if recovery.load_ratio is not None
                    else None
                ),
            )
        )

    readiness_components = [
        {
            "label": COMPONENT_LABELS.get(item.component, item.component.replace("_", " ").title()),
            "score": item.score,
            "weight": round(item.normalized_weight * 100),
        }
        for item in recovery.pacepilot_readiness_components
    ]

    training_complete = training.history_complete and training.data_status in {"ok", "empty"}
    training_show_data = training.workouts > 0 or training_complete
    training_stats: list[tuple[str, str]] = []
    if training_show_data:
        training_stats.extend(
            [
                ("Workouts", str(training.workouts)),
                ("Aktive Tage", str(training.active_days)),
            ]
        )
        if training.total_duration_s is not None:
            training_stats.append(("Trainingsdauer", format_duration(training.total_duration_s)))
        if training.running_distance_m not in (None, 0):
            training_stats.append(("Laufumfang", format_distance(training.running_distance_m)))
        if training.cycling_distance_m not in (None, 0):
            training_stats.append(("Radumfang", format_distance(training.cycling_distance_m)))
        if days > 1:
            training_stats.append(
                ("Frequenz", f"{_display_number(training.training_frequency_per_week, 1)} / Woche")
            )
        if training.hard_workouts:
            training_stats.append(("Harte Einheiten", str(training.hard_workouts)))
        if training.exercise_load is not None:
            training_stats.append(("Exercise Load", _display_number(training.exercise_load)))

    training_charts = _training_charts(training, timeline)
    zone_chart = _zone_chart(training)
    if zone_chart is not None:
        training_charts.append(zone_chart)
    if (
        training.moderate_intensity_minutes is not None
        or training.vigorous_intensity_minutes is not None
    ):
        training_charts.append(
            {
                "id": "intensity-chart",
                "kicker": "Intensität",
                "title": "Intensitätsminuten",
                "unit": "min",
                "type": "doughnut",
                "labels": ["Moderat", "Intensiv"],
                "links": [],
                "datasets": [
                    {
                        "label": "Minuten",
                        "data": [
                            training.moderate_intensity_minutes,
                            training.vigorous_intensity_minutes,
                        ],
                        "colors": ["#1d5a48", "#6757a8"],
                    }
                ],
            }
        )

    sport_volumes = [
        {
            "sport": _sport_label(item.sport),
            "workouts": item.workouts,
            "duration": format_duration(item.duration_s),
            "distance": format_distance(item.distance_m) if item.distance_m is not None else None,
            "elevation": (
                f"{_display_number(item.elevation_gain_m)} hm"
                if item.elevation_gain_m is not None
                else None
            ),
        }
        for item in training.volume_per_sport
    ]

    all_health_charts = _health_charts(health)
    garmin_load_charts = [
        chart for chart in all_health_charts if chart["id"] == "garmin-training-load-chart"
    ]
    health_charts = [
        chart for chart in all_health_charts if chart["id"] != "garmin-training-load-chart"
    ]
    training_charts.extend(garmin_load_charts)
    if period == "day":
        health_charts = []
        training_charts = [
            chart for chart in training_charts if chart["id"] in {"intensity-chart", "zone-chart"}
        ]
    _decorate_charts(health_charts)
    _decorate_charts(training_charts)
    chart_data = {"charts": health_charts + training_charts}
    health_notices: list[str] = []
    not_synced = [item for item in health.coverage if item.status == "not_synced"]
    if len(not_synced) == len(health.coverage):
        health_notices.append("Der Gesundheitsverlauf wurde noch nicht synchronisiert.")
    else:
        for item in health.coverage:
            label = RESOURCE_LABELS[item.resource]
            if item.status == "unsupported":
                health_notices.append(f"{label} wird von diesem Garmin-Konto nicht unterstützt.")
            elif item.status in {"error", "authentication_failure", "rate_limited"}:
                health_notices.append(f"{label}: Synchronisierung derzeit unvollständig.")
            elif not item.backfill_complete and item.status not in {"empty", "not_synced"}:
                health_notices.append(f"{label}: Historie ist noch nicht vollständig.")
    return templates.TemplateResponse(
        request,
        "profile.html",
        context(
            request,
            active_page="profile",
            user=user,
            account=account,
            period=period,
            period_options=[
                {"key": key, "label": label, "days": option_days}
                for key, label, option_days in PERIODS
            ],
            period_label=next(label for key, label, _ in PERIODS if key == period),
            is_today=end_date == today,
            start=start,
            end=end_date,
            recovery=recovery,
            readiness_label=READINESS_LABELS.get(recovery.pacepilot_readiness_label or ""),
            readiness_components=readiness_components,
            metric_cards=cards,
            health_notices=health_notices,
            health_charts=health_charts,
            training=training,
            training_show_data=training_show_data,
            training_stats=training_stats,
            training_charts=training_charts,
            sport_volumes=sport_volumes,
            recent_workouts=[
                _recent_workout(item) for item in analytics.get_recent_workouts(limit=6)
            ],
            chart_data=chart_data,
        ),
    )
