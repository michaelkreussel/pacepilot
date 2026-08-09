from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import CurrentUser
from app.database import SessionDep
from app.models.user import utcnow
from app.onboarding import onboarding_state
from app.web import context, templates

router = APIRouter()

BLOCKED_COPY = {
    "planning": "Trainingsplan und Workout-Editor werden verfügbar, sobald Garmin verbunden ist.",
    "data": "Dieser Bereich wird nach deiner ersten erfolgreichen Synchronisierung verfügbar.",
}


@router.get("/onboarding", response_class=HTMLResponse)
def onboarding_page(
    request: Request,
    user: CurrentUser,
    blocked: str | None = None,
    review: bool = False,
) -> Response:
    state = onboarding_state(user)
    review = review and state.complete
    if state.complete and not review:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "onboarding.html",
        context(
            request,
            active_page="onboarding",
            review=review,
            blocked_message=BLOCKED_COPY.get(blocked or ""),
        ),
    )


@router.post("/onboarding/start", response_class=RedirectResponse, status_code=303)
def start_onboarding(session: SessionDep, user: CurrentUser) -> RedirectResponse:
    if user.onboarding_notice_acknowledged_at is None:
        user.onboarding_notice_acknowledged_at = utcnow()
        session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request, _: CurrentUser) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "help.html",
        context(request, active_page="help"),
    )
