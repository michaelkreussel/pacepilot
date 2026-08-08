from contextlib import suppress
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.auth import CurrentUser
from app.database import SessionDep
from app.models import Activity, DailyHealth, Workout
from app.repositories.health import recent_health
from app.repositories.users import get_or_create_garmin_account
from app.services.analytics.training_load import calculate_weekly_load
from app.services.garmin.sync import SyncAlreadyRunningError, refresh_daily_summary
from app.web import context, templates

router = APIRouter()


def _today_health(session: SessionDep, user_id: int) -> DailyHealth | None:
    return session.scalar(
        select(DailyHealth).where(DailyHealth.user_id == user_id, DailyHealth.day == date.today())
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: SessionDep, user: CurrentUser) -> HTMLResponse:
    account = get_or_create_garmin_account(session, user)
    latest_activity = session.scalar(
        select(Activity)
        .where(Activity.user_id == user.id)
        .order_by(Activity.started_at.desc())
        .limit(1)
    )
    latest_health = _today_health(session, user.id)
    upcoming = session.scalar(
        select(Workout)
        .options(selectinload(Workout.steps))
        .where(Workout.user_id == user.id, Workout.scheduled_for >= date.today())
        .order_by(Workout.scheduled_for)
        .limit(1)
    )
    health = recent_health(session, user.id, 14)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        context(
            request,
            active_page="dashboard",
            user=user,
            account=account,
            latest_activity=latest_activity,
            latest_health=latest_health,
            upcoming=upcoming,
            health=health,
            weekly_load=calculate_weekly_load(session, user.id),
        ),
    )


@router.get("/health", response_class=HTMLResponse)
def health_cards(request: Request, session: SessionDep, user: CurrentUser) -> HTMLResponse:
    latest_health = _today_health(session, user.id)
    return templates.TemplateResponse(
        request,
        "partials/health_cards.html",
        context(
            request,
            latest_health=latest_health,
            weekly_load=calculate_weekly_load(session, user.id),
        ),
    )


@router.post("/health", response_class=HTMLResponse)
def refresh_health_cards(request: Request, session: SessionDep, user: CurrentUser) -> HTMLResponse:
    account = get_or_create_garmin_account(session, user)
    if account.connected_at is not None:
        with suppress(SyncAlreadyRunningError):
            refresh_daily_summary(session, user.id)
    return templates.TemplateResponse(
        request,
        "partials/health_cards.html",
        context(
            request,
            latest_health=_today_health(session, user.id),
            weekly_load=calculate_weekly_load(session, user.id),
        ),
    )
