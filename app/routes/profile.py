from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.auth import CurrentUser
from app.database import SessionDep
from app.onboarding import require_data_access
from app.services.analytics import AthleteDataService
from app.services.analytics.fitness_trends import GarminFitnessAnalytics
from app.services.analytics.health_trends import MetricTrend, preferred_readiness
from app.services.analytics.training_trends import TrainingTimelinePoint
from app.web import (
    context,
    format_activity_type,
    format_duration,
    format_precise_duration,
    format_speed_as_pace,
    templates,
)

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
    "POOR": "Niedrig",
    "FAIR": "Solide",
    "MODERATE": "Solide",
    "GOOD": "Gut",
    "HIGH": "Hoch",
    "PRIME": "Sehr hoch",
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
READINESS_LIMITER_LABELS = {
    "sleep_duration": "Die sehr kurze Schlafdauer begrenzt die heutige Einschätzung.",
    "garmin_sleep_score": "Der niedrige Garmin-Schlafwert begrenzt die heutige Einschätzung.",
    "recent_sleep_debt": (
        "Das Schlafdefizit der letzten drei Nächte begrenzt die heutige Einschätzung."
    ),
}
CARD_DETAIL_SLUGS = {
    "resting_hr": "resting-hr",
    "hrv": "hrv",
    "sleep": "sleep",
    "body_battery": "recovery",
    "stress": "recovery",
    "garmin_readiness": "garmin-readiness",
    "vo2max": "vo2max",
    "training_load": "training-load",
}
METRIC_DETAILS: dict[str, dict[str, str]] = {
    "resting-hr": {
        "chart_id": "resting-hr-chart",
        "group": "Gesundheit",
        "title": "Ruhepuls",
        "description": (
            "Zeigt, wie sich dein Ruhepuls relativ zu deiner persönlichen Basis entwickelt."
        ),
        "source": "health",
    },
    "hrv": {
        "chart_id": "hrv-chart",
        "group": "Erholung",
        "title": "HRV",
        "description": (
            "Hilft, Veränderungen deiner nächtlichen Erholung im persönlichen Kontext zu erkennen."
        ),
        "source": "health",
    },
    "sleep": {
        "chart_id": "sleep-chart",
        "group": "Erholung",
        "title": "Schlafdauer",
        "description": "Vergleicht deine Schlafdauer mit dem von Garmin ermittelten Schlafbedarf.",
        "source": "health",
    },
    "sleep-score": {
        "chart_id": "sleep-score-chart",
        "group": "Erholung",
        "title": "Schlafwert",
        "description": (
            "Ordnet die von Garmin bewertete Schlafqualität über den gewählten Zeitraum ein."
        ),
        "source": "health",
    },
    "recovery": {
        "chart_id": "recovery-chart",
        "group": "Erholung",
        "title": "Stress & Body Battery",
        "description": "Stellt tägliche Belastung und verfügbare Energiereserven gemeinsam dar.",
        "source": "health",
    },
    "vo2max": {
        "chart_id": "vo2max-chart",
        "group": "Leistung",
        "title": "VO2max",
        "description": (
            "Zeigt die längerfristige Entwicklung deiner geschätzten aeroben Leistungsfähigkeit."
        ),
        "source": "health",
    },
    "garmin-readiness": {
        "chart_id": "garmin-readiness-chart",
        "group": "Garmin",
        "title": "Garmin Trainingsbereitschaft",
        "description": (
            "Zeigt den von Garmin gelieferten Bereitschaftswert. Dieser ist nicht der "
            "PacePilot-Score."
        ),
        "source": "health",
    },
    "training-load": {
        "chart_id": "garmin-training-load-chart",
        "group": "Belastung",
        "title": "Garmin Trainingslast",
        "description": (
            "Vergleicht deine Garmin Trainingslast mit akuter und chronischer Belastung."
        ),
        "source": "health",
    },
    "running-volume": {
        "chart_id": "running-volume-chart",
        "group": "Training",
        "title": "Laufumfang",
        "description": "Zeigt deinen Laufumfang je Abschnitt und als rollierende 28-Tage-Summe.",
        "source": "training",
    },
    "training-duration": {
        "chart_id": "training-duration-chart",
        "group": "Training",
        "title": "Trainingsdauer",
        "description": (
            "Zeigt, wie sich deine gesamte Trainingszeit im gewählten Zeitraum verteilt."
        ),
        "source": "training",
    },
    "workout-frequency": {
        "chart_id": "workout-frequency-chart",
        "group": "Training",
        "title": "Workout-Häufigkeit",
        "description": "Macht sichtbar, wie regelmäßig du trainiert hast.",
        "source": "training",
    },
    "training-effect": {
        "chart_id": "training-effect-chart",
        "group": "Belastung",
        "title": "Training Effect",
        "description": (
            "Zeigt den durchschnittlichen aeroben und anaeroben Trainingsreiz deiner Einheiten."
        ),
        "source": "training",
    },
    "exercise-load": {
        "chart_id": "exercise-load-chart",
        "group": "Belastung",
        "title": "Exercise Load",
        "description": (
            "Summiert die von Garmin ermittelte Belastung deiner Aktivitäten je Abschnitt."
        ),
        "source": "training",
    },
    "zones": {
        "chart_id": "zone-chart",
        "group": "Intensität",
        "title": "Trainingszonen",
        "description": (
            "Zeigt die verfügbare Zeitverteilung über Herzfrequenz- oder Leistungszonen."
        ),
        "source": "training",
    },
    "intensity": {
        "chart_id": "intensity-chart",
        "group": "Intensität",
        "title": "Intensitätsminuten",
        "description": "Vergleicht moderate und intensive Minuten aus deinen Aktivitäten.",
        "source": "training",
    },
    "threshold-pace": {
        "chart_id": "threshold-pace-chart",
        "group": "Leistung & Schwellen",
        "title": "Schwellenpace",
        "description": "Zeigt Garmins aktuell geschätzte Laufpace an der Laktatschwelle.",
        "source": "fitness",
        "fitness_keys": "threshold_speed",
    },
    "threshold-heart-rate": {
        "chart_id": "threshold-heart-rate-chart",
        "group": "Leistung & Schwellen",
        "title": "Schwellen-Herzfrequenz",
        "description": "Zeigt Garmins Herzfrequenzschätzung an der Laktatschwelle.",
        "source": "fitness",
        "fitness_keys": "threshold_hr",
    },
    "running-ftp": {
        "chart_id": "running-ftp-chart",
        "group": "Leistung & Schwellen",
        "title": "Running FTP",
        "description": (
            "Zeigt die von Garmin ermittelte funktionelle Schwellenleistung beim Laufen."
        ),
        "source": "fitness",
        "fitness_keys": "running_ftp",
    },
    "cycling-ftp": {
        "chart_id": "cycling-ftp-chart",
        "group": "Leistung & Schwellen",
        "title": "Cycling FTP",
        "description": (
            "Zeigt die von Garmin ermittelte funktionelle Schwellenleistung beim Radfahren."
        ),
        "source": "fitness",
        "fitness_keys": "cycling_ftp",
    },
    "race-predictions": {
        "chart_id": "race-predictions-chart",
        "group": "Leistung & Schwellen",
        "title": "Garmin Rennprognosen",
        "description": (
            "Zeigt Garmins geschätzte Zielzeiten auf vier standardisierten Laufdistanzen."
        ),
        "source": "fitness",
        "fitness_keys": "race_5k,race_10k,race_half,race_marathon",
    },
    "endurance-score": {
        "chart_id": "endurance-score-chart",
        "group": "Leistung & Schwellen",
        "title": "Garmin Ausdauerwert",
        "description": "Zeigt den geräteabhängigen Garmin Endurance Score ohne eigene Bewertung.",
        "source": "fitness",
        "fitness_keys": "endurance_score",
    },
    "hill-score": {
        "chart_id": "hill-score-chart",
        "group": "Leistung & Schwellen",
        "title": "Garmin Anstiegswert",
        "description": "Zeigt den geräteabhängigen Garmin Hill Score ohne eigene Bewertung.",
        "source": "fitness",
        "fitness_keys": "hill_score",
    },
    "fitness-age": {
        "chart_id": "fitness-age-chart",
        "group": "Leistung & Schwellen",
        "title": "Garmin Fitnessalter",
        "description": "Zeigt Garmins geschätztes Fitnessalter als geräteabhängigen Leistungswert.",
        "source": "fitness",
        "fitness_keys": "fitness_age",
    },
}
CHART_DETAIL_SLUGS = {details["chart_id"]: slug for slug, details in METRIC_DETAILS.items()}
TIMELINE_CHART_IDS = {
    "running-volume-chart",
    "training-duration-chart",
    "workout-frequency-chart",
    "training-effect-chart",
    "exercise-load-chart",
}
FITNESS_PRESENTATION = {
    "threshold_speed": ("Schwellenpace", "threshold-pace"),
    "threshold_hr": ("Schwellen-HF", "threshold-heart-rate"),
    "running_ftp": ("Running FTP", "running-ftp"),
    "cycling_ftp": ("Cycling FTP", "cycling-ftp"),
    "race_5k": ("5-km-Prognose", "race-predictions"),
    "race_10k": ("10-km-Prognose", "race-predictions"),
    "race_half": ("Halbmarathon-Prognose", "race-predictions"),
    "race_marathon": ("Marathon-Prognose", "race-predictions"),
    "endurance_score": ("Garmin Ausdauerwert", "endurance-score"),
    "hill_score": ("Garmin Anstiegswert", "hill-score"),
    "fitness_age": ("Garmin Fitnessalter", "fitness-age"),
}
FITNESS_DATASET_LABELS = {
    "threshold_speed": "Schwellenpace",
    "threshold_hr": "Schwellen-HF",
    "running_ftp": "Running FTP",
    "cycling_ftp": "Cycling FTP",
    "race_5k": "5 km",
    "race_10k": "10 km",
    "race_half": "Halbmarathon",
    "race_marathon": "Marathon",
    "endurance_score": "Ausdauerwert",
    "hill_score": "Anstiegswert",
    "fitness_age": "Fitnessalter",
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
    if not any(
        any(value is not None for value in dataset["data"])
        for dataset in datasets
        if dataset["label"] != "Persönliche Basis"
    ):
        return None
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
    links: list[str] = []
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
    available = [chart for chart in charts if chart is not None]
    for chart in available:
        slug = CHART_DETAIL_SLUGS[chart["id"]]
        chart["links"] = [f"/profile/{slug}?period=day&end={day.isoformat()}" for day in dates]
    return available


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


def _intensity_chart(summary: Any) -> dict[str, Any] | None:
    if summary.moderate_intensity_minutes is None and summary.vigorous_intensity_minutes is None:
        return None
    return {
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
                    summary.moderate_intensity_minutes,
                    summary.vigorous_intensity_minutes,
                ],
                "colors": ["#1d5a48", "#6757a8"],
            }
        ],
    }


def _format_fitness_value(key: str, value: float) -> str:
    if key == "threshold_speed":
        return format_speed_as_pace(value)
    if key.startswith("race_"):
        return format_precise_duration(value)
    if key in {"threshold_hr", "running_ftp", "cycling_ftp"}:
        unit = "bpm" if key == "threshold_hr" else "W"
        return f"{round(value)} {unit}"
    if key == "fitness_age":
        return f"{_display_number(value, 1)} Jahre"
    return _display_number(value, 0)


def _performance_cards(
    fitness: GarminFitnessAnalytics, period: Period, end: date
) -> list[dict[str, Any]]:
    return [
        {
            "key": metric.key,
            "label": FITNESS_PRESENTATION[metric.key][0],
            "value": _format_fitness_value(metric.key, metric.latest.value),
            "source_day": metric.latest.day,
            "detail_url": (
                f"/profile/{FITNESS_PRESENTATION[metric.key][1]}?period={period}"
                f"&end={end.isoformat()}"
            ),
        }
        for metric in fitness.metrics
    ]


def _fitness_chart(
    fitness: GarminFitnessAnalytics, details: dict[str, str]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    keys = details["fitness_keys"].split(",")
    metrics = [metric for key in keys if (metric := fitness.get(key)) is not None]
    current_values = [
        {
            "label": FITNESS_DATASET_LABELS[metric.key],
            "value": _format_fitness_value(metric.key, metric.latest.value),
            "source_day": metric.latest.day,
        }
        for metric in metrics
    ]
    days = sorted({point.day for metric in metrics for point in metric.points})
    if not days:
        return None, current_values

    colors = ("#1d5a48", "#ff5c35", "#6757a8", "#e24b3b")
    datasets = []
    for index, metric in enumerate(metrics):
        values = {point.day: point.value for point in metric.points}
        datasets.append(
            _line_dataset(
                FITNESS_DATASET_LABELS[metric.key],
                [
                    (1000 / values[day] if metric.key == "threshold_speed" else values[day])
                    if day in values
                    else None
                    for day in days
                ],
                colors[index % len(colors)],
            )
        )

    first_key = metrics[0].key
    value_format = (
        "pace"
        if first_key == "threshold_speed"
        else "duration"
        if first_key.startswith("race_")
        else "number"
    )
    unit = (
        "min/km"
        if value_format == "pace"
        else ""
        if value_format == "duration"
        else metrics[0].unit
    )
    return (
        {
            "id": details["chart_id"],
            "kicker": "Garmin-Leistungswert",
            "title": details["title"],
            "unit": unit,
            "type": "line",
            "labels": [day.strftime("%d.%m.") for day in days],
            "links": [],
            "datasets": datasets,
            "value_format": value_format,
            "decimals": 1 if first_key == "fitness_age" else 0,
            "reverse_y": value_format == "pace",
            "summary": [],
            "summary_title": "",
            "drilldown_url": None,
        },
        current_values,
    )


def _latest_chart_value(chart: dict[str, Any], dataset: dict[str, Any]) -> dict[str, Any] | None:
    index = next(
        (
            index
            for index in range(len(dataset["data"]) - 1, -1, -1)
            if dataset["data"][index] is not None
        ),
        None,
    )
    if index is None:
        return None
    return {
        "label": dataset["label"],
        "value": dataset["data"][index],
        "unit": chart["unit"],
        "context": "Referenzwert" if dataset.get("dashed") else f"Stand {chart['labels'][index]}",
    }


def _decorate_chart(chart: dict[str, Any], training: Any | None = None) -> None:
    if training is None:
        chart["summary_title"] = "Letzte verfügbare Werte"
        chart["summary"] = [
            item
            for dataset in chart["datasets"]
            if (item := _latest_chart_value(chart, dataset)) is not None
        ]
        return

    period_context = (
        "Gewählter Zeitraum" if training.history_complete else "Bisher erfasster Zeitraum"
    )
    chart_id = chart["id"]
    items: list[dict[str, Any]] = []
    chart["summary_title"] = "Zeitraum zusammengefasst"

    if chart_id == "running-volume-chart":
        if training.running_distance_m is not None:
            items.append(
                {
                    "label": "Laufumfang gesamt",
                    "value": round(training.running_distance_m / 1000, 2),
                    "unit": "km",
                    "context": period_context,
                }
            )
        rolling = next(
            (dataset for dataset in chart["datasets"] if dataset["label"] == "Rollierende 28 Tage"),
            None,
        )
        if rolling is not None and (latest := _latest_chart_value(chart, rolling)) is not None:
            items.append(latest)
    elif chart_id == "training-duration-chart" and training.total_duration_s is not None:
        items.append(
            {
                "label": "Trainingszeit gesamt",
                "value": round(training.total_duration_s / 3600, 2),
                "unit": "h",
                "context": period_context,
            }
        )
    elif chart_id == "workout-frequency-chart":
        items.extend(
            [
                {
                    "label": "Einheiten gesamt",
                    "value": training.workouts,
                    "unit": "",
                    "context": period_context,
                },
                {
                    "label": "Durchschnitt pro Woche",
                    "value": training.training_frequency_per_week,
                    "unit": "Einheiten",
                    "context": "Über den gewählten Zeitraum",
                },
            ]
        )
    elif chart_id == "training-effect-chart":
        chart["summary_title"] = "Durchschnitt im Zeitraum"
        for label, value in (
            ("Aerober Training Effect", training.average_aerobic_training_effect),
            ("Anaerober Training Effect", training.average_anaerobic_training_effect),
        ):
            if value is not None:
                items.append(
                    {
                        "label": label,
                        "value": value,
                        "unit": "/ 5",
                        "context": "Ø aller Einheiten mit Messwert",
                    }
                )
    elif chart_id == "exercise-load-chart" and training.exercise_load is not None:
        items.append(
            {
                "label": "Exercise Load gesamt",
                "value": training.exercise_load,
                "unit": "Load",
                "context": period_context,
            }
        )
    elif chart_id == "zone-chart":
        values = [value for value in chart["datasets"][0]["data"] if value is not None]
        if values:
            items.append(
                {
                    "label": "Erfasste Zonenzeit",
                    "value": round(sum(values), 1),
                    "unit": "min",
                    "context": period_context,
                }
            )
    elif chart_id == "intensity-chart":
        items = [
            {
                "label": f"{label} erfasst",
                "value": value,
                "unit": "min",
                "context": period_context,
            }
            for label, value in zip(chart["labels"], chart["datasets"][0]["data"], strict=True)
            if value is not None
        ]

    chart["summary"] = items


def _period_dates(period: Period, end: date | None) -> tuple[date, date, int]:
    today = date.today()
    end_date = end or today
    if end_date > today:
        raise HTTPException(status_code=400, detail="Das Enddatum darf nicht in der Zukunft liegen")
    if end_date < date(2, 1, 1):
        raise HTTPException(
            status_code=400, detail="Das Enddatum liegt außerhalb des gültigen Bereichs"
        )
    days = next(days for key, _, days in PERIODS if key == period)
    return end_date - timedelta(days=days - 1), end_date, days


def _health_notices(health: Any) -> list[str]:
    notices: list[str] = []
    not_synced = [item for item in health.coverage if item.status == "not_synced"]
    if len(not_synced) == len(health.coverage):
        return ["Der Gesundheitsverlauf wurde noch nicht synchronisiert."]
    for item in health.coverage:
        label = RESOURCE_LABELS[item.resource]
        if item.status == "unsupported":
            notices.append(f"{label} wird von diesem Garmin-Konto nicht unterstützt.")
        elif item.status in {"error", "authentication_failure", "rate_limited"}:
            notices.append(f"{label}: Synchronisierung derzeit unvollständig.")
        elif not item.backfill_complete and item.status not in {"empty", "not_synced"}:
            notices.append(f"{label}: Historie ist noch nicht vollständig.")
    return notices


@router.get("", response_class=HTMLResponse)
def profile(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    period: Period = "month",
    end: date | None = None,
) -> HTMLResponse:
    start, end_date, days = _period_dates(period, end)
    analytics = AthleteDataService(session, user.id, as_of=end_date)
    recovery = analytics.get_current_recovery_state()
    readiness = preferred_readiness(recovery)
    health = analytics.get_health_trends(days)
    training = analytics.get_training_summary(days)
    fitness = analytics.get_garmin_fitness_metrics(days)

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
    for card in cards:
        slug = CARD_DETAIL_SLUGS.get(card["key"])
        card["detail_url"] = (
            f"/profile/{slug}?period={period}&end={end_date.isoformat()}" if slug else None
        )

    training_complete = training.history_complete and training.data_status in {"ok", "empty"}
    training_show_data = training.workouts > 0 or training_complete
    health_notices = _health_notices(health)
    return templates.TemplateResponse(
        request,
        "profile.html",
        context(
            request,
            active_page="profile",
            user=user,
            period=period,
            period_options=[
                {"key": key, "label": label, "days": option_days}
                for key, label, option_days in PERIODS
            ],
            period_label=next(label for key, label, _ in PERIODS if key == period),
            is_today=end_date == date.today(),
            start=start,
            end=end_date,
            recovery=recovery,
            readiness=readiness,
            readiness_label=(
                GARMIN_STATUS_LABELS.get(readiness.label or "", readiness.label)
                if readiness and readiness.source == "garmin"
                else READINESS_LABELS.get(readiness.label or "")
                if readiness
                else None
            ),
            readiness_limiter=(
                READINESS_LIMITER_LABELS.get(recovery.pacepilot_readiness_limiter or "")
                if readiness and readiness.source == "pacepilot"
                else None
            ),
            readiness_components=readiness_components,
            metric_cards=cards,
            performance_cards=_performance_cards(fitness, period, end_date),
            health_notices=health_notices,
            training=training,
            training_show_data=training_show_data,
        ),
    )


@router.get("/{metric_slug}", response_class=HTMLResponse)
def profile_metric(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    metric_slug: str,
    period: Period = "month",
    end: date | None = None,
) -> HTMLResponse:
    metric = METRIC_DETAILS.get(metric_slug)
    if metric is None:
        raise HTTPException(status_code=404, detail="Analyse nicht gefunden")

    start, end_date, days = _period_dates(period, end)
    analytics = AthleteDataService(session, user.id, as_of=end_date)
    chart: dict[str, Any] | None = None
    training: Any | None = None
    current_values: list[dict[str, Any]] = []
    notices: list[str] = []

    if metric["source"] == "health":
        health = analytics.get_health_trends(days)
        chart = next(
            (item for item in _health_charts(health) if item["id"] == metric["chart_id"]),
            None,
        )
        notices = _health_notices(health)
    elif metric["source"] == "training":
        training = analytics.get_training_summary(days)
        if metric["chart_id"] in TIMELINE_CHART_IDS:
            timeline = analytics.get_training_timeline(days, bucket_days=1 if days <= 7 else 7)
            chart = next(
                (
                    item
                    for item in _training_charts(training, timeline)
                    if item["id"] == metric["chart_id"]
                ),
                None,
            )
        elif metric["chart_id"] == "zone-chart":
            chart = _zone_chart(training)
        elif metric["chart_id"] == "intensity-chart":
            chart = _intensity_chart(training)
        if not training.history_complete:
            notices.append(
                "Die Trainingshistorie ist noch nicht vollständig; Lücken werden nicht als null "
                "gewertet."
            )
    else:
        fitness = analytics.get_garmin_fitness_metrics(days)
        chart, current_values = _fitness_chart(fitness, metric)

    if chart is not None and metric["source"] != "fitness":
        _decorate_chart(chart, training)
        chart["drilldown_url"] = (
            f"/activities?from={start.isoformat()}&to={end_date.isoformat()}"
            if training is not None
            else None
        )

    return templates.TemplateResponse(
        request,
        "profile_detail.html",
        context(
            request,
            active_page="profile",
            period=period,
            period_options=[
                {"key": key, "label": label, "days": option_days}
                for key, label, option_days in PERIODS
            ],
            period_label=next(label for key, label, _ in PERIODS if key == period),
            start=start,
            end=end_date,
            metric_slug=metric_slug,
            metric=metric,
            chart=chart,
            current_values=current_values,
            notices=notices,
            chart_data={"charts": [chart] if chart is not None else []},
        ),
    )
