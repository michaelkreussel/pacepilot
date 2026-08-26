from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from jinja2.runtime import Context
from markupsafe import Markup, escape

from app.config import coach_feature_enabled, get_settings
from app.http_security import get_csrf_token
from app.onboarding import onboarding_state

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

ACTIVITY_TYPE_LABELS = {
    "running": "Laufen",
    "trail_running": "Trailrunning",
    "treadmill_running": "Laufband",
    "cycling": "Radfahren",
    "strength_training": "Krafttraining",
    "walking": "Gehen",
    "hiking": "Wandern",
}


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "–"
    hours, remainder = divmod(int(seconds), 3600)
    minutes = remainder // 60
    return f"{hours}:{minutes:02d} h" if hours else f"{minutes} min"


def format_precise_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "–"
    hours, remainder = divmod(round(seconds), 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remaining_seconds:02d} h"
    return f"{minutes}:{remaining_seconds:02d} min"


def format_distance(meters: float | int | None) -> str:
    return "–" if meters is None else f"{meters / 1000:.1f} km"


def format_pace(seconds: float | int | None) -> str:
    if seconds is None:
        return "–"
    total = round(seconds)
    minutes, remainder = divmod(total, 60)
    return f"{minutes}:{remainder:02d} min/km"


def format_speed_as_pace(speed_mps: float | int | None) -> str:
    if speed_mps is None or speed_mps <= 0:
        return "–"
    return format_pace(1000 / speed_mps)


def format_activity_type(value: str) -> str:
    return ACTIVITY_TYPE_LABELS.get(value, value.replace("_", " ").title())


def format_date(value: date | datetime | None, include_time: bool = False) -> str:
    if value is None:
        return "–"
    if isinstance(value, datetime) and include_time:
        return value.strftime("%d.%m.%Y, %H:%M")
    return value.strftime("%d.%m.%Y")


templates.env.filters.update(
    duration=format_duration,
    precise_duration=format_precise_duration,
    distance=format_distance,
    pace=format_pace,
    speed_as_pace=format_speed_as_pace,
    activity_type=format_activity_type,
    date=format_date,
    datetime=lambda value: format_date(value, include_time=True),
)


@pass_context
def csrf_field(template_context: Context) -> Markup:
    token = template_context.get("csrf_token", "")
    return Markup(
        f'<input type="hidden" name="_csrf_token" value="{escape(str(token))}">'  # noqa: S704
    )


cast(dict[str, Any], templates.env.globals)["csrf_field"] = csrf_field


def context(request: Request, **values: Any) -> dict[str, Any]:
    current_user = getattr(request.state, "current_user", None)
    settings = get_settings()
    user_id = current_user.id if current_user is not None else 0
    return {
        "request": request,
        "current_user": current_user,
        "onboarding": onboarding_state(current_user) if current_user is not None else None,
        "csrf_token": get_csrf_token(request),
        "features": {
            "coach_workout_proposals": coach_feature_enabled(
                settings.coach_workout_proposals_enabled, user_id
            ),
            "coach_garmin_sync": coach_feature_enabled(settings.coach_garmin_sync_enabled, user_id),
            "coach_daily_adaptation": coach_feature_enabled(
                settings.coach_daily_adaptation_enabled, user_id
            ),
            "coach_plan_generation": coach_feature_enabled(
                settings.coach_plan_generation_enabled, user_id
            ),
            "coach_planner_history_gates": settings.coach_planner_history_gates_enabled,
            "coach_deferred_quality_templates": (
                settings.environment == "development"
                and settings.coach_deferred_quality_templates_enabled
            ),
        },
        **values,
    }
