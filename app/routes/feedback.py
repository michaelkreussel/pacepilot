import json
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError

from app.auth import CurrentUser
from app.database import SessionDep
from app.onboarding import require_data_access, require_planning_access
from app.services.planning.feedback_service import FeedbackNotFoundError, FeedbackService
from app.services.planning.safety_triage import (
    IllnessSignal,
    PainInput,
    PostSessionFeedbackInput,
    PreSessionFeedbackInput,
)

router = APIRouter()


def _optional_int(form: Any, name: str) -> int | None:
    value = str(form.get(name, "")).strip()
    return int(value) if value else None


def _optional_bool(form: Any, name: str) -> bool | None:
    value = str(form.get(name, "")).strip()
    if not value:
        return None
    if value not in {"yes", "no"}:
        raise ValueError(name)
    return value == "yes"


def _pain_input(form: Any) -> PainInput:
    present = str(form.get("pain_present", "")) == "yes"
    return PainInput(
        present=present,
        location=str(form.get("pain_location", "")).strip() or None if present else None,
        severity=_optional_int(form, "pain_severity") if present else None,
        alters_gait=_optional_bool(form, "pain_alters_gait") if present else None,
        worsens_with_activity=(
            _optional_bool(form, "pain_worsens_with_activity") if present else None
        ),
    )


def _error_redirect(path: str, message: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?{urlencode({'error': message})}", status_code=303)


@router.post(
    "/workouts/{workout_id}/feedback/pre-session",
    dependencies=[Depends(require_planning_access)],
)
async def record_pre_session_feedback(
    workout_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
) -> RedirectResponse:
    form = await request.form()
    try:
        data = PreSessionFeedbackInput(
            motivation=int(str(form.get("motivation", ""))),
            fatigue=int(str(form.get("fatigue", ""))),
            leg_freshness=int(str(form.get("leg_freshness", ""))),
            soreness=int(str(form.get("soreness", ""))),
            sleep_quality=_optional_int(form, "sleep_quality"),
            pain=_pain_input(form),
            illness_signal=IllnessSignal(str(form.get("illness_signal", "none"))),
            available_minutes=_optional_int(form, "available_minutes"),
            notes=str(form.get("notes", "")).strip() or None,
        )
        FeedbackService(session, user).record_pre_session(workout_id, data)
    except (FeedbackNotFoundError, ValidationError, ValueError):
        return _error_redirect(
            f"/workouts/{workout_id}",
            "Bitte prüfe die Angaben zum Tagesgefühl.",
        )
    return RedirectResponse(
        f"/workouts/{workout_id}?{urlencode({'notice': 'Tagesgefühl gespeichert.'})}",
        status_code=303,
    )


@router.post(
    "/activities/{activity_id}/feedback/post-session",
    dependencies=[Depends(require_data_access)],
)
async def record_post_session_feedback(
    activity_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
) -> RedirectResponse:
    form = await request.form()
    try:
        data = PostSessionFeedbackInput(
            completion_percent=int(str(form.get("completion_percent", ""))),
            session_rpe=float(str(form.get("session_rpe", ""))),
            overall_feel=int(str(form.get("overall_feel", ""))),
            pain=_pain_input(form),
            stopped_reason=str(form.get("stopped_reason", "")).strip() or None,
            notes=str(form.get("notes", "")).strip() or None,
        )
        FeedbackService(session, user).record_post_session(activity_id, data)
    except (FeedbackNotFoundError, ValidationError, ValueError):
        return _error_redirect(
            f"/activities/{activity_id}",
            "Bitte prüfe die Angaben zur Einheit.",
        )
    return RedirectResponse(
        f"/activities/{activity_id}?{urlencode({'notice': 'Feedback gespeichert.'})}",
        status_code=303,
    )


@router.post(
    "/feedback/pre-session/{feedback_id}/delete",
    dependencies=[Depends(require_planning_access)],
)
def delete_pre_session_feedback(
    feedback_id: int,
    session: SessionDep,
    user: CurrentUser,
) -> RedirectResponse:
    try:
        FeedbackService(session, user).delete_pre_session(feedback_id)
    except FeedbackNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(
        f"/settings?{urlencode({'notice': 'Feedback gelöscht.'})}", status_code=303
    )


@router.post(
    "/feedback/post-session/{feedback_id}/delete",
    dependencies=[Depends(require_data_access)],
)
def delete_post_session_feedback(
    feedback_id: int,
    session: SessionDep,
    user: CurrentUser,
) -> RedirectResponse:
    try:
        FeedbackService(session, user).delete_post_session(feedback_id)
    except FeedbackNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(
        f"/settings?{urlencode({'notice': 'Feedback gelöscht.'})}", status_code=303
    )


@router.get(
    "/settings/feedback/export",
    response_class=Response,
    dependencies=[Depends(require_data_access)],
)
def export_feedback(session: SessionDep, user: CurrentUser) -> Response:
    payload = FeedbackService(session, user).export_data()
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="pacepilot-subjektives-feedback.json"',
            "Cache-Control": "no-store",
        },
    )
