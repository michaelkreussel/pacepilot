import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.auth import CurrentUser
from app.database import SessionDep
from app.models import User, Workout
from app.repositories.users import get_or_create_garmin_account
from app.repositories.workouts import find_workout
from app.services.garmin.client import (
    GarminUnavailableError,
    connect_garmin_account,
    message_from_exception,
)
from app.services.garmin.locks import GarminAccountBusyError, garmin_account_slot
from app.services.garmin.workout_export import (
    delete_published_workout,
    push_workout,
    schedule_published_workout,
    update_published_workout,
    upload_workout,
)
from app.services.planning.validator import WorkoutInput, WorkoutValidationError, validate_workout
from app.services.planning.workout_definition import (
    default_definition,
    definition_to_json,
    parse_definition,
)
from app.web import context, templates

router = APIRouter(prefix="/workouts")


def _parse_optional_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise WorkoutValidationError("Das Trainingsdatum ist ungültig.") from exc


@dataclass(frozen=True)
class WorkoutFormError:
    message: str
    form_data: dict[str, str]
    definition: dict[str, object]


def _form_data(workout: Workout | None = None) -> dict[str, str]:
    return {
        "name": workout.name if workout else "",
        "sport": workout.sport if workout else "running",
        "scheduled_for": (
            workout.scheduled_for.isoformat() if workout and workout.scheduled_for else ""
        ),
        "description": workout.description or "" if workout else "",
    }


async def _parse_workout_result(request: Request) -> WorkoutInput | WorkoutFormError:
    form = await request.form()
    form_data = {
        "name": str(form.get("name", "")).strip(),
        "sport": str(form.get("sport", "running")),
        "scheduled_for": str(form.get("scheduled_for", "")),
        "description": str(form.get("description", "")).strip(),
    }
    fallback = definition_to_json(default_definition())
    try:
        raw_definition = json.loads(str(form.get("definition", "")))
        definition = parse_definition(raw_definition)
        workout = WorkoutInput(
            name=form_data["name"],
            sport=form_data["sport"],
            scheduled_for=_parse_optional_date(form_data["scheduled_for"]),
            description=form_data["description"],
            definition=definition,
        )
        validate_workout(workout)
        return workout
    except json.JSONDecodeError:
        return WorkoutFormError("Die Workout-Struktur ist ungültig.", form_data, fallback)
    except ValidationError:
        return WorkoutFormError("Die Workout-Struktur ist unvollständig.", form_data, fallback)
    except WorkoutValidationError as exc:
        definition_data = definition_to_json(definition) if "definition" in locals() else fallback
        return WorkoutFormError(str(exc), form_data, definition_data)


WorkoutFormDep = Annotated[WorkoutInput | WorkoutFormError, Depends(_parse_workout_result)]


@router.get("/new", response_class=HTMLResponse)
def new_workout(request: Request, _: CurrentUser) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "workouts/form.html",
        context(
            request,
            active_page="workout-new",
            today=date.today(),
            workout=None,
            form_data=_form_data(),
            initial_definition=definition_to_json(default_definition()),
            error=None,
        ),
    )


@router.post("", response_class=HTMLResponse)
def create_workout(
    request: Request, data: WorkoutFormDep, session: SessionDep, user: CurrentUser
) -> Response:
    if isinstance(data, WorkoutFormError):
        return templates.TemplateResponse(
            request,
            "workouts/form.html",
            context(
                request,
                active_page="workout-new",
                today=date.today(),
                workout=None,
                form_data=data.form_data,
                initial_definition=data.definition,
                error=data.message,
            ),
            status_code=422,
        )
    workout = Workout(
        user_id=user.id,
        name=data.name,
        sport=data.sport,
        scheduled_for=data.scheduled_for,
        description=data.description or None,
        status="draft",
        definition_version=1,
        definition=definition_to_json(data.definition),
    )
    session.add(workout)
    session.commit()
    return RedirectResponse(f"/workouts/{workout.id}", status_code=303)


def _get_workout(session: Session, user_id: int, workout_id: int) -> Workout:
    workout = find_workout(session, user_id, workout_id)
    if workout is None:
        raise HTTPException(status_code=404, detail="Workout nicht gefunden")
    return workout


def _validate_persisted_workout(workout: Workout) -> None:
    validate_workout(
        WorkoutInput(
            name=workout.name,
            sport=workout.sport,
            scheduled_for=workout.scheduled_for,
            description=workout.description or "",
            definition=workout.definition_model,
        )
    )


@contextmanager
def _garmin_client(session: Session, user: User) -> Iterator[object]:
    account = get_or_create_garmin_account(session, user)
    if account.connected_at is None:
        raise GarminUnavailableError("Garmin ist noch nicht verbunden.")
    try:
        with garmin_account_slot(account.id):
            yield connect_garmin_account(session, account)
    except GarminAccountBusyError as exc:
        raise GarminUnavailableError(
            "Für dieses Garmin-Konto läuft gerade eine andere Operation."
        ) from exc


@router.get("/{workout_id}", response_class=HTMLResponse)
def workout_detail(
    workout_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    error: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "workouts/detail.html",
        context(
            request,
            active_page="plans",
            workout=_get_workout(session, user.id, workout_id),
            error=error,
        ),
    )


@router.get("/{workout_id}/edit", response_class=HTMLResponse)
def edit_workout(
    workout_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> HTMLResponse:
    workout = _get_workout(session, user.id, workout_id)
    return templates.TemplateResponse(
        request,
        "workouts/form.html",
        context(
            request,
            active_page="plans",
            today=date.today(),
            workout=workout,
            form_data=_form_data(workout),
            initial_definition=workout.definition,
            error=None,
        ),
    )


@router.post("/{workout_id}", response_class=HTMLResponse)
def update_workout(
    workout_id: int,
    request: Request,
    data: WorkoutFormDep,
    session: SessionDep,
    user: CurrentUser,
) -> Response:
    workout = _get_workout(session, user.id, workout_id)
    if isinstance(data, WorkoutFormError):
        return templates.TemplateResponse(
            request,
            "workouts/form.html",
            context(
                request,
                active_page="plans",
                today=date.today(),
                workout=workout,
                form_data=data.form_data,
                initial_definition=data.definition,
                error=data.message,
            ),
            status_code=422,
        )

    previous_date = workout.scheduled_for
    workout.name = data.name
    workout.sport = data.sport
    workout.scheduled_for = data.scheduled_for
    workout.description = data.description or None
    workout.definition_version = 1
    workout.definition = definition_to_json(data.definition)
    try:
        if workout.garmin_workout_id:
            with _garmin_client(session, user) as client:
                update_published_workout(client, workout, previous_date)
            workout.status = "pushed"
        session.commit()
    except (GarminUnavailableError, WorkoutValidationError) as exc:
        session.rollback()
        query = urlencode({"error": message_from_exception(exc)})
        return RedirectResponse(f"/workouts/{workout.id}?{query}", status_code=303)
    return RedirectResponse(f"/workouts/{workout.id}", status_code=303)


@router.post("/{workout_id}/delete", response_class=RedirectResponse, status_code=303)
def delete_workout(workout_id: int, session: SessionDep, user: CurrentUser) -> RedirectResponse:
    workout = _get_workout(session, user.id, workout_id)
    try:
        if workout.garmin_workout_id:
            with _garmin_client(session, user) as client:
                delete_published_workout(client, workout)
        session.delete(workout)
        session.commit()
    except (GarminUnavailableError, WorkoutValidationError) as exc:
        session.rollback()
        query = urlencode({"error": message_from_exception(exc)})
        return RedirectResponse(f"/workouts/{workout.id}?{query}", status_code=303)
    return RedirectResponse("/plans", status_code=303)


@router.post("/{workout_id}/confirm", response_class=RedirectResponse, status_code=303)
def confirm_workout(workout_id: int, session: SessionDep, user: CurrentUser) -> RedirectResponse:
    workout = _get_workout(session, user.id, workout_id)
    if workout.status == "draft":
        try:
            _validate_persisted_workout(workout)
            workout.status = "confirmed"
            session.commit()
        except WorkoutValidationError as exc:
            query = urlencode({"error": str(exc)})
            return RedirectResponse(f"/workouts/{workout.id}?{query}", status_code=303)
    return RedirectResponse(f"/workouts/{workout.id}", status_code=303)


@router.post("/{workout_id}/publish", response_class=RedirectResponse, status_code=303)
def publish(workout_id: int, session: SessionDep, user: CurrentUser) -> RedirectResponse:
    workout = _get_workout(session, user.id, workout_id)
    if workout.status not in {"confirmed", "published", "pushed"}:
        error = "Bitte den Entwurf vor der Übertragung bestätigen."
        return RedirectResponse(
            f"/workouts/{workout.id}?{urlencode({'error': error})}", status_code=303
        )
    try:
        with _garmin_client(session, user) as client:
            if not workout.garmin_workout_id:
                workout.garmin_workout_id = upload_workout(client, workout)
                workout.status = "published"
                session.commit()
            schedule_published_workout(client, workout)
            workout.status = "published"
            session.commit()
    except (GarminUnavailableError, WorkoutValidationError) as exc:
        session.rollback()
        query = urlencode({"error": message_from_exception(exc)})
        return RedirectResponse(f"/workouts/{workout.id}?{query}", status_code=303)
    return RedirectResponse(f"/workouts/{workout.id}", status_code=303)


@router.post("/{workout_id}/push", response_class=RedirectResponse, status_code=303)
def push(workout_id: int, session: SessionDep, user: CurrentUser) -> RedirectResponse:
    workout = _get_workout(session, user.id, workout_id)
    if workout.status not in {"published", "pushed"}:
        error = "Das Workout muss vor der Übertragung veröffentlicht werden."
        return RedirectResponse(
            f"/workouts/{workout.id}?{urlencode({'error': error})}", status_code=303
        )
    try:
        with _garmin_client(session, user) as client:
            push_workout(client, workout)
        workout.status = "pushed"
        session.commit()
    except (GarminUnavailableError, WorkoutValidationError) as exc:
        session.rollback()
        query = urlencode({"error": message_from_exception(exc)})
        return RedirectResponse(f"/workouts/{workout.id}?{query}", status_code=303)
    return RedirectResponse(f"/workouts/{workout.id}", status_code=303)
