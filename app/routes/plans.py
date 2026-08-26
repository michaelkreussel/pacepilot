from datetime import date, timedelta
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import CurrentUser
from app.config import get_settings
from app.database import SessionDep
from app.models import (
    AthleteGoal,
    TrainingCycle,
    TrainingPlanRevision,
    TrainingPlanWorkout,
    Workout,
)
from app.onboarding import require_planning_access
from app.repositories.workouts import workouts_between
from app.services.planning.multiweek_planner import (
    TrainingCyclePersistenceError,
    accept_training_cycle_revision,
    get_training_cycle,
    list_training_cycle_weeks,
    persist_training_cycle,
    plan_training_cycle,
)
from app.services.planning.weekly_plan_service import (
    persist_week_candidate,
    plan_proposals_between,
)
from app.services.planning.weekly_planner import plan_shadow_week
from app.services.planning.workout_views import CalendarWorkout
from app.web import context, templates

router = APIRouter(prefix="/plans", dependencies=[Depends(require_planning_access)])

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
PLAN_ROLE_LABELS = {
    "easy_run": "Lockerer Lauf",
    "long_run": "Langer Lauf",
    "strides": "Steigerungen",
    "threshold_cruise": "Schwellenintervalle",
    "vo2_intervals": "VO₂max-Intervalle",
}

GOAL_TYPE_LABELS = {
    "general_fitness": "Allgemeine Fitness",
    "5k": "5 km",
    "10k": "10 km",
    "half_marathon": "Halbmarathon",
    "marathon": "Marathon",
}


def _proposal_items(
    session: Session, user_id: int, starts_on: date, ends_on: date
) -> dict[date, list[dict[str, object]]]:
    by_day: dict[date, list[dict[str, object]]] = {
        starts_on + timedelta(days=offset): [] for offset in range((ends_on - starts_on).days + 1)
    }
    for membership, workout in plan_proposals_between(session, user_id, starts_on, ends_on):
        by_day[membership.scheduled_for].append(
            {
                "id": workout.id,
                "name": workout.name,
                "role": PLAN_ROLE_LABELS.get(membership.role, membership.role),
            }
        )
    return by_day


@router.get("/cycles/new", response_class=HTMLResponse)
def new_training_cycle(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    error: Annotated[str | None, Query(max_length=500)] = None,
) -> HTMLResponse:
    if not get_settings().coach_plan_generation_enabled:
        raise HTTPException(status_code=404, detail="Seite nicht gefunden")
    goals = list(
        session.scalars(
            select(AthleteGoal)
            .where(AthleteGoal.user_id == user.id, AthleteGoal.status == "active")
            .order_by(AthleteGoal.target_date, AthleteGoal.id)
        )
    )
    current_week_start = date.today() - timedelta(days=date.today().weekday())
    start = (
        current_week_start
        if date.today().weekday() == 0
        else current_week_start + timedelta(days=7)
    )
    default_target = (
        goals[0].target_date if goals and goals[0].target_date else start + timedelta(days=83)
    )
    return templates.TemplateResponse(
        request,
        "plans/cycle_new.html",
        context(
            request,
            active_page="plans",
            goals=goals,
            goal_type_labels=GOAL_TYPE_LABELS,
            default_start=start,
            default_target=default_target,
            error=error,
        ),
    )


@router.post("/generate-week")
def generate_week_plan(
    session: SessionDep,
    user: CurrentUser,
    week_start: str = Form(),
) -> RedirectResponse:
    if not get_settings().coach_plan_generation_enabled:
        raise HTTPException(status_code=404, detail="Seite nicht gefunden")
    try:
        starts_on = date.fromisoformat(week_start)
        candidate = plan_shadow_week(session, user, week_start=starts_on)
        persist_week_candidate(session, user, candidate)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    current_monday = date.today() - timedelta(days=date.today().weekday())
    week_offset = (starts_on - current_monday).days // 7
    return RedirectResponse(f"/plans?view=week&week={week_offset}", status_code=303)


@router.post("/generate-cycle")
def generate_training_cycle(
    session: SessionDep,
    user: CurrentUser,
    start_date: str = Form(),
    target_date: str = Form(),
    goal_id: int | None = Form(None),
    event_type: str | None = Form(None),
) -> RedirectResponse:
    if not get_settings().coach_plan_generation_enabled:
        raise HTTPException(status_code=404, detail="Seite nicht gefunden")
    try:
        candidate = plan_training_cycle(
            session,
            user,
            start_date=date.fromisoformat(start_date),
            target_date=date.fromisoformat(target_date),
            goal_id=goal_id,
            event_type=event_type or None,
        )
        revision = persist_training_cycle(session, user, candidate)
    except ValueError as exc:
        return RedirectResponse(
            f"/plans/cycles/new?{urlencode({'error': str(exc)})}", status_code=303
        )
    return RedirectResponse(f"/plans/cycles/{revision.cycle_id}", status_code=303)


@router.post("/cycles/{cycle_id}/revisions/{revision_id}/accept")
def accept_training_cycle(
    cycle_id: int, revision_id: int, session: SessionDep, user: CurrentUser
) -> RedirectResponse:
    if not get_settings().coach_plan_generation_enabled:
        raise HTTPException(status_code=404, detail="Seite nicht gefunden")
    try:
        accept_training_cycle_revision(session, user, cycle_id=cycle_id, revision_id=revision_id)
    except TrainingCyclePersistenceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/plans/cycles/{cycle_id}", status_code=303)


@router.get("/cycles/{cycle_id}", response_class=HTMLResponse)
def training_cycle_detail(
    cycle_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> HTMLResponse:
    if not get_settings().coach_plan_generation_enabled:
        raise HTTPException(status_code=404, detail="Seite nicht gefunden")
    loaded = get_training_cycle(session, user.id, cycle_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail="Mehrwochenplan nicht gefunden")
    cycle, revision = loaded
    weeks = []
    for membership in list_training_cycle_weeks(session, user.id, revision.id):
        rows = session.execute(
            select(TrainingPlanWorkout, Workout)
            .join(Workout, Workout.id == TrainingPlanWorkout.workout_id)
            .join(
                TrainingPlanRevision,
                TrainingPlanRevision.id == TrainingPlanWorkout.plan_revision_id,
            )
            .where(
                TrainingPlanWorkout.plan_revision_id == membership.training_plan_revision_id,
                TrainingPlanWorkout.owner_user_id == user.id,
                Workout.user_id == user.id,
                Workout.deleted_at.is_(None),
            )
            .order_by(TrainingPlanWorkout.position)
        ).all()
        weeks.append(
            {
                "membership": membership,
                "workouts": [
                    {"membership": workout_membership, "workout": workout}
                    for workout_membership, workout in rows
                ],
            }
        )
    return templates.TemplateResponse(
        request,
        "plans/cycle.html",
        context(
            request,
            active_page="plans",
            cycle=cycle,
            revision=revision,
            weeks=weeks,
            goal_type_label=GOAL_TYPE_LABELS.get(cycle.event_type, cycle.event_type),
            is_accepted=cycle.accepted_revision_id == revision.id,
        ),
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
    cycles = list(
        session.scalars(
            select(TrainingCycle)
            .where(TrainingCycle.user_id == user.id)
            .order_by(TrainingCycle.target_date.desc(), TrainingCycle.id.desc())
        )
    )

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
        proposals_by_day = _proposal_items(session, user.id, calendar_start, calendar_end)
        month_by_day: dict[date, list[CalendarWorkout]] = {
            calendar_start + timedelta(days=offset): []
            for offset in range((calendar_end - calendar_start).days + 1)
        }
        for workout in workouts:
            scheduled_for = workout.scheduled_for
            if scheduled_for is not None and scheduled_for in month_by_day:
                month_by_day[scheduled_for].append(workout)

        month_weeks: list[tuple[int, dict[date, list[CalendarWorkout]]]] = []
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
                plan_proposals_by_day=proposals_by_day,
                cycles=cycles,
                goal_type_labels=GOAL_TYPE_LABELS,
            ),
        )

    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=week)
    sunday = monday + timedelta(days=6)
    workouts = workouts_between(session, user.id, monday, sunday)
    proposals_by_day = _proposal_items(session, user.id, monday, sunday)
    by_day: dict[date, list[CalendarWorkout]] = {
        monday + timedelta(days=offset): [] for offset in range(7)
    }
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
            plan_proposals_by_day=proposals_by_day,
            cycles=cycles,
            goal_type_labels=GOAL_TYPE_LABELS,
        ),
    )
