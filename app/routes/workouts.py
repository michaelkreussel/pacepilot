from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.database import SessionDep
from app.models import Workout, WorkoutStep
from app.repositories.users import get_or_create_default_user
from app.repositories.workouts import find_workout
from app.services.garmin.client import message_from_exception
from app.services.garmin.workout_export import publish_workout, push_workout
from app.services.planning.validator import (
    StepInput,
    WorkoutInput,
    WorkoutValidationError,
    validate_workout,
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


async def _parse_workout(request: Request) -> WorkoutInput:
    form = await request.form()
    step_types = form.getlist("step_type")
    duration_types = form.getlist("duration_type")
    values = form.getlist("duration_value")
    repeats = form.getlist("repeat_count")
    steps: list[StepInput] = []
    for index, step_type in enumerate(step_types):
        duration_type = str(duration_types[index]) if index < len(duration_types) else "time"
        raw_value = str(values[index]) if index < len(values) else ""
        raw_repeat = str(repeats[index]) if index < len(repeats) else "1"
        try:
            value = float(raw_value) if raw_value else None
            repeat_count = int(raw_repeat or 1)
        except ValueError as exc:
            raise WorkoutValidationError("Dauer und Wiederholungen müssen Zahlen sein.") from exc
        if duration_type == "time" and value is not None:
            value *= 60
        steps.append(StepInput(str(step_type), duration_type, value, repeat_count))
    workout = WorkoutInput(
        name=str(form.get("name", "")).strip(),
        sport=str(form.get("sport", "running")),
        scheduled_for=_parse_optional_date(str(form.get("scheduled_for", ""))),
        description=str(form.get("description", "")).strip(),
        steps=steps,
    )
    validate_workout(workout)
    return workout


@router.get("/new", response_class=HTMLResponse)
def new_workout(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "workouts/form.html",
        context(request, active_page="workout-new", today=date.today(), error=None),
    )


@router.post("", response_class=HTMLResponse)
async def create_workout(request: Request, session: SessionDep) -> Response:
    try:
        data = await _parse_workout(request)
    except WorkoutValidationError as exc:
        return templates.TemplateResponse(
            request,
            "workouts/form.html",
            context(request, active_page="workout-new", today=date.today(), error=str(exc)),
            status_code=422,
        )
    user = get_or_create_default_user(session)
    workout = Workout(
        user_id=user.id,
        name=data.name,
        sport=data.sport,
        scheduled_for=data.scheduled_for,
        description=data.description or None,
        status="draft",
    )
    for position, step in enumerate(data.steps, 1):
        workout.steps.append(
            WorkoutStep(
                position=position,
                step_type=step.step_type,
                duration_type=step.duration_type,
                duration_value=step.duration_value,
                target_type="no_target",
                repeat_count=step.repeat_count,
            )
        )
    session.add(workout)
    session.commit()
    return RedirectResponse(f"/workouts/{workout.id}", status_code=303)


def _get_workout(session: Session, workout_id: int) -> Workout:
    user = get_or_create_default_user(session)
    workout = find_workout(session, user.id, workout_id)
    if workout is None:
        raise HTTPException(status_code=404, detail="Workout nicht gefunden")
    return workout


@router.get("/{workout_id}", response_class=HTMLResponse)
def workout_detail(
    workout_id: int,
    request: Request,
    session: SessionDep,
    error: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "workouts/detail.html",
        context(
            request,
            active_page="plans",
            workout=_get_workout(session, workout_id),
            error=error,
        ),
    )


@router.post("/{workout_id}/confirm")
def confirm_workout(workout_id: int, session: SessionDep) -> RedirectResponse:
    workout = _get_workout(session, workout_id)
    if workout.status == "draft":
        workout.status = "confirmed"
        session.commit()
    return RedirectResponse(f"/workouts/{workout.id}", status_code=303)


@router.post("/{workout_id}/publish")
def publish(workout_id: int, session: SessionDep) -> RedirectResponse:
    workout = _get_workout(session, workout_id)
    if workout.status not in {"confirmed", "published", "pushed"}:
        error = "Bitte den Entwurf vor der Übertragung bestätigen."
        return RedirectResponse(
            f"/workouts/{workout.id}?{urlencode({'error': error})}", status_code=303
        )
    try:
        if not workout.garmin_workout_id:
            workout.garmin_workout_id = publish_workout(workout)
        workout.status = "published"
        session.commit()
    except Exception as exc:
        session.rollback()
        query = urlencode({"error": message_from_exception(exc)})
        return RedirectResponse(f"/workouts/{workout.id}?{query}", status_code=303)
    return RedirectResponse(f"/workouts/{workout.id}", status_code=303)


@router.post("/{workout_id}/push")
def push(workout_id: int, session: SessionDep) -> RedirectResponse:
    workout = _get_workout(session, workout_id)
    try:
        push_workout(workout)
        workout.status = "pushed"
        session.commit()
    except Exception as exc:
        session.rollback()
        query = urlencode({"error": message_from_exception(exc)})
        return RedirectResponse(f"/workouts/{workout.id}?{query}", status_code=303)
    return RedirectResponse(f"/workouts/{workout.id}", status_code=303)
