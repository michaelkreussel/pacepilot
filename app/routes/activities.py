from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.database import SessionDep
from app.repositories.activities import find_activity, list_activities
from app.repositories.users import get_or_create_default_user
from app.web import context, templates

router = APIRouter(prefix="/activities")


@router.get("", response_class=HTMLResponse)
def activities(request: Request, session: SessionDep) -> HTMLResponse:
    user = get_or_create_default_user(session)
    return templates.TemplateResponse(
        request,
        "activities/index.html",
        context(
            request,
            active_page="activities",
            activities=list_activities(session, user.id),
        ),
    )


@router.get("/{activity_id}", response_class=HTMLResponse)
def activity_detail(activity_id: int, request: Request, session: SessionDep) -> HTMLResponse:
    user = get_or_create_default_user(session)
    activity = find_activity(session, user.id, activity_id)
    if activity is None:
        raise HTTPException(status_code=404, detail="Aktivität nicht gefunden")
    return templates.TemplateResponse(
        request,
        "activities/detail.html",
        context(request, active_page="activities", activity=activity),
    )
