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
from app.services.garmin.workout_export import (
    delete_published_workout,
    publish_workout,
    push_workout,
    update_published_workout,
)
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


def _parse_optional_pace(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    try:
        minutes, seconds = (int(part) for part in value.split(":"))
    except (ValueError, TypeError) as exc:
        raise WorkoutValidationError("Pace bitte als MM:SS angeben.") from exc
    if minutes < 0 or not 0 <= seconds <= 59 or minutes + seconds == 0:
        raise WorkoutValidationError("Pace bitte als MM:SS angeben.")
    return float(minutes * 60 + seconds)


def _format_pace_input(value: float | None) -> str:
    if value is None:
        return ""
    minutes, seconds = divmod(round(value), 60)
    return f"{minutes}:{seconds:02d}"


def _form_steps(workout: Workout | None) -> list[dict[str, object]]:
    if workout is None:
        return []
    return [
        {
            "id": step.id,
            "type": step.step_type,
            "duration": step.duration_type,
            "value": (
                (step.duration_value or 0) / 60
                if step.duration_type == "time"
                else step.duration_value
            ),
            "repeats": step.repeat_count or 1,
            "target": step.target_type,
            "targetMin": _format_pace_input(step.target_min),
            "targetMax": _format_pace_input(step.target_max),
        }
        for step in workout.steps
    ]


async def _parse_workout(request: Request) -> WorkoutInput:
    form = await request.form()
    step_types = form.getlist("step_type")
    duration_types = form.getlist("duration_type")
    values = form.getlist("duration_value")
    repeats = form.getlist("repeat_count")
    target_types = form.getlist("target_type")
    target_mins = form.getlist("target_min")
    target_maxes = form.getlist("target_max")
    steps: list[StepInput] = []
    for index, step_type in enumerate(step_types):
        duration_type = str(duration_types[index]) if index < len(duration_types) else "time"
        raw_value = str(values[index]) if index < len(values) else ""
        raw_repeat = str(repeats[index]) if index < len(repeats) else "1"
        target_type = str(target_types[index]) if index < len(target_types) else "no_target"
        target_min = _parse_optional_pace(
            str(target_mins[index]) if index < len(target_mins) else ""
        )
        target_max = _parse_optional_pace(
            str(target_maxes[index]) if index < len(target_maxes) else ""
        )
        try:
            value = float(raw_value) if raw_value else None
            repeat_count = int(raw_repeat or 1)
        except ValueError as exc:
            raise WorkoutValidationError("Dauer und Wiederholungen müssen Zahlen sein.") from exc
        if duration_type == "time" and value is not None:
            value *= 60
        steps.append(
            StepInput(
                str(step_type),
                duration_type,
                value,
                repeat_count,
                target_type,
                target_min,
                target_max,
            )
        )
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
        context(
            request,
            active_page="workout-new",
            today=date.today(),
            workout=None,
            initial_steps=[],
            error=None,
        ),
    )


@router.post("", response_class=HTMLResponse)
async def create_workout(request: Request, session: SessionDep) -> Response:
    try:
        data = await _parse_workout(request)
    except WorkoutValidationError as exc:
        return templates.TemplateResponse(
            request,
            "workouts/form.html",
            context(
                request,
                active_page="workout-new",
                today=date.today(),
                workout=None,
                initial_steps=[],
                error=str(exc),
            ),
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
    _replace_workout_steps(workout, data)
    session.add(workout)
    session.commit()
    return RedirectResponse(f"/workouts/{workout.id}", status_code=303)


def _get_workout(session: Session, workout_id: int) -> Workout:
    user = get_or_create_default_user(session)
    workout = find_workout(session, user.id, workout_id)
    if workout is None:
        raise HTTPException(status_code=404, detail="Workout nicht gefunden")
    return workout


def _replace_workout_steps(workout: Workout, data: WorkoutInput) -> None:
    workout.steps.clear()
    for position, step in enumerate(data.steps, 1):
        workout.steps.append(
            WorkoutStep(
                position=position,
                step_type=step.step_type,
                duration_type=step.duration_type,
                duration_value=step.duration_value,
                target_type=step.target_type,
                target_min=step.target_min,
                target_max=step.target_max,
                repeat_count=step.repeat_count,
            )
        )


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


@router.get("/{workout_id}/edit", response_class=HTMLResponse)
def edit_workout(workout_id: int, request: Request, session: SessionDep) -> HTMLResponse:
    workout = _get_workout(session, workout_id)
    return templates.TemplateResponse(
        request,
        "workouts/form.html",
        context(
            request,
            active_page="plans",
            today=date.today(),
            workout=workout,
            initial_steps=_form_steps(workout),
            error=None,
        ),
    )


@router.post("/{workout_id}", response_class=HTMLResponse)
async def update_workout(workout_id: int, request: Request, session: SessionDep) -> Response:
    workout = _get_workout(session, workout_id)
    try:
        data = await _parse_workout(request)
    except WorkoutValidationError as exc:
        return templates.TemplateResponse(
            request,
            "workouts/form.html",
            context(
                request,
                active_page="plans",
                today=date.today(),
                workout=workout,
                initial_steps=_form_steps(workout),
                error=str(exc),
            ),
            status_code=422,
        )

    previous_date = workout.scheduled_for
    workout.name = data.name
    workout.sport = data.sport
    workout.scheduled_for = data.scheduled_for
    workout.description = data.description or None
    _replace_workout_steps(workout, data)
    try:
        if workout.garmin_workout_id:
            update_published_workout(workout, previous_date)
            workout.status = "pushed"
        session.commit()
    except Exception as exc:
        session.rollback()
        query = urlencode({"error": message_from_exception(exc)})
        return RedirectResponse(f"/workouts/{workout.id}?{query}", status_code=303)
    return RedirectResponse(f"/workouts/{workout.id}", status_code=303)


@router.post("/{workout_id}/delete")
def delete_workout(workout_id: int, session: SessionDep) -> RedirectResponse:
    workout = _get_workout(session, workout_id)
    try:
        delete_published_workout(workout)
        session.delete(workout)
        session.commit()
    except Exception as exc:
        session.rollback()
        query = urlencode({"error": message_from_exception(exc)})
        return RedirectResponse(f"/workouts/{workout.id}?{query}", status_code=303)
    return RedirectResponse("/plans", status_code=303)


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
