from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.auth import CurrentUser
from app.database import SessionDep
from app.jobs.scheduler import queue_account_sync
from app.models import GarminAccount, SyncRun
from app.models.user import utcnow
from app.repositories.users import get_or_create_garmin_account
from app.services.garmin.client import (
    GarminUnavailableError,
    connect_garmin,
    message_from_exception,
)
from app.web import context, templates

router = APIRouter(prefix="/settings")


def _latest_sync(session: SessionDep, user_id: int) -> SyncRun | None:
    return session.scalar(
        select(SyncRun).where(SyncRun.user_id == user_id).order_by(SyncRun.id.desc()).limit(1)
    )


def _sync_view(account: GarminAccount, sync_run: SyncRun | None) -> dict[str, object]:
    active = account.sync_status in {"queued", "running"}
    progress_percent: int | None = None
    duration_seconds: int | None = None
    if sync_run is not None:
        if sync_run.total_items:
            progress_percent = min(round(sync_run.current_item / sync_run.total_items * 100), 100)
        if sync_run.status == "ok":
            progress_percent = 100
        end = sync_run.finished_at or utcnow()
        duration_seconds = max(int((end - sync_run.started_at).total_seconds()), 0)
    return {
        "account": account,
        "sync_run": sync_run,
        "sync_active": active,
        "progress_percent": progress_percent,
        "duration_seconds": duration_seconds,
    }


@router.get("", response_class=HTMLResponse)
def settings_page(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    error: str | None = None,
) -> HTMLResponse:
    account = get_or_create_garmin_account(session, user)
    values = _sync_view(account, _latest_sync(session, user.id))
    return templates.TemplateResponse(
        request,
        "settings.html",
        context(request, active_page="settings", error=error, **values),
    )


@router.get("/sync-status", response_class=HTMLResponse)
def sync_status(request: Request, session: SessionDep, user: CurrentUser) -> HTMLResponse:
    account = get_or_create_garmin_account(session, user)
    return templates.TemplateResponse(
        request,
        "partials/sync_status.html",
        context(request, **_sync_view(account, _latest_sync(session, user.id))),
    )


@router.post("/garmin/connect", response_class=RedirectResponse, status_code=303)
def connect_account(
    session: SessionDep,
    user: CurrentUser,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> RedirectResponse:
    account = get_or_create_garmin_account(session, user)
    try:
        connect_garmin(email.strip(), password, account_id=account.id)
    except GarminUnavailableError as exc:
        account.sync_status = "error"
        account.sync_error = message_from_exception(exc)
        session.commit()
        return RedirectResponse(
            f"/settings?{urlencode({'error': account.sync_error})}", status_code=303
        )
    account.email = email.strip()
    account.connected_at = datetime.now(UTC).replace(tzinfo=None)
    account.sync_status = "connected"
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

    def mark_queued() -> None:
        account.sync_status = "queued"
        session.commit()

    queue_account_sync(account.id, mark_queued)
    return RedirectResponse("/settings", status_code=303)
