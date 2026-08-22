import json
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Never
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from app.auth import CurrentUser
from app.database import SessionDep
from app.models import Workout
from app.onboarding import require_planning_access
from app.services.garmin.client import (
    GarminUnavailableError,
    connect_garmin_account,
    message_from_exception,
)
from app.services.planning.validator import WorkoutInput, WorkoutValidationError, validate_workout
from app.services.planning.workout_definition import (
    default_definition,
    definition_to_json,
    parse_definition,
)
from app.services.planning.workout_service import (
    WorkoutNotFoundError,
    WorkoutService,
    WorkoutTransitionError,
)
from app.web import context, templates

router = APIRouter(prefix="/workouts", dependencies=[Depends(require_planning_access)])


def _parse_optional_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise WorkoutValidationError(
            "Das Trainingsdatum ist ungültig.", code="workout.date_invalid"
        ) from exc


@dataclass(frozen=True)
class WorkoutFormError:
    code: str
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
        return WorkoutFormError(
            "definition.json_invalid",
            "Die Workout-Struktur ist ungültig.",
            form_data,
            fallback,
        )
    except ValidationError:
        return WorkoutFormError(
            "definition.schema_invalid",
            "Die Workout-Struktur ist unvollständig.",
            form_data,
            fallback,
        )
    except WorkoutValidationError as exc:
        definition_data = definition_to_json(definition) if "definition" in locals() else fallback
        return WorkoutFormError(exc.code, str(exc), form_data, definition_data)


WorkoutFormDep = Annotated[WorkoutInput | WorkoutFormError, Depends(_parse_workout_result)]


def _workout_service(session: SessionDep, user: CurrentUser) -> WorkoutService:
    # Passing the connector keeps the route test seam while orchestration remains in the service.
    return WorkoutService(session, user, connect_garmin=connect_garmin_account)


WorkoutServiceDep = Annotated[WorkoutService, Depends(_workout_service)]


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
def create_workout(request: Request, data: WorkoutFormDep, service: WorkoutServiceDep) -> Response:
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
    workout = service.create(data)
    return RedirectResponse(f"/workouts/{workout.id}", status_code=303)


def _get_workout(service: WorkoutService, workout_id: int) -> Workout:
    try:
        return service.get(workout_id)
    except WorkoutNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _raise_not_found(exc: WorkoutNotFoundError) -> Never:
    raise HTTPException(status_code=404, detail=str(exc)) from exc


def _operation_error_redirect(
    workout_id: int,
    exc: GarminUnavailableError | WorkoutValidationError | WorkoutTransitionError,
) -> RedirectResponse:
    message = str(exc) if isinstance(exc, WorkoutTransitionError) else message_from_exception(exc)
    query = urlencode({"error": message})
    return RedirectResponse(f"/workouts/{workout_id}?{query}", status_code=303)


@router.get("/{workout_id}", response_class=HTMLResponse)
def workout_detail(
    workout_id: int,
    request: Request,
    service: WorkoutServiceDep,
    error: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "workouts/detail.html",
        context(
            request,
            active_page="plans",
            workout=_get_workout(service, workout_id),
            error=error,
        ),
    )


@router.get("/{workout_id}/edit", response_class=HTMLResponse)
def edit_workout(workout_id: int, request: Request, service: WorkoutServiceDep) -> HTMLResponse:
    workout = _get_workout(service, workout_id)
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
    service: WorkoutServiceDep,
) -> Response:
    workout = _get_workout(service, workout_id)
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

    try:
        service.update(workout_id, data)
    except (GarminUnavailableError, WorkoutValidationError) as exc:
        return _operation_error_redirect(workout_id, exc)
    return RedirectResponse(f"/workouts/{workout.id}", status_code=303)


@router.post("/{workout_id}/delete", response_class=RedirectResponse, status_code=303)
def delete_workout(workout_id: int, service: WorkoutServiceDep) -> RedirectResponse:
    try:
        service.delete(workout_id)
    except WorkoutNotFoundError as exc:
        _raise_not_found(exc)
    except (GarminUnavailableError, WorkoutValidationError) as exc:
        return _operation_error_redirect(workout_id, exc)
    return RedirectResponse("/plans", status_code=303)


@router.post("/{workout_id}/confirm", response_class=RedirectResponse, status_code=303)
def confirm_workout(workout_id: int, service: WorkoutServiceDep) -> RedirectResponse:
    try:
        service.confirm(workout_id)
    except WorkoutNotFoundError as exc:
        _raise_not_found(exc)
    except WorkoutValidationError as exc:
        query = urlencode({"error": str(exc)})
        return RedirectResponse(f"/workouts/{workout_id}?{query}", status_code=303)
    return RedirectResponse(f"/workouts/{workout_id}", status_code=303)


@router.post("/{workout_id}/publish", response_class=RedirectResponse, status_code=303)
def publish(workout_id: int, service: WorkoutServiceDep) -> RedirectResponse:
    try:
        service.publish(workout_id)
    except WorkoutNotFoundError as exc:
        _raise_not_found(exc)
    except (GarminUnavailableError, WorkoutValidationError, WorkoutTransitionError) as exc:
        return _operation_error_redirect(workout_id, exc)
    return RedirectResponse(f"/workouts/{workout_id}", status_code=303)


@router.post("/{workout_id}/push", response_class=RedirectResponse, status_code=303)
def push(workout_id: int, service: WorkoutServiceDep) -> RedirectResponse:
    try:
        service.push(workout_id)
    except WorkoutNotFoundError as exc:
        _raise_not_found(exc)
    except (GarminUnavailableError, WorkoutValidationError, WorkoutTransitionError) as exc:
        return _operation_error_redirect(workout_id, exc)
    return RedirectResponse(f"/workouts/{workout_id}", status_code=303)
