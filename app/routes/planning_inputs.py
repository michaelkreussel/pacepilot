from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.auth import CurrentUser
from app.database import SessionDep
from app.models.planning import ANCHOR_KINDS, EXPERIENCE_LEVELS, GOAL_EVENT_TYPES
from app.onboarding import require_planning_access
from app.repositories.users import get_or_create_garmin_account
from app.services.garmin.client import (
    GarminUnavailableError,
    connect_garmin_account,
    message_from_exception,
)
from app.services.garmin.locks import GarminAccountBusyError, garmin_account_slot
from app.services.garmin.personal_records import (
    RECORD_TYPE_LABELS,
    PersonalRecordCandidate,
    fetch_running_records,
)
from app.services.planning.planning_commands import (
    AnchorKind,
    AvailabilityInput,
    GoalCreateInput,
    GoalEventType,
    GoalUpdateInput,
    PerformanceAnchorCreateInput,
    PerformanceAnchorUpdateInput,
    PlanningInputCommandError,
    PlanningInputCommands,
    PlanningProfileUpdateInput,
    ReferencedGoalChangeConfirmation,
)
from app.services.planning.planning_queries import (
    get_planning_profile,
    list_availability,
    list_goals,
    list_performance_anchors,
)
from app.services.planning.workout_views import GOAL_TYPE_LABELS
from app.web import context, templates

router = APIRouter(prefix="/planning-inputs", dependencies=[Depends(require_planning_access)])

ANCHOR_KIND_LABELS = {
    "race": "Wettkampf",
    "time_trial": "Zeitlauf",
    "manual": "Training",
}

EXPERIENCE_LEVEL_LABELS = {
    "novice": "Einsteiger",
    "intermediate": "Fortgeschritten",
    "advanced": "Erfahren",
}

GOAL_STATUS_LABELS = {
    "active": "Aktiv",
    "achieved": "Erreicht",
    "archived": "Archiviert",
}

WEEKDAY_LABELS = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)

RECORD_DURATION_TOLERANCE_S = 5.0

_FAILURE_MESSAGE = "Die Änderung konnte nicht gespeichert werden."


def _redirect(error: str | None = None, notice: str | None = None) -> RedirectResponse:
    params = {}
    if error:
        params["error"] = error
    if notice:
        params["notice"] = notice
    target = "/planning-inputs"
    if params:
        target = f"{target}?{urlencode(params)}"
    return RedirectResponse(target, status_code=303)


def _failure_message(exc: Exception) -> str:
    if isinstance(exc, PlanningInputCommandError):
        return str(exc)
    return _FAILURE_MESSAGE


def _parse_anchor_fields(
    kind: str,
    distance_km: str,
    minutes: str,
    seconds: str,
    achieved_on: str,
) -> tuple[AnchorKind, float, float, date]:
    if kind not in ANCHOR_KINDS:
        raise ValueError("Unbekannte Anker-Art.")
    try:
        distance_m = float(distance_km.replace(",", ".")) * 1000
        duration_s = float(int(minutes) * 60 + int(seconds))
        achieved = date.fromisoformat(achieved_on)
    except (TypeError, ValueError) as exc:
        raise ValueError("Bitte Distanz, Zeit und Datum gültig ausfüllen.") from exc
    if distance_m <= 0 or duration_s <= 0:
        raise ValueError("Distanz und Zeit müssen größer als null sein.")
    return kind, distance_m, duration_s, achieved


@router.get("", response_class=HTMLResponse)
def planning_inputs(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    error: Annotated[str | None, Query(max_length=500)] = None,
    notice: Annotated[str | None, Query(max_length=500)] = None,
) -> HTMLResponse:
    return _render_page(request, session, user, error=error, notice=notice)


def _render_page(
    request: Request,
    session: Session,
    user: CurrentUser,
    *,
    error: str | None = None,
    notice: str | None = None,
    record_candidates: list[PersonalRecordCandidate] | None = None,
    existing_record_types: frozenset[int] = frozenset(),
) -> HTMLResponse:
    availability_by_weekday = {fact.weekday: fact for fact in list_availability(session, user.id)}
    return templates.TemplateResponse(
        request,
        "planning_inputs.html",
        context(
            request,
            active_page="planning-inputs",
            anchors=list_performance_anchors(session, user.id),
            goals=list_goals(session, user.id),
            profile=get_planning_profile(session, user.id),
            availability=[availability_by_weekday.get(weekday) for weekday in range(7)],
            anchor_kinds=ANCHOR_KINDS,
            anchor_kind_labels=ANCHOR_KIND_LABELS,
            goal_event_types=GOAL_EVENT_TYPES,
            goal_type_labels=GOAL_TYPE_LABELS,
            goal_status_labels=GOAL_STATUS_LABELS,
            experience_levels=EXPERIENCE_LEVELS,
            experience_level_labels=EXPERIENCE_LEVEL_LABELS,
            weekday_labels=WEEKDAY_LABELS,
            today=date.today().isoformat(),
            record_candidates=record_candidates,
            record_type_labels=RECORD_TYPE_LABELS,
            existing_record_types=existing_record_types,
            error=error,
            notice=notice,
        ),
    )


@router.post("/anchors")
def create_anchor(
    session: SessionDep,
    user: CurrentUser,
    kind: Annotated[str, Form()],
    distance_km: Annotated[str, Form()],
    minutes: Annotated[str, Form()],
    seconds: Annotated[str, Form()],
    achieved_on: Annotated[str, Form()],
    notes: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        parsed_kind, distance_m, duration_s, achieved = _parse_anchor_fields(
            kind, distance_km, minutes, seconds, achieved_on
        )
        commands = PlanningInputCommands(session, user)
        commands.create_performance_anchor(
            PerformanceAnchorCreateInput(
                kind=parsed_kind,
                distance_m=distance_m,
                duration_s=duration_s,
                achieved_on=achieved,
                notes=notes or None,
            )
        )
    except (ValueError, ValidationError, PlanningInputCommandError) as exc:
        return _redirect(error=_failure_message(exc))
    return _redirect(notice="Leistungsanker gespeichert.")


@router.post("/anchors/{anchor_id}")
def update_anchor(
    session: SessionDep,
    user: CurrentUser,
    anchor_id: int,
    kind: Annotated[str, Form()],
    distance_km: Annotated[str, Form()],
    minutes: Annotated[str, Form()],
    seconds: Annotated[str, Form()],
    achieved_on: Annotated[str, Form()],
    reliable: Annotated[bool, Form()] = False,
    notes: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        parsed_kind, distance_m, duration_s, achieved = _parse_anchor_fields(
            kind, distance_km, minutes, seconds, achieved_on
        )
        commands = PlanningInputCommands(session, user)
        commands.update_performance_anchor(
            anchor_id,
            PerformanceAnchorUpdateInput(
                kind=parsed_kind,
                distance_m=distance_m,
                duration_s=duration_s,
                achieved_on=achieved,
                reliable=reliable,
                notes=notes or None,
            ),
        )
    except (ValueError, ValidationError, PlanningInputCommandError) as exc:
        return _redirect(error=_failure_message(exc))
    return _redirect(notice="Leistungsanker aktualisiert.")


@router.post("/anchors/{anchor_id}/deactivate")
def deactivate_anchor(session: SessionDep, user: CurrentUser, anchor_id: int) -> RedirectResponse:
    try:
        PlanningInputCommands(session, user).deactivate_performance_anchor(anchor_id)
    except PlanningInputCommandError as exc:
        return _redirect(error=_failure_message(exc))
    return _redirect(notice="Leistungsanker deaktiviert.")


@router.post("/anchors/{anchor_id}/delete")
def delete_anchor(
    session: SessionDep,
    user: CurrentUser,
    anchor_id: int,
    confirm: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    if not confirm:
        return _redirect(error="Bitte das Löschen ausdrücklich bestätigen.")
    try:
        PlanningInputCommands(session, user).delete_performance_anchor(anchor_id)
    except PlanningInputCommandError as exc:
        return _redirect(error=_failure_message(exc))
    return _redirect(notice="Leistungsanker gelöscht.")


def _load_record_candidates(session: Session, user: CurrentUser) -> list[PersonalRecordCandidate]:
    account = get_or_create_garmin_account(session, user)
    if account.connected_at is None:
        raise PlanningInputCommandError(
            "Garmin ist nicht verbunden. Bitte verbinde dein Konto zuerst unter Einstellungen.",
            code="planning.garmin_not_connected",
        )
    try:
        with garmin_account_slot(account.id):
            client = connect_garmin_account(session, account)
            return fetch_running_records(client)
    except GarminAccountBusyError as exc:
        raise PlanningInputCommandError(
            "Für dieses Garmin-Konto läuft gerade eine andere Operation.",
            code="planning.garmin_busy",
        ) from exc
    except GarminUnavailableError as exc:
        raise PlanningInputCommandError(
            message_from_exception(exc), code="planning.garmin_unavailable"
        ) from exc


def _existing_record_types(
    session: Session, user_id: int, candidates: list[PersonalRecordCandidate]
) -> frozenset[int]:
    anchors = list_performance_anchors(session, user_id)
    matched: set[int] = set()
    for candidate in candidates:
        for anchor in anchors:
            if (
                abs(anchor.distance_m - candidate.distance_m) <= 1
                and abs(anchor.duration_s - candidate.duration_s) <= RECORD_DURATION_TOLERANCE_S
            ):
                matched.add(candidate.record_type_id)
                break
    return frozenset(matched)


@router.post("/garmin-records", response_class=HTMLResponse, response_model=None)
def load_garmin_records(
    request: Request, session: SessionDep, user: CurrentUser
) -> HTMLResponse | RedirectResponse:
    try:
        candidates = _load_record_candidates(session, user)
    except PlanningInputCommandError as exc:
        return _redirect(error=_failure_message(exc))
    if not candidates:
        return _redirect(error="Keine Lauf-Bestzeiten in Garmin gefunden.")
    return _render_page(
        request,
        session,
        user,
        record_candidates=candidates,
        existing_record_types=_existing_record_types(session, user.id, candidates),
        notice="Garmin-Bestzeiten geladen. Wähle aus, was als Leistungsanker übernommen wird.",
    )


@router.post("/garmin-records/confirm")
async def confirm_garmin_records(
    request: Request, session: SessionDep, user: CurrentUser
) -> RedirectResponse:
    try:
        form = {key: str(value) for key, value in (await request.form()).multi_items()}
        candidates = _load_record_candidates(session, user)
        selected = {
            candidate
            for candidate in candidates
            if form.get(f"record_{candidate.record_type_id}") == "on"
        }
        if not selected:
            return _redirect(error="Bitte mindestens eine Bestzeit auswählen.")
        kinds: dict[int, AnchorKind] = {}
        for candidate in selected:
            kind = form.get(f"kind_{candidate.record_type_id}", "manual")
            if kind not in ANCHOR_KINDS:
                raise ValueError("Unbekannte Anker-Art.")
            kinds[candidate.record_type_id] = kind
        commands = PlanningInputCommands(session, user)
        existing = _existing_record_types(session, user.id, list(selected))
        created = 0
        skipped = 0
        for candidate in sorted(selected, key=lambda item: item.distance_m):
            if candidate.record_type_id in existing:
                skipped += 1
                continue
            notes = "Garmin-Bestzeit"
            if candidate.activity_name:
                notes = f"Garmin-Bestzeit · {candidate.activity_name}"
            commands.create_performance_anchor(
                PerformanceAnchorCreateInput(
                    kind=kinds[candidate.record_type_id],
                    distance_m=candidate.distance_m,
                    duration_s=candidate.duration_s,
                    achieved_on=candidate.achieved_on,
                    notes=notes,
                )
            )
            created += 1
    except (ValueError, ValidationError, PlanningInputCommandError) as exc:
        return _redirect(error=_failure_message(exc))
    parts = []
    if created:
        parts.append(
            f"{created} Leistungsanker übernommen" if created > 1 else "1 Leistungsanker übernommen"
        )
    if skipped:
        parts.append(f"{skipped} bereits vorhanden" if skipped > 1 else "1 bereits vorhanden")
    return _redirect(notice=f"{', '.join(parts)}." if parts else "Nichts übernommen.")


def _goal_confirmation(
    commands: PlanningInputCommands, goal_id: int, operation: Literal["update", "deactivate"]
) -> ReferencedGoalChangeConfirmation | None:
    expected = commands.referenced_goal_change_confirmation(
        goal_id,
        operation=operation,
    )
    if expected is None:
        return None
    return ReferencedGoalChangeConfirmation(
        goal_id=goal_id,
        operation=operation,
        accepted_cycles=list(expected.accepted_cycles),
    )


def _goal_event_type(event_type: str) -> GoalEventType:
    if event_type not in GOAL_EVENT_TYPES:
        raise ValueError("Unbekannte Ziel-Distanz.")
    return event_type


@router.post("/goals")
def create_goal(
    session: SessionDep,
    user: CurrentUser,
    event_type: Annotated[str, Form()],
    event_name: Annotated[str, Form()] = "",
    target_date: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        commands = PlanningInputCommands(session, user)
        commands.create_goal(
            GoalCreateInput(
                event_type=_goal_event_type(event_type),
                event_name=event_name or None,
                target_date=date.fromisoformat(target_date) if target_date else None,
            )
        )
    except (ValueError, ValidationError, PlanningInputCommandError) as exc:
        return _redirect(error=_failure_message(exc))
    return _redirect(notice="Ziel gespeichert.")


@router.post("/goals/{goal_id}")
def update_goal(
    session: SessionDep,
    user: CurrentUser,
    goal_id: int,
    event_type: Annotated[str, Form()],
    event_name: Annotated[str, Form()] = "",
    target_date: Annotated[str, Form()] = "",
    confirm: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    try:
        commands = PlanningInputCommands(session, user)
        data = GoalUpdateInput(
            event_type=_goal_event_type(event_type),
            event_name=event_name or None,
            target_date=date.fromisoformat(target_date) if target_date else None,
        )
        try:
            commands.update_goal(goal_id, data)
        except PlanningInputCommandError as exc:
            if exc.code != "planning.goal_confirmation_required" or not confirm:
                raise
            commands.update_goal(
                goal_id, data, confirmation=_goal_confirmation(commands, goal_id, "update")
            )
    except (ValueError, ValidationError, PlanningInputCommandError) as exc:
        return _redirect(error=_failure_message(exc))
    return _redirect(notice="Ziel aktualisiert.")


@router.post("/goals/{goal_id}/deactivate")
def deactivate_goal(
    session: SessionDep,
    user: CurrentUser,
    goal_id: int,
    confirm: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    try:
        commands = PlanningInputCommands(session, user)
        try:
            commands.deactivate_goal(goal_id)
        except PlanningInputCommandError as exc:
            if exc.code != "planning.goal_confirmation_required" or not confirm:
                raise
            commands.deactivate_goal(
                goal_id,
                confirmation=_goal_confirmation(commands, goal_id, "deactivate"),
            )
    except PlanningInputCommandError as exc:
        return _redirect(error=_failure_message(exc))
    return _redirect(notice="Ziel archiviert.")


@router.post("/goals/{goal_id}/delete")
def delete_goal(
    session: SessionDep,
    user: CurrentUser,
    goal_id: int,
    confirm: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    if not confirm:
        return _redirect(error="Bitte das Löschen ausdrücklich bestätigen.")
    try:
        PlanningInputCommands(session, user).delete_goal(goal_id)
    except PlanningInputCommandError as exc:
        return _redirect(error=_failure_message(exc))
    return _redirect(notice="Ziel gelöscht.")


@router.post("/profile")
def update_profile(
    session: SessionDep,
    user: CurrentUser,
    experience_level: Annotated[str, Form()] = "",
    preferred_long_run_weekday: Annotated[str, Form()] = "",
    self_declared_reentry: Annotated[bool, Form()] = False,
    constraint_note: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        commands = PlanningInputCommands(session, user)
        experience = experience_level or None
        if experience is not None and experience not in EXPERIENCE_LEVELS:
            raise ValueError("Unbekannte Erfahrung.")
        commands.update_profile(
            PlanningProfileUpdateInput(
                experience_level=experience,
                preferred_long_run_weekday=(
                    int(preferred_long_run_weekday) if preferred_long_run_weekday else None
                ),
                self_declared_reentry=self_declared_reentry,
                constraint_note=constraint_note or None,
            )
        )
    except (ValueError, ValidationError, PlanningInputCommandError) as exc:
        return _redirect(error=_failure_message(exc))
    return _redirect(notice="Trainingsprofil gespeichert.")


@router.post("/availability")
async def update_availability(
    request: Request, session: SessionDep, user: CurrentUser
) -> RedirectResponse:
    try:
        form = {key: str(value) for key, value in (await request.form()).multi_items()}
        commands = PlanningInputCommands(session, user)
        current = {fact.weekday: fact for fact in list_availability(session, user.id)}
        for weekday in range(7):
            available = form.get(f"available_{weekday}") == "on"
            minutes_raw = (form.get(f"minutes_{weekday}") or "").strip()
            if available:
                try:
                    minutes = int(minutes_raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Bitte für {WEEKDAY_LABELS[weekday]} gültige Minuten angeben."
                    ) from exc
                commands.set_availability(
                    AvailabilityInput(weekday=weekday, available=True, available_minutes=minutes)
                )
            else:
                existing = current.get(weekday)
                if existing is not None and (
                    existing.available or existing.available_minutes is not None
                ):
                    commands.deactivate_availability(weekday=weekday)
    except (ValueError, ValidationError, PlanningInputCommandError) as exc:
        return _redirect(error=_failure_message(exc))
    return _redirect(notice="Verfügbarkeiten gespeichert.")
