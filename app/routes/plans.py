from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.database import SessionDep
from app.models import Workout
from app.repositories.users import get_or_create_default_user
from app.repositories.workouts import workouts_between
from app.web import context, templates

router = APIRouter(prefix="/plans")


@router.get("", response_class=HTMLResponse)
def training_plan(
    request: Request,
    session: SessionDep,
    week: Annotated[int, Query(ge=-52, le=52)] = 0,
) -> HTMLResponse:
    user = get_or_create_default_user(session)
    today = date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=week)
    sunday = monday + timedelta(days=6)
    workouts = workouts_between(session, user.id, monday, sunday)
    by_day: dict[date, list[Workout]] = {monday + timedelta(days=offset): [] for offset in range(7)}
    for workout in workouts:
        if workout.scheduled_for in by_day:
            by_day[workout.scheduled_for].append(workout)
    return templates.TemplateResponse(
        request,
        "plans/index.html",
        context(
            request,
            active_page="plans",
            week=week,
            monday=monday,
            sunday=sunday,
            by_day=by_day,
            today=today,
        ),
    )
