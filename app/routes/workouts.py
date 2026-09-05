import json
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Never
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from app.auth import CurrentUser
from app.config import coach_feature_enabled, get_settings
from app.database import SessionDep
from app.models import WorkoutRevision
from app.onboarding import require_planning_access
from app.services.garmin.client import (
    GarminUnavailableError,
    connect_garmin_account,
    message_from_exception,
)
from app.services.planning.daily_adaptation import (
    DailyAdaptationClass,
    DailyAdaptationError,
    DailyAdaptationPreview,
    DailyAdaptationService,
)
from app.services.planning.feedback_service import FeedbackQueries
from app.services.planning.validator import WorkoutInput, WorkoutValidationError, validate_workout
from app.services.planning.workout_definition import (
    DefinitionValidationError,
    default_definition,
    definition_to_json,
    parse_definition,
)
from app.services.planning.workout_revision import (
    AcceptRevisionCommand,
    RejectRevisionCommand,
    RevisionIdentity,
    ScheduleWorkoutCommand,
    UnscheduleWorkoutCommand,
)
from app.services.planning.workout_service import (
    WorkoutConflictError,
    WorkoutNotFoundError,
    WorkoutService,
    WorkoutTransitionError,
)
from app.services.planning.workout_views import WorkoutDetailView, workout_detail_view
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


def _definition_schema_message(exc: ValidationError) -> str:
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        if error["type"] == "too_long" and "instructions" in error["loc"]:
            return f"Zu viele Schrittanweisungen bei {location}: maximal fünf Zeilen."
        if error["type"] == "string_too_long" and "instructions" in error["loc"]:
            return f"Schrittanweisung bei {location} ist zu lang: maximal 300 Zeichen."
    return "Die Workout-Struktur ist unvollständig oder enthält ungültige Felder."


def _form_data(workout: WorkoutDetailView | None = None) -> dict[str, str]:
    revision = workout.current if workout else None
    return {
        "name": revision.name if revision else "",
        "sport": revision.sport if revision else "running",
        "scheduled_for": (
            revision.suggested_for.isoformat() if revision and revision.suggested_for else ""
        ),
        "description": revision.description or "" if revision else "",
        "definition_version": str(revision.definition_version if revision else 1),
    }


async def _parse_workout_result(request: Request) -> WorkoutInput | WorkoutFormError:
    form = await request.form()
    form_data = {
        "name": str(form.get("name", "")).strip(),
        "sport": str(form.get("sport", "running")),
        "scheduled_for": str(form.get("scheduled_for", "")),
        "description": str(form.get("description", "")).strip(),
        "definition_version": str(form.get("definition_version", "1")),
    }
    fallback = definition_to_json(default_definition())
    try:
        definition_version = int(form_data["definition_version"])
        raw_definition = json.loads(str(form.get("definition", "")))
        definition = parse_definition(raw_definition, definition_version)
        workout = WorkoutInput(
            name=form_data["name"],
            sport=form_data["sport"],
            scheduled_for=_parse_optional_date(form_data["scheduled_for"]),
            description=form_data["description"],
            definition=definition,
            definition_version=definition_version,
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
    except ValidationError as exc:
        return WorkoutFormError(
            "definition.schema_invalid",
            _definition_schema_message(exc),
            form_data,
            raw_definition if isinstance(raw_definition, dict) else fallback,
        )
    except WorkoutValidationError as exc:
        definition_data = definition_to_json(definition) if "definition" in locals() else fallback
        return WorkoutFormError(exc.code, str(exc), form_data, definition_data)
    except (DefinitionValidationError, ValueError) as exc:
        code = (
            exc.code if isinstance(exc, DefinitionValidationError) else "definition.version_invalid"
        )
        message = (
            str(exc) if isinstance(exc, DefinitionValidationError) else "Ungültige Formatversion."
        )
        return WorkoutFormError(
            code,
            message,
            {**form_data, "definition_version": "1"},
            fallback,
        )


WorkoutFormDep = Annotated[WorkoutInput | WorkoutFormError, Depends(_parse_workout_result)]


def _workout_service(request: Request, session: SessionDep, user: CurrentUser) -> WorkoutService:
    # Passing the connector keeps the route test seam while orchestration remains in the service.
    return WorkoutService(
        session,
        user,
        connect_garmin=connect_garmin_account,
        request_id=request.state.request_id,
    )


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
            edit_idempotency_key=None,
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


def _get_workout(service: WorkoutService, workout_id: int) -> WorkoutDetailView:
    try:
        workout = service.get(workout_id)
        safety_context = service.acceptance_context(workout_id)
        action_revision_id = (
            workout.current_revision_id
            if workout.current_revision_id != workout.accepted_revision_id
            else workout.accepted_revision_id
        )
        action_revision = (
            service.session.get(WorkoutRevision, action_revision_id)
            if action_revision_id is not None
            else None
        )
        effective_date = (
            workout.scheduled_for or action_revision.suggested_for
            if action_revision is not None
            else None
        )
        training_fit = (
            service.local_action_training_fit(workout.id, action_revision.id, effective_date)
            if action_revision is not None and effective_date is not None
            else None
        )
        schedule_effective_date = None
        schedule_training_fit = None
        if workout.accepted_revision_id is not None:
            accepted_revision = service.session.get(WorkoutRevision, workout.accepted_revision_id)
            if (
                accepted_revision is not None
                and accepted_revision.suggested_for is not None
                and workout.scheduled_for != accepted_revision.suggested_for
            ):
                schedule_effective_date = accepted_revision.suggested_for
                schedule_training_fit = service.local_action_training_fit(
                    workout.id, accepted_revision.id, schedule_effective_date
                )
        return workout_detail_view(
            service.session,
            workout,
            context_fingerprint=safety_context.fingerprint,
            safety_report=safety_context.report.to_json(),
            training_fit_outcome=(
                training_fit.assessment.outcome.value if training_fit is not None else None
            ),
            training_fit_effective_date=effective_date,
            training_fit_acknowledgement_required=(
                training_fit.acknowledgement_required if training_fit is not None else False
            ),
            training_fit_schedule_acknowledgement_required=(
                schedule_training_fit.acknowledgement_required
                if schedule_training_fit is not None
                else False
            ),
        )
    except WorkoutNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _ensure_workout_exists(service: WorkoutService, workout_id: int) -> None:
    try:
        service.get(workout_id)
    except WorkoutNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _raise_not_found(exc: WorkoutNotFoundError) -> Never:
    raise HTTPException(status_code=404, detail=str(exc)) from exc


def _operation_error_redirect(
    workout_id: int,
    exc: GarminUnavailableError
    | WorkoutValidationError
    | WorkoutTransitionError
    | WorkoutConflictError,
) -> RedirectResponse:
    message = (
        str(exc)
        if isinstance(exc, (WorkoutTransitionError, WorkoutConflictError))
        else message_from_exception(exc)
    )
    query = urlencode({"error": message})
    return RedirectResponse(f"/workouts/{workout_id}?{query}", status_code=303)


def _adaptation_preview(service: WorkoutService, workout_id: int) -> DailyAdaptationPreview | None:
    if not coach_feature_enabled(get_settings().coach_daily_adaptation_enabled, service.user.id):
        return None
    try:
        return DailyAdaptationService(
            service.session,
            service.user,
            as_of=date.today(),
            request_id=service.request_id,
        ).assess_today(workout_id)
    except DailyAdaptationError:
        return None


@router.get("/{workout_id}", response_class=HTMLResponse)
def workout_detail(
    workout_id: int,
    request: Request,
    service: WorkoutServiceDep,
    error: str | None = None,
    notice: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "workouts/detail.html",
        context(
            request,
            active_page="plans",
            workout=_get_workout(service, workout_id),
            pre_session_feedback=FeedbackQueries(
                service.session, service.user
            ).pre_session_for_workout(workout_id),
            adaptation_preview=_adaptation_preview(service, workout_id),
            adaptation_apply_key=str(uuid4()),
            error=error,
            notice=notice,
        ),
    )


@router.get("/{workout_id}/edit", response_class=HTMLResponse)
def edit_workout(workout_id: int, request: Request, service: WorkoutServiceDep) -> HTMLResponse:
    workout = _get_workout(service, workout_id)
    if workout.source_type == "coach_weekly_plan":
        raise HTTPException(
            status_code=409,
            detail="Wochenplan-Vorschläge werden angenommen oder abgelehnt, nicht bearbeitet.",
        )
    return templates.TemplateResponse(
        request,
        "workouts/form.html",
        context(
            request,
            active_page="plans",
            today=date.today(),
            workout=workout,
            form_data=_form_data(workout),
            initial_definition=workout.current.definition,
            error=None,
            edit_idempotency_key=str(uuid4()),
        ),
    )


@router.post("/{workout_id}", response_class=HTMLResponse)
async def update_workout(
    workout_id: int,
    request: Request,
    data: WorkoutFormDep,
    service: WorkoutServiceDep,
) -> Response:
    workout = _get_workout(service, workout_id)
    form = await request.form()
    edit_idempotency_key = str(form.get("idempotency_key", "")) or str(uuid4())
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
                edit_idempotency_key=edit_idempotency_key,
            ),
            status_code=422,
        )

    try:
        identity = None
        if workout.source_type == "coach_single":
            try:
                identity = RevisionIdentity(
                    revision_id=int(str(form["revision_id"])),
                    revision_number=int(str(form["revision_number"])),
                    content_hash=str(form["content_hash"]),
                    lock_version=int(str(form["lock_version"])),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="Ungültige Revisionsangaben") from exc
        service.update(
            workout_id,
            data,
            expected_identity=identity,
            idempotency_key=(
                edit_idempotency_key if workout.source_type == "coach_single" else None
            ),
        )
    except (
        GarminUnavailableError,
        WorkoutValidationError,
        WorkoutTransitionError,
        WorkoutConflictError,
    ) as exc:
        return _operation_error_redirect(workout_id, exc)
    return RedirectResponse(f"/workouts/{workout.id}", status_code=303)


@router.post("/{workout_id}/delete", response_class=RedirectResponse, status_code=303)
def delete_workout(workout_id: int, service: WorkoutServiceDep) -> RedirectResponse:
    try:
        service.delete(workout_id)
    except WorkoutNotFoundError as exc:
        _raise_not_found(exc)
    except (GarminUnavailableError, WorkoutValidationError, WorkoutTransitionError) as exc:
        return _operation_error_redirect(workout_id, exc)
    return RedirectResponse("/plans", status_code=303)


@router.post("/{workout_id}/confirm", response_class=RedirectResponse, status_code=303)
async def confirm_workout(
    workout_id: int,
    request: Request,
    service: WorkoutServiceDep,
) -> Response:
    _ensure_workout_exists(service, workout_id)
    form = await request.form()
    try:
        revision_id = int(str(form["revision_id"]))
        revision_number = int(str(form["revision_number"]))
        content_hash = str(form["content_hash"])
        lock_version = int(str(form["lock_version"]))
        context_fingerprint = str(form["context_fingerprint"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Ungültige Revisionsangaben") from exc
    try:
        service.accept(
            workout_id,
            AcceptRevisionCommand(
                identity=RevisionIdentity(
                    revision_id=revision_id,
                    revision_number=revision_number,
                    content_hash=content_hash,
                    lock_version=lock_version,
                ),
                context_fingerprint=context_fingerprint,
                acknowledge_elevated_warning=(
                    str(form.get("acknowledge_elevated_warning", "")) == "yes"
                ),
            ),
        )
    except WorkoutNotFoundError as exc:
        _raise_not_found(exc)
    except WorkoutConflictError as exc:
        return templates.TemplateResponse(
            request,
            "workouts/detail.html",
            context(
                request,
                active_page="plans",
                workout=_get_workout(service, workout_id),
                pre_session_feedback=FeedbackQueries(
                    service.session, service.user
                ).pre_session_for_workout(workout_id),
                error=str(exc),
                notice=None,
            ),
            status_code=409,
        )
    except (WorkoutValidationError, WorkoutTransitionError) as exc:
        return _operation_error_redirect(workout_id, exc)
    return RedirectResponse(f"/workouts/{workout_id}", status_code=303)


@router.post("/{workout_id}/reject", response_class=RedirectResponse, status_code=303)
async def reject_workout(
    workout_id: int,
    request: Request,
    service: WorkoutServiceDep,
) -> Response:
    _ensure_workout_exists(service, workout_id)
    form = await request.form()
    try:
        command = RejectRevisionCommand(
            identity=RevisionIdentity(
                revision_id=int(str(form["revision_id"])),
                revision_number=int(str(form["revision_number"])),
                content_hash=str(form["content_hash"]),
                lock_version=int(str(form["lock_version"])),
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Ungültige Revisionsangaben") from exc
    try:
        service.reject(workout_id, command)
    except WorkoutNotFoundError as exc:
        _raise_not_found(exc)
    except (WorkoutTransitionError, WorkoutConflictError) as exc:
        return _operation_error_redirect(workout_id, exc)
    return RedirectResponse(f"/workouts/{workout_id}?notice=Vorschlag abgelehnt", status_code=303)


@router.post("/{workout_id}/adaptation/apply", response_class=RedirectResponse, status_code=303)
async def apply_adaptation(
    workout_id: int,
    request: Request,
    service: WorkoutServiceDep,
) -> Response:
    _ensure_workout_exists(service, workout_id)
    form = await request.form()
    try:
        adaptation_class = DailyAdaptationClass(str(form["adaptation_class"]))
        context_fingerprint = str(form["context_fingerprint"])
        idempotency_key = str(form["idempotency_key"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Ungültige Anpassungsangaben") from exc
    if not idempotency_key or len(idempotency_key) > 200:
        raise HTTPException(status_code=422, detail="Ungültiger Wiederholungsschlüssel")
    try:
        result = DailyAdaptationService(
            service.session,
            service.user,
            as_of=date.today(),
            request_id=service.request_id,
        ).apply(
            workout_id,
            adaptation_class,
            expected_context_fingerprint=context_fingerprint,
            idempotency_key=idempotency_key,
            acknowledge_elevated_warning=(
                str(form.get("acknowledge_elevated_warning", "")) == "yes"
            ),
        )
    except DailyAdaptationError as exc:
        query = urlencode({"error": str(exc)})
        return RedirectResponse(f"/workouts/{workout_id}?{query}", status_code=303)
    except (WorkoutTransitionError, WorkoutConflictError) as exc:
        return _operation_error_redirect(workout_id, exc)
    notice = {
        DailyAdaptationClass.KEEP: "Training für heute bestätigt.",
        DailyAdaptationClass.REDUCE_VOLUME: "Reduzierte Revision erstellt. Bitte annehmen.",
        DailyAdaptationClass.REPLACE_WITH_EASY: "Easy-Run-Ersatz erstellt. Bitte annehmen.",
        DailyAdaptationClass.REST: "Ruhetag eingetragen; der Termin wurde entfernt.",
    }[adaptation_class]
    return RedirectResponse(
        f"/workouts/{result.workout.id}?{urlencode({'notice': notice})}", status_code=303
    )


@router.post("/{workout_id}/adaptation/discard", response_class=RedirectResponse, status_code=303)
async def discard_adaptation(
    workout_id: int,
    request: Request,
    service: WorkoutServiceDep,
) -> Response:
    _ensure_workout_exists(service, workout_id)
    form = await request.form()
    try:
        command = RejectRevisionCommand(
            identity=RevisionIdentity(
                revision_id=int(str(form["revision_id"])),
                revision_number=int(str(form["revision_number"])),
                content_hash=str(form["content_hash"]),
                lock_version=int(str(form["lock_version"])),
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Ungültige Revisionsangaben") from exc
    try:
        service.discard_adaptation_revision(workout_id, command)
    except (WorkoutTransitionError, WorkoutConflictError) as exc:
        return _operation_error_redirect(workout_id, exc)
    return RedirectResponse(f"/workouts/{workout_id}?notice=Anpassung verworfen", status_code=303)


@router.post("/{workout_id}/schedule", response_class=RedirectResponse, status_code=303)
async def schedule_workout(
    workout_id: int,
    request: Request,
    service: WorkoutServiceDep,
) -> Response:
    _ensure_workout_exists(service, workout_id)
    form = await request.form()
    try:
        revision_id = int(str(form["revision_id"]))
        lock_version = int(str(form["lock_version"]))
        scheduled_for_value = str(form["scheduled_for"])
        if not scheduled_for_value:
            raise ValueError
        scheduled_for = date.fromisoformat(scheduled_for_value)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Ungültige Revisionsangaben") from exc
    try:
        service.schedule(
            workout_id,
            ScheduleWorkoutCommand(
                revision_id=revision_id,
                scheduled_for=scheduled_for,
                expected_lock_version=lock_version,
                acknowledge_elevated_warning=(
                    str(form.get("acknowledge_elevated_warning", "")) == "yes"
                ),
            ),
        )
    except WorkoutNotFoundError as exc:
        _raise_not_found(exc)
    except (WorkoutValidationError, WorkoutTransitionError, WorkoutConflictError) as exc:
        return _operation_error_redirect(workout_id, exc)
    return RedirectResponse(f"/workouts/{workout_id}", status_code=303)


@router.post("/{workout_id}/unschedule", response_class=RedirectResponse, status_code=303)
async def unschedule_workout(
    workout_id: int,
    request: Request,
    service: WorkoutServiceDep,
) -> Response:
    _ensure_workout_exists(service, workout_id)
    form = await request.form()
    try:
        revision_id = int(str(form["revision_id"]))
        lock_version = int(str(form["lock_version"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Ungültige Revisionsangaben") from exc
    try:
        service.unschedule(
            workout_id,
            UnscheduleWorkoutCommand(
                revision_id=revision_id,
                expected_lock_version=lock_version,
            ),
        )
    except WorkoutNotFoundError as exc:
        _raise_not_found(exc)
    except (WorkoutValidationError, WorkoutTransitionError, WorkoutConflictError) as exc:
        return _operation_error_redirect(workout_id, exc)
    return RedirectResponse(f"/workouts/{workout_id}", status_code=303)


@router.post("/{workout_id}/publish", response_class=RedirectResponse, status_code=303)
def publish(
    workout_id: int,
    service: WorkoutServiceDep,
    acknowledge_elevated_warning: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    try:
        service.publish(
            workout_id,
            acknowledge_elevated_warning=acknowledge_elevated_warning == "yes",
        )
    except WorkoutNotFoundError as exc:
        _raise_not_found(exc)
    except (
        GarminUnavailableError,
        WorkoutValidationError,
        WorkoutTransitionError,
        WorkoutConflictError,
    ) as exc:
        return _operation_error_redirect(workout_id, exc)
    return RedirectResponse(f"/workouts/{workout_id}", status_code=303)


@router.post("/{workout_id}/push", response_class=RedirectResponse, status_code=303)
def push(
    workout_id: int,
    service: WorkoutServiceDep,
    acknowledge_elevated_warning: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    try:
        service.push(
            workout_id,
            acknowledge_elevated_warning=acknowledge_elevated_warning == "yes",
        )
    except WorkoutNotFoundError as exc:
        _raise_not_found(exc)
    except (
        GarminUnavailableError,
        WorkoutValidationError,
        WorkoutTransitionError,
        WorkoutConflictError,
    ) as exc:
        return _operation_error_redirect(workout_id, exc)
    return RedirectResponse(f"/workouts/{workout_id}", status_code=303)
