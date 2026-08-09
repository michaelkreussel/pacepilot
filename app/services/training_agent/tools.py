import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from app.services.analytics.coach_data import CoachDataService
from app.services.analytics.health_trends import MetricTrend

HEALTH_METRICS = {
    "resting_hr": "resting_hr",
    "hrv": "hrv",
    "sleep_duration": "sleep_duration",
    "sleep_need": "sleep_need",
    "sleep_score": "sleep_score",
    "stress": "stress",
    "body_battery_high": "body_battery_high",
    "body_battery_charged": "body_battery_charged",
    "garmin_training_readiness": "garmin_training_readiness",
    "recovery_time": "recovery_time",
    "vo2max": "vo2max",
    "training_load": "training_load",
    "acute_load": "acute_load",
    "chronic_load": "chronic_load",
}
DEFAULT_HEALTH_METRICS = (
    "resting_hr",
    "hrv",
    "sleep_duration",
    "sleep_score",
    "stress",
    "body_battery_high",
)


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _result(data: dict[str, object], progress_summary: str) -> str:
    return json.dumps(
        {"progress_summary": progress_summary, **data},
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )


def _error(message: str) -> str:
    return _result({"error": message}, message)


def _trend_payload(trend: MetricTrend) -> tuple[dict[str, object], dict[str, object]]:
    source_facts = {
        "unit": trend.unit,
        "current": trend.current,
        "current_day": trend.current_day,
        "sample_count": trend.sample_count,
        "measurements": [asdict(point) for point in trend.points],
    }
    calculated = {
        "average_7d": trend.average_7d,
        "average_28d": trend.average_28d,
        "personal_baseline": trend.personal_baseline,
        "difference_from_baseline": trend.difference_from_baseline,
        "baseline_sample_count": trend.baseline_sample_count,
    }
    return source_facts, calculated


def _health_summary(trends: list[MetricTrend], days: int) -> str:
    changes = [
        f"{trend.metric}: {trend.difference_from_baseline:+g} {trend.unit} zur Basis"
        for trend in trends
        if trend.difference_from_baseline is not None
    ]
    measured = sum(trend.sample_count for trend in trends)
    summary = f"{len(trends)} Gesundheitsmetriken über {days} Tage geladen ({measured} Werte)."
    if changes:
        summary += f" Auffällig im Vergleich zur persönlichen Basis: {', '.join(changes[:2])}."
    return summary


class CoachTools:
    """Small, allowlisted agent capabilities over the read-only Coach domain service."""

    def __init__(self, data: CoachDataService) -> None:
        self._data = data

    def agent_tools(self) -> tuple[Callable[..., str], ...]:
        return (
            self.get_profile_context,
            self.get_health_and_recovery,
            self.get_training_history,
            self.get_activity_details,
            self.get_planned_workouts,
        )

    def get_profile_context(self) -> str:
        """Return the athlete profile fields that PacePilot currently stores."""
        profile = self._data.get_profile_context()
        return _result(
            {
                "as_of": profile.as_of,
                "source_facts": {
                    "display_name": profile.display_name,
                    "available_fields": profile.available_fields,
                },
                "data_quality": {
                    "note": (
                        "PacePilot currently stores no goals, injuries, availability, thresholds, "
                        "age, sex, height, or weight in the athlete profile."
                    )
                },
            },
            (
                "Das verfügbare Athletenprofil wurde geprüft; derzeit ist nur der "
                "Anzeigename hinterlegt."
            ),
        )

    def get_health_and_recovery(self, metrics: list[str] | None = None, days: int = 28) -> str:
        """Return recovery plus selected health metrics, personal baselines, and sync coverage.

        Args:
            metrics: Metric names to retrieve. Valid names are resting_hr, hrv, sleep_duration,
                sleep_need, sleep_score, stress, body_battery_high, body_battery_charged,
                garmin_training_readiness, recovery_time, vo2max, training_load, acute_load,
                and chronic_load.
            days: Inclusive trend window from 1 through 365 days.
        """
        if not 1 <= days <= 365:
            return _error("Der Zeitraum für Gesundheitsdaten muss zwischen 1 und 365 Tagen liegen.")
        selected = tuple(dict.fromkeys(metrics or DEFAULT_HEALTH_METRICS))
        unknown = [metric for metric in selected if metric not in HEALTH_METRICS]
        if unknown:
            return _error(f"Unbekannte Gesundheitsmetrik: {', '.join(unknown)}")
        if len(selected) > 8:
            return _error("Bitte höchstens acht Gesundheitsmetriken pro Abfrage auswählen.")

        recovery = asdict(self._data.get_recovery_state())
        pacepilot_readiness = {
            key.removeprefix("pacepilot_readiness_"): recovery.pop(key)
            for key in tuple(recovery)
            if key.startswith("pacepilot_readiness_")
        }
        trends = self._data.get_health_trends(days)
        trend_values = [getattr(trends, HEALTH_METRICS[name]) for name in selected]
        source_facts: dict[str, object] = {}
        calculations: dict[str, object] = {"pacepilot_readiness": pacepilot_readiness}
        for name, trend in zip(selected, trend_values, strict=True):
            source, calculated = _trend_payload(trend)
            source_facts[name] = source
            calculations[name] = calculated

        return _result(
            {
                "as_of": self._data.as_of,
                "window": {"start": trends.start, "end": trends.end, "days": days},
                "source_facts": {
                    "current_recovery": recovery,
                    "metrics": source_facts,
                },
                "pacepilot_calculations": calculations,
                "data_quality": {"coverage": [asdict(item) for item in trends.coverage]},
            },
            _health_summary(trend_values, days),
        )

    def get_training_history(self, days: int = 28, recent_workouts: int = 10) -> str:
        """Return training totals, a chronological trend, recent workouts, and sync coverage.

        Use a longer period such as 84 days to judge whether recent load is unusual for
        this athlete.

        Args:
            days: Inclusive history window from 7 through 365 days.
            recent_workouts: Number of recent workout summaries from 1 through 20.
        """
        if not 7 <= days <= 365:
            return _error("Der Trainingszeitraum muss zwischen 7 und 365 Tagen liegen.")
        if not 1 <= recent_workouts <= 20:
            return _error("Die Zahl der letzten Einheiten muss zwischen 1 und 20 liegen.")
        summary = self._data.get_training_summary(days)
        timeline = self._data.get_training_timeline(days, bucket_days=7)
        workouts = self._data.get_recent_workouts(recent_workouts)
        progress = (
            f"{days} Trainingstage geprüft: {summary.workouts} Einheiten, "
            f"{round((summary.total_duration_s or 0) / 3600, 1)} Stunden und "
            f"{summary.hard_workouts} harte Einheiten."
        )
        return _result(
            {
                "as_of": self._data.as_of,
                "window": {"start": summary.start, "end": summary.end, "days": days},
                "source_facts": {"recent_workouts": [asdict(item) for item in workouts]},
                "pacepilot_calculations": {
                    "summary": asdict(summary),
                    "timeline": [asdict(item) for item in timeline],
                },
                "data_quality": {
                    "status": summary.data_status,
                    "history_complete": summary.history_complete,
                    "oldest_synced_date": summary.oldest_synced_date,
                    "newest_synced_date": summary.newest_synced_date,
                },
            },
            progress,
        )

    def get_activity_details(self, activity_id: int) -> str:
        """Return bounded normalized details for one activity ID from training history.

        Args:
            activity_id: PacePilot activity ID obtained from get_training_history.
        """
        details = self._data.get_activity_details(activity_id)
        if details is None:
            return _error(
                "Die Aktivität wurde nicht gefunden oder gehört nicht zu diesem Athleten."
            )
        payload = asdict(details)
        split_count = len(payload["splits"])
        set_count = len(payload["exercise_sets"])
        payload["splits"] = payload["splits"][:100]
        payload["exercise_sets"] = payload["exercise_sets"][:100]
        return _result(
            {
                "as_of": self._data.as_of,
                "source_facts": payload,
                "data_quality": {
                    "details_complete": details.details_complete,
                    "splits_complete": details.splits_complete,
                    "splits_truncated": split_count > 100,
                    "exercise_sets_truncated": set_count > 100,
                },
            },
            f"Die Einheit „{details.workout.name}“ wurde im Detail geprüft.",
        )

    def get_planned_workouts(self, days: int = 14) -> str:
        """Return scheduled local workouts and their validation status; never modify them.

        Args:
            days: Forward-looking calendar window from 1 through 84 days.
        """
        if not 1 <= days <= 84:
            return _error("Der Planungszeitraum muss zwischen 1 und 84 Tagen liegen.")
        workouts = self._data.get_planned_workouts(days)
        return _result(
            {
                "as_of": self._data.as_of,
                "window_days": days,
                "source_facts": {"planned_workouts": [asdict(item) for item in workouts]},
                "data_quality": {
                    "status_note": (
                        "draft is unconfirmed; confirmed, published, and pushed are "
                        "validated states"
                    )
                },
            },
            (
                f"Der Trainingskalender für die nächsten {days} Tage enthält "
                f"{len(workouts)} Einheiten."
            ),
        )


def tool_result_summary(raw_result: str | None) -> str:
    if not raw_result:
        return "Die Abfrage ist abgeschlossen."
    try:
        parsed: Any = json.loads(raw_result)
    except (TypeError, json.JSONDecodeError):
        return "Die Abfrage ist abgeschlossen."
    if isinstance(parsed, dict) and isinstance(parsed.get("progress_summary"), str):
        return parsed["progress_summary"]
    return "Die Abfrage ist abgeschlossen."
