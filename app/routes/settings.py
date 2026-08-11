import json
import logging
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from app.auth import CurrentUser
from app.database import SessionDep
from app.jobs.scheduler import queue_account_sync
from app.models import GarminAccount, SyncEvent, SyncRun
from app.models.user import utcnow
from app.onboarding import require_notice_acknowledged
from app.repositories.users import get_or_create_garmin_account
from app.services.garmin.account_data import delete_garmin_data, disconnect_garmin_account
from app.services.garmin.client import (
    GarminMfaExpiredError,
    GarminUnavailableError,
    cancel_garmin_login,
    finish_garmin_login,
    message_from_exception,
    pending_garmin_login,
    start_garmin_login,
)
from app.services.garmin.locks import GarminAccountBusyError
from app.services.garmin.sync import METRIC_LABELS, rate_limit_cooldown_remaining
from app.web import context, templates

router = APIRouter(prefix="/settings", dependencies=[Depends(require_notice_acknowledged)])
GARMIN_MFA_SESSION_KEY = "garmin_mfa_challenge"
logger = logging.getLogger(__name__)


def _latest_sync(session: SessionDep, user_id: int) -> SyncRun | None:
    return session.scalar(
        select(SyncRun).where(SyncRun.user_id == user_id).order_by(SyncRun.id.desc()).limit(1)
    )


def _sync_view(
    session: SessionDep, account: GarminAccount, sync_run: SyncRun | None
) -> dict[str, object]:
    active = account.sync_status in {"queued", "running"}
    progress_percent: int | None = None
    duration_seconds: int | None = None
    sync_events: list[SyncEvent] = []
    current_metrics = [
        {"key": key, "label": label, "status": "pending"}
        for key, label in METRIC_LABELS.items()
        if key
        in {
            "daily_summary",
            "body_battery",
            "sleep",
            "hrv",
            "spo2",
            "vo2max",
            "training_readiness",
            "training_status",
        }
    ]
    if sync_run is not None:
        if sync_run.days_total:
            progress_percent = min(round(sync_run.days_completed / sync_run.days_total * 100), 100)
        if sync_run.status == "ok":
            progress_percent = 100
        end = sync_run.finished_at or utcnow()
        duration_seconds = max(int((end - sync_run.started_at).total_seconds()), 0)
        sync_events = list(
            session.scalars(
                select(SyncEvent)
                .where(SyncEvent.sync_run_id == sync_run.id)
                .order_by(SyncEvent.id.desc())
                .limit(60)
            )
        )
        if sync_run.current_day is not None:
            latest_by_resource: dict[str, SyncEvent] = {}
            for event in sync_events:
                if (
                    event.day == sync_run.current_day
                    and event.resource is not None
                    and event.resource not in latest_by_resource
                ):
                    latest_by_resource[event.resource] = event
            for metric in current_metrics:
                event = latest_by_resource.get(str(metric["key"]))
                if event is not None:
                    metric["status"] = event.status
    return {
        "account": account,
        "sync_run": sync_run,
        "sync_active": active,
        "progress_percent": progress_percent,
        "duration_seconds": duration_seconds,
        "remaining_days": (
            max(sync_run.days_total - sync_run.days_completed, 0) if sync_run else 0
        ),
        "sync_events": sync_events,
        "current_metrics": current_metrics,
        "metric_labels": METRIC_LABELS,
        "cooldown_seconds": rate_limit_cooldown_remaining(session, account),
    }


@router.get("", response_class=HTMLResponse)
def settings_page(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    error: str | None = None,
    notice: str | None = None,
) -> HTMLResponse:
    account = get_or_create_garmin_account(session, user)
    challenge_id = request.session.get(GARMIN_MFA_SESSION_KEY)
    mfa_required = pending_garmin_login(
        challenge_id if isinstance(challenge_id, str) else None,
        account_id=account.id,
        user_id=user.id,
    )
    if not mfa_required:
        request.session.pop(GARMIN_MFA_SESSION_KEY, None)
        if account.sync_status == "mfa_required":
            account.sync_status = "not_connected"
            session.commit()
    values = _sync_view(session, account, _latest_sync(session, user.id))
    return templates.TemplateResponse(
        request,
        "settings.html",
        context(
            request,
            active_page="settings",
            error=error,
            notice=notice,
            mfa_required=mfa_required,
            **values,
        ),
    )


@router.get("/sync-status", response_class=HTMLResponse)
def sync_status(request: Request, session: SessionDep, user: CurrentUser) -> HTMLResponse:
    account = get_or_create_garmin_account(session, user)
    return templates.TemplateResponse(
        request,
        "partials/sync_status.html",
        context(request, **_sync_view(session, account, _latest_sync(session, user.id))),
    )


@router.get("/sync-runs/{sync_run_id}/export", response_class=Response)
def export_sync_run(
    sync_run_id: int,
    session: SessionDep,
    user: CurrentUser,
) -> Response:
    sync_run = session.scalar(
        select(SyncRun).where(SyncRun.id == sync_run_id, SyncRun.user_id == user.id)
    )
    if sync_run is None:
        raise HTTPException(status_code=404, detail="Synchronisierung nicht gefunden")
    events = list(
        session.scalars(
            select(SyncEvent).where(SyncEvent.sync_run_id == sync_run.id).order_by(SyncEvent.id)
        )
    )
    payload = {
        "exported_at": utcnow().isoformat(timespec="seconds") + "Z",
        "sync_run": {
            "id": sync_run.id,
            "user_id": sync_run.user_id,
            "started_at": sync_run.started_at.isoformat(timespec="milliseconds") + "Z",
            "finished_at": (
                sync_run.finished_at.isoformat(timespec="milliseconds") + "Z"
                if sync_run.finished_at
                else None
            ),
            "status": sync_run.status,
            "stage": sync_run.stage,
            "message": sync_run.message,
            "error": sync_run.error,
            "activities_processed": sync_run.activities_processed,
            "activities_total": sync_run.activities_total,
            "activities_synced": sync_run.activities_synced,
            "days_completed": sync_run.days_completed,
            "days_total": sync_run.days_total,
            "health_days_synced": sync_run.health_days_synced,
            "operations_completed": sync_run.operations_completed,
            "operations_total": sync_run.operations_total,
            "current_day": sync_run.current_day.isoformat() if sync_run.current_day else None,
            "current_operation": sync_run.current_operation,
        },
        "events": [
            {
                "id": event.id,
                "created_at": event.created_at.isoformat(timespec="milliseconds") + "Z",
                "level": event.level,
                "category": event.category,
                "status": event.status,
                "resource": event.resource,
                "day": event.day.isoformat() if event.day else None,
                "operation": event.operation,
                "message": event.message,
                "duration_ms": event.duration_ms,
                "record_count": event.record_count,
            }
            for event in events
        ],
    }
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="pacepilot-garmin-sync-{sync_run.id}.json"'
            )
        },
    )


@router.post("/garmin/connect", response_class=RedirectResponse, status_code=303)
def connect_account(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> RedirectResponse:
    account = get_or_create_garmin_account(session, user)
    previous_challenge = request.session.pop(GARMIN_MFA_SESSION_KEY, None)
    cancel_garmin_login(
        previous_challenge if isinstance(previous_challenge, str) else None,
        account_id=account.id,
        user_id=user.id,
    )
    normalized_email = email.strip()
    try:
        challenge_id = start_garmin_login(
            normalized_email,
            password,
            account_id=account.id,
            user_id=user.id,
        )
    except GarminUnavailableError as exc:
        account.sync_status = "error"
        account.sync_error = message_from_exception(exc)
        session.commit()
        return RedirectResponse(
            f"/settings?{urlencode({'error': account.sync_error})}", status_code=303
        )
    if challenge_id is not None:
        request.session[GARMIN_MFA_SESSION_KEY] = challenge_id
        account.sync_status = "mfa_required"
        account.sync_error = None
        session.commit()
        return RedirectResponse("/settings", status_code=303)

    account.email = normalized_email
    account.connected_at = datetime.now(UTC).replace(tzinfo=None)
    account.sync_status = "connected"
    account.sync_error = None
    account.rate_limit_until = None
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/garmin/mfa", response_class=RedirectResponse, status_code=303)
def verify_garmin_mfa(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    code: Annotated[str, Form()],
) -> RedirectResponse:
    account = get_or_create_garmin_account(session, user)
    challenge_id = request.session.get(GARMIN_MFA_SESSION_KEY)
    try:
        email = finish_garmin_login(
            challenge_id if isinstance(challenge_id, str) else None,
            code,
            account_id=account.id,
            user_id=user.id,
        )
    except GarminMfaExpiredError as exc:
        request.session.pop(GARMIN_MFA_SESSION_KEY, None)
        account.sync_status = "error"
        account.sync_error = message_from_exception(exc)
        session.commit()
        return RedirectResponse(
            f"/settings?{urlencode({'error': account.sync_error})}", status_code=303
        )
    except GarminUnavailableError as exc:
        account.sync_status = "mfa_required"
        account.sync_error = message_from_exception(exc)
        session.commit()
        return RedirectResponse(
            f"/settings?{urlencode({'error': account.sync_error})}", status_code=303
        )

    request.session.pop(GARMIN_MFA_SESSION_KEY, None)
    account.email = email
    account.connected_at = datetime.now(UTC).replace(tzinfo=None)
    account.sync_status = "connected"
    account.sync_error = None
    account.rate_limit_until = None
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/garmin/mfa/cancel", response_class=RedirectResponse, status_code=303)
def cancel_garmin_mfa(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
) -> RedirectResponse:
    account = get_or_create_garmin_account(session, user)
    challenge_id = request.session.pop(GARMIN_MFA_SESSION_KEY, None)
    cancel_garmin_login(
        challenge_id if isinstance(challenge_id, str) else None,
        account_id=account.id,
        user_id=user.id,
    )
    account.sync_status = "not_connected"
    account.sync_error = None
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/garmin/sync", response_class=RedirectResponse, status_code=303)
def start_sync(
    session: SessionDep,
    user: CurrentUser,
) -> RedirectResponse:
    account = get_or_create_garmin_account(session, user)
    if account.connected_at is None:
        query = urlencode({"error": "Garmin ist noch nicht verbunden."})
        return RedirectResponse(f"/settings?{query}", status_code=303)
    cooldown = rate_limit_cooldown_remaining(session, account)
    if cooldown:
        query = urlencode(
            {
                "error": (
                    "Garmin begrenzt derzeit die Anfragen. "
                    f"Bitte warte noch etwa {cooldown} Sekunden."
                )
            }
        )
        return RedirectResponse(f"/settings?{query}", status_code=303)

    def mark_queued() -> bool:
        account.sync_status = "queued"
        session.commit()
        return True

    queue_account_sync(account.id, mark_queued)
    return RedirectResponse("/settings", status_code=303)


@router.post("/garmin/disconnect", response_class=RedirectResponse, status_code=303)
def disconnect_account(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
) -> RedirectResponse:
    account = get_or_create_garmin_account(session, user)
    challenge_id = request.session.pop(GARMIN_MFA_SESSION_KEY, None)
    cancel_garmin_login(
        challenge_id if isinstance(challenge_id, str) else None,
        account_id=account.id,
        user_id=user.id,
    )
    try:
        disconnect_garmin_account(session, account)
    except (GarminAccountBusyError, OSError, ValueError) as exc:
        session.rollback()
        query = urlencode({"error": str(exc)})
        return RedirectResponse(f"/settings?{query}", status_code=303)
    query = urlencode(
        {"notice": "Garmin wurde abgemeldet. Deine importierten Daten bleiben erhalten."}
    )
    return RedirectResponse(f"/settings?{query}", status_code=303)


@router.post("/garmin/data/delete", response_class=RedirectResponse, status_code=303)
def delete_account_data(
    session: SessionDep,
    user: CurrentUser,
    confirmation: Annotated[str, Form()],
) -> RedirectResponse:
    if confirmation != "delete":
        query = urlencode({"error": "Die Löschbestätigung fehlt."})
        return RedirectResponse(f"/settings?{query}", status_code=303)

    account = get_or_create_garmin_account(session, user)
    try:
        result = delete_garmin_data(session, account)
    except (GarminAccountBusyError, OSError, ValueError) as exc:
        session.rollback()
        logger.warning(
            "Garmin data deletion blocked",
            extra={
                "garmin_account_id": account.id,
                "sync_user_id": user.id,
                "error_type": type(exc).__name__,
            },
        )
        query = urlencode({"error": str(exc)})
        return RedirectResponse(f"/settings?{query}", status_code=303)
    notice = (
        f"Importierte Garmin-Daten gelöscht: {result.activities} Aktivitäten und "
        f"{result.health_days} Gesundheitstage entfernt. Die Verbindung bleibt aktiv."
    )
    return RedirectResponse(f"/settings?{urlencode({'notice': notice})}", status_code=303)
