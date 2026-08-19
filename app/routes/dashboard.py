from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from app.auth import CurrentUser
from app.database import SessionDep
from app.models import Activity, DailyHealth, Workout
from app.onboarding import onboarding_state
from app.repositories.users import get_or_create_garmin_account
from app.services.analytics import AthleteDataService
from app.services.analytics.health_trends import preferred_readiness
from app.services.analytics.training_load import calculate_weekly_load
from app.web import context, templates

router = APIRouter()

READINESS_LABELS = {"low": "Niedrig", "fair": "Solide", "good": "Gut", "high": "Hoch"}
GARMIN_READINESS_LABELS = {
    "LOW": "Niedrig",
    "POOR": "Niedrig",
    "FAIR": "Solide",
    "MODERATE": "Solide",
    "GOOD": "Gut",
    "HIGH": "Hoch",
    "PRIME": "Sehr hoch",
}
READINESS_GUIDANCE = {
    "low": "Deine Signale sprechen heute eher für Erholung oder eine sehr lockere Einheit.",
    "fair": "Trainiere bewusst und passe die Belastung an dein Körpergefühl an.",
    "good": "Deine Erholung wirkt stabil. Du kannst dein Training wie geplant angehen.",
    "high": "Deine Signale sprechen für eine hohe Belastbarkeit.",
}


def _today_health(session: SessionDep, user_id: int) -> DailyHealth | None:
    return session.scalar(
        select(DailyHealth).where(DailyHealth.user_id == user_id, DailyHealth.day == date.today())
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: SessionDep, user: CurrentUser) -> Response:
    if not onboarding_state(user).complete:
        return RedirectResponse("/onboarding", status_code=303)
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
        .where(Workout.user_id == user.id, Workout.scheduled_for >= date.today())
        .order_by(Workout.scheduled_for)
        .limit(1)
    )
    recovery = AthleteDataService(session, user.id).get_current_recovery_state()
    readiness = preferred_readiness(recovery)
    readiness_tone = (
        "high"
        if readiness and readiness.score >= 80
        else "good"
        if readiness and readiness.score >= 65
        else "fair"
        if readiness and readiness.score >= 45
        else "low"
    )
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
            weekly_load=calculate_weekly_load(session, user.id),
            recovery=recovery,
            readiness=readiness,
            readiness_tone=readiness_tone,
            readiness_label=(
                GARMIN_READINESS_LABELS.get(readiness.label or "", readiness.label)
                if readiness and readiness.source == "garmin"
                else READINESS_LABELS.get(readiness.label or "")
                if readiness
                else None
            ),
            readiness_guidance=READINESS_GUIDANCE.get(
                readiness_tone,
                "Sobald genügend Gesundheitsdaten vorliegen, ordnet PacePilot deinen Tag ein.",
            ),
        ),
    )
