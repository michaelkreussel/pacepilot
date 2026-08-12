from dataclasses import dataclass
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import CurrentUser
from app.database import SessionDep
from app.onboarding import require_data_access
from app.repositories.athlete_profile import (
    get_athlete_availability,
    get_athlete_goal,
    get_athlete_profile,
    get_manual_anchors,
    save_athlete_profile,
)
from app.routes.performance_view import planning_view
from app.services.analytics import AthleteDataService
from app.services.planning.athlete_profile import (
    AthleteProfileInput,
    AthleteProfileValidationError,
    AvailabilityInput,
    GoalInput,
    ManualAnchorInput,
    validate_athlete_profile,
)
from app.web import context, templates

router = APIRouter(dependencies=[Depends(require_data_access)])

SPORT_OPTIONS = (
    ("running", "Laufen"),
    ("cycling", "Radfahren"),
    ("walking", "Gehen"),
    ("hiking", "Wandern"),
)
EXPERIENCE_OPTIONS = (
    ("beginner", "Einsteiger"),
    ("intermediate", "Fortgeschritten"),
    ("advanced", "Erfahren"),
)
WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
PERFORMANCE_METRICS = {
    "max_hr": "max_hr",
    "threshold_hr": "threshold_hr",
    "threshold_pace_s_per_km": "threshold_pace",
    "running_threshold_power_watts": "running_threshold_power",
    "cycling_ftp_watts": "cycling_ftp",
}
REFERENCE_METRICS = {
    "reference_5k_seconds": "reference_5k",
    "reference_10k_seconds": "reference_10k",
    "reference_half_seconds": "reference_half",
    "reference_marathon_seconds": "reference_marathon",
}


@dataclass(frozen=True)
class ProfileFormError:
    message: str
    form_data: dict[str, str]


def _format_clock(seconds: float | int | None) -> str:
    if seconds is None:
        return ""
    total = round(seconds)
    hours, remainder = divmod(total, 3_600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{minutes}:{remaining_seconds:02d}"


def _form_data(session: SessionDep, user_id: int) -> dict[str, str]:
    profile = get_athlete_profile(session, user_id)
    goal = get_athlete_goal(session, user_id)
    availability = {
        item.weekday: item.max_duration_minutes
        for item in get_athlete_availability(session, user_id)
    }
    anchors = {(item.sport, item.metric): item for item in get_manual_anchors(session, user_id)}
    primary_sport = profile.primary_sport if profile and profile.primary_sport else "running"
    values = {
        "primary_sport": primary_sport,
        "experience_level": profile.experience_level or "" if profile else "",
        "experience_years": str(profile.experience_years)
        if profile and profile.experience_years is not None
        else "",
        "constraint_note": profile.constraint_note or "" if profile else "",
        "constraint_until": profile.constraint_until.isoformat()
        if profile and profile.constraint_until
        else "",
        "goal_enabled": "1" if goal else "",
        "goal_sport": goal.sport if goal else primary_sport,
        "goal_event_name": goal.event_name or "" if goal else "",
        "goal_target_date": goal.target_date.isoformat() if goal else "",
        "goal_distance_km": f"{goal.distance_m / 1_000:g}"
        if goal and goal.distance_m is not None
        else "",
        "goal_target_time": _format_clock(goal.target_duration_s if goal else None),
        "performance_method": "manual",
        "performance_observed_on": "",
        "reference_observed_on": "",
    }
    for weekday in range(7):
        values[f"available_{weekday}"] = "1" if weekday in availability else ""
        values[f"duration_{weekday}"] = str(availability.get(weekday, ""))

    performance_dates: list[date] = []
    reference_dates: list[date] = []
    for metric, field in PERFORMANCE_METRICS.items():
        sport = "cycling" if metric == "cycling_ftp_watts" else "running"
        anchor = anchors.get((sport, metric))
        if anchor is None:
            values[field] = ""
            continue
        values[field] = (
            _format_clock(anchor.value)
            if metric == "threshold_pace_s_per_km"
            else f"{anchor.value:g}"
        )
        values["performance_method"] = anchor.method
        performance_dates.append(anchor.observed_on)
    for metric, field in REFERENCE_METRICS.items():
        anchor = anchors.get(("running", metric))
        values[field] = _format_clock(anchor.value) if anchor else ""
        if anchor is not None:
            reference_dates.append(anchor.observed_on)
    if performance_dates:
        values["performance_observed_on"] = max(performance_dates).isoformat()
    if reference_dates:
        values["reference_observed_on"] = max(reference_dates).isoformat()
    return values


def _parse_date(value: str, label: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise AthleteProfileValidationError(f"{label} ist ungültig.") from exc


def _parse_int(value: str, label: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise AthleteProfileValidationError(f"{label} muss eine ganze Zahl sein.") from exc


def _parse_number(value: str, label: str) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError as exc:
        raise AthleteProfileValidationError(f"{label} muss eine Zahl sein.") from exc


def _parse_clock(value: str, label: str) -> int | None:
    if not value:
        return None
    try:
        parts = [int(part) for part in value.split(":")]
    except ValueError as exc:
        raise AthleteProfileValidationError(
            f"{label} muss als MM:SS oder HH:MM:SS angegeben werden."
        ) from exc
    if len(parts) == 2:
        minutes, seconds = parts
        hours = 0
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise AthleteProfileValidationError(
            f"{label} muss als MM:SS oder HH:MM:SS angegeben werden."
        )
    if min(hours, minutes, seconds) < 0 or minutes >= 60 and len(parts) == 3 or seconds >= 60:
        raise AthleteProfileValidationError(f"{label} ist ungültig.")
    return hours * 3_600 + minutes * 60 + seconds


async def _parse_profile(request: Request) -> AthleteProfileInput | ProfileFormError:
    form = await request.form()
    fields = (
        "primary_sport",
        "experience_level",
        "experience_years",
        "constraint_note",
        "constraint_until",
        "goal_enabled",
        "goal_sport",
        "goal_event_name",
        "goal_target_date",
        "goal_distance_km",
        "goal_target_time",
        "performance_method",
        "performance_observed_on",
        "reference_observed_on",
        *PERFORMANCE_METRICS.values(),
        *REFERENCE_METRICS.values(),
    )
    form_data = {field: str(form.get(field, "")).strip() for field in fields}
    for weekday in range(7):
        form_data[f"available_{weekday}"] = "1" if form.get(f"available_{weekday}") else ""
        form_data[f"duration_{weekday}"] = str(form.get(f"duration_{weekday}", "")).strip()
    try:
        goal = None
        if form_data["goal_enabled"]:
            goal_date = _parse_date(form_data["goal_target_date"], "Das Zieldatum")
            if goal_date is None:
                raise AthleteProfileValidationError("Bitte gib ein Zieldatum an.")
            distance_km = _parse_number(form_data["goal_distance_km"], "Die Zieldistanz")
            goal = GoalInput(
                sport=form_data["goal_sport"],
                event_name=form_data["goal_event_name"],
                target_date=goal_date,
                distance_m=distance_km * 1_000 if distance_km is not None else None,
                target_duration_s=_parse_clock(form_data["goal_target_time"], "Die Zielzeit"),
            )

        availability = []
        for weekday in range(7):
            if not form_data[f"available_{weekday}"]:
                continue
            duration = _parse_int(
                form_data[f"duration_{weekday}"], f"Die Dauer für {WEEKDAYS[weekday]}"
            )
            if duration is None:
                raise AthleteProfileValidationError(
                    f"Bitte gib die maximale Dauer für {WEEKDAYS[weekday]} an."
                )
            availability.append(AvailabilityInput(weekday, duration))

        anchors = []
        performance_day = _parse_date(
            form_data["performance_observed_on"], "Der Stand der Leistungswerte"
        )
        reference_day = _parse_date(
            form_data["reference_observed_on"], "Das Datum der Referenzzeiten"
        )
        for metric, field in PERFORMANCE_METRICS.items():
            value = (
                _parse_clock(form_data[field], "Die Schwellenpace")
                if metric == "threshold_pace_s_per_km"
                else _parse_number(form_data[field], "Der Leistungswert")
            )
            if value is None:
                continue
            if performance_day is None:
                raise AthleteProfileValidationError("Bitte gib den Stand der Leistungswerte an.")
            anchors.append(
                ManualAnchorInput(
                    sport="cycling" if metric == "cycling_ftp_watts" else "running",
                    metric=metric,
                    value=float(value),
                    observed_on=performance_day,
                    method=form_data["performance_method"] or "manual",
                )
            )
        for metric, field in REFERENCE_METRICS.items():
            value = _parse_clock(form_data[field], "Die Referenzzeit")
            if value is None:
                continue
            if reference_day is None:
                raise AthleteProfileValidationError("Bitte gib das Datum der Referenzzeiten an.")
            anchors.append(
                ManualAnchorInput("running", metric, float(value), reference_day, "race")
            )

        result = AthleteProfileInput(
            primary_sport=form_data["primary_sport"] or None,
            experience_level=form_data["experience_level"] or None,
            experience_years=_parse_int(form_data["experience_years"], "Trainingsjahre"),
            constraint_note=form_data["constraint_note"],
            constraint_until=_parse_date(
                form_data["constraint_until"], "Das Ende der Einschränkung"
            ),
            goal=goal,
            availability=tuple(availability),
            anchors=tuple(anchors),
        )
        validate_athlete_profile(result)
        return result
    except AthleteProfileValidationError as exc:
        return ProfileFormError(str(exc), form_data)


def _render_form(
    request: Request, form_data: dict[str, str], error: str | None = None
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "profile_edit.html",
        context(
            request,
            active_page="profile",
            form_data=form_data,
            sport_options=SPORT_OPTIONS,
            experience_options=EXPERIENCE_OPTIONS,
            weekdays=WEEKDAYS,
            today=date.today(),
            error=error,
        ),
        status_code=422 if error else 200,
    )


@router.get("/performance", response_class=HTMLResponse)
def performance_profile(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    notice: str | None = None,
) -> HTMLResponse:
    planning = AthleteDataService(session, user.id).get_planning_context()
    return templates.TemplateResponse(
        request,
        "performance.html",
        context(
            request,
            active_page="profile",
            planning=planning_view(planning),
            notice=notice,
        ),
    )


@router.get("/performance/edit", response_class=HTMLResponse)
def edit_profile(request: Request, session: SessionDep, user: CurrentUser) -> HTMLResponse:
    return _render_form(request, _form_data(session, user.id))


@router.post("/performance", response_class=HTMLResponse)
async def update_profile(request: Request, session: SessionDep, user: CurrentUser) -> Response:
    result = await _parse_profile(request)
    if isinstance(result, ProfileFormError):
        return _render_form(request, result.form_data, result.message)
    save_athlete_profile(session, user.id, result)
    session.commit()
    return RedirectResponse("/performance?notice=Leistungsprofil+gespeichert", status_code=303)
