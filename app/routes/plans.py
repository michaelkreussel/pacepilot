from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.auth import CurrentUser
from app.database import SessionDep
from app.models import Workout
from app.repositories.workouts import workouts_between
from app.web import context, templates

router = APIRouter(prefix="/plans")

MONTH_NAMES = (
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
)


@router.get("", response_class=HTMLResponse)
def training_plan(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    view: Literal["week", "month"] = "week",
    week: Annotated[int, Query(ge=-52, le=52)] = 0,
    month: Annotated[int, Query(ge=-120, le=120)] = 0,
) -> HTMLResponse:
    today = date.today()

    if view == "month":
        month_index = today.year * 12 + today.month - 1 + month
        year, zero_based_month = divmod(month_index, 12)
        month_start = date(year, zero_based_month + 1, 1)
        if month_start.month == 12:
            next_month = date(month_start.year + 1, 1, 1)
        else:
            next_month = date(month_start.year, month_start.month + 1, 1)
        month_end = next_month - timedelta(days=1)
        calendar_start = month_start - timedelta(days=month_start.weekday())
        calendar_end = month_end + timedelta(days=6 - month_end.weekday())
        workouts = workouts_between(session, user.id, calendar_start, calendar_end)
        month_by_day: dict[date, list[Workout]] = {
            calendar_start + timedelta(days=offset): []
            for offset in range((calendar_end - calendar_start).days + 1)
        }
        for workout in workouts:
            scheduled_for = workout.scheduled_for
            if scheduled_for is not None and scheduled_for in month_by_day:
                month_by_day[scheduled_for].append(workout)

        month_weeks: list[tuple[int, dict[date, list[Workout]]]] = []
        week_start = calendar_start
        while week_start <= calendar_end:
            days = {}
            for offset in range(7):
                day = week_start + timedelta(days=offset)
                days[day] = month_by_day[day]
            month_weeks.append((week_start.isocalendar().week, days))
            week_start += timedelta(days=7)

        return templates.TemplateResponse(
            request,
            "plans/index.html",
            context(
                request,
                active_page="plans",
                view=view,
                month=month,
                month_start=month_start,
                month_end=month_end,
                month_name=MONTH_NAMES[month_start.month - 1],
                month_weeks=month_weeks,
                today=today,
            ),
        )

    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=week)
    sunday = monday + timedelta(days=6)
    workouts = workouts_between(session, user.id, monday, sunday)
    by_day: dict[date, list[Workout]] = {monday + timedelta(days=offset): [] for offset in range(7)}
    for workout in workouts:
        scheduled_for = workout.scheduled_for
        if scheduled_for is not None and scheduled_for in by_day:
            by_day[scheduled_for].append(workout)
    return templates.TemplateResponse(
        request,
        "plans/index.html",
        context(
            request,
            active_page="plans",
            view=view,
            week=week,
            monday=monday,
            sunday=sunday,
            by_day=by_day,
            today=today,
        ),
    )
