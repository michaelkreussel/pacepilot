from datetime import date
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.auth import CurrentUser
from app.database import SessionDep
from app.onboarding import require_data_access
from app.repositories.activities import (
    count_activities_filtered,
    find_activity,
    list_activities_filtered,
    list_activity_types,
)
from app.services.garmin.activity_details import empty_activity_details, load_activity_details
from app.web import context, format_activity_type, templates

router = APIRouter(prefix="/activities", dependencies=[Depends(require_data_access)])
PAGE_SIZE = 25


def _parse_optional_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail="Ungültiges Datum") from None


@router.get("", response_class=HTMLResponse)
def activities(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    from_value: Annotated[str | None, Query(alias="from")] = None,
    to_value: Annotated[str | None, Query(alias="to")] = None,
    sport: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    from_date = _parse_optional_date(from_value)
    to_date = _parse_optional_date(to_value)
    if from_date is not None and to_date is not None and from_date > to_date:
        raise HTTPException(status_code=400, detail="Der Start darf nicht nach dem Ende liegen")
    sport = sport or None
    total = count_activities_filtered(session, user.id, start=from_date, end=to_date, sport=sport)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > total_pages:
        raise HTTPException(status_code=404, detail="Seite nicht gefunden")
    filter_params = {}
    if from_date is not None:
        filter_params["from"] = from_date.isoformat()
    if to_date is not None:
        filter_params["to"] = to_date.isoformat()
    if sport is not None:
        filter_params["sport"] = sport
    encoded_filters = urlencode(filter_params)
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
                limit=PAGE_SIZE,
                offset=(page - 1) * PAGE_SIZE,
            ),
            activity_types=[
                (value, format_activity_type(value))
                for value in list_activity_types(session, user.id)
            ],
            from_date=from_date,
            to_date=to_date,
            sport=sport,
            filters_active=from_date is not None or to_date is not None or sport is not None,
            total=total,
            first_result=(page - 1) * PAGE_SIZE + 1 if total else 0,
            last_result=min(page * PAGE_SIZE, total),
            page=page,
            total_pages=total_pages,
            page_numbers=range(max(1, page - 2), min(total_pages, page + 2) + 1),
            page_url=f"/activities?{encoded_filters}{'&' if encoded_filters else ''}page=",
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
