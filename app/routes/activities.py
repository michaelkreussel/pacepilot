from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.auth import CurrentUser
from app.database import SessionDep
from app.onboarding import require_data_access
from app.repositories.activities import find_activity, list_activities_filtered
from app.services.garmin.activity_details import empty_activity_details, load_activity_details
from app.web import context, templates

router = APIRouter(prefix="/activities", dependencies=[Depends(require_data_access)])


@router.get("", response_class=HTMLResponse)
def activities(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
    sport: str | None = None,
) -> HTMLResponse:
    if from_date is not None and to_date is not None and from_date > to_date:
        raise HTTPException(status_code=400, detail="Der Start darf nicht nach dem Ende liegen")
    return templates.TemplateResponse(
        request,
        "activities/index.html",
        context(
            request,
            active_page="activities",
            activities=list_activities_filtered(
                session,
                user.id,
                start=from_date,
                end=to_date,
                sport=sport,
            ),
            from_date=from_date,
            to_date=to_date,
            sport=sport,
            filters_active=from_date is not None or to_date is not None or sport is not None,
        ),
    )


@router.get("/{activity_id}", response_class=HTMLResponse)
def activity_detail(
    activity_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> HTMLResponse:
    activity = find_activity(session, user.id, activity_id)
    if activity is None:
        raise HTTPException(status_code=404, detail="Aktivität nicht gefunden")
    return templates.TemplateResponse(
        request,
        "activities/detail.html",
        context(
            request,
            active_page="activities",
            activity=activity,
            activity_data=(
                load_activity_details(
                    activity.started_at,
                    activity.garmin_activity_id,
                    activity.activity_type,
                    activity.user_id,
                )
                if activity.details_complete and activity.details_file
                else empty_activity_details(activity.activity_type)
            ),
        ),
    )
