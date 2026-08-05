from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from app.database import SessionDep, SessionLocal
from app.models import GarminAccount
from app.repositories.users import get_or_create_default_user, get_or_create_garmin_account
from app.services.garmin.client import connect_garmin, message_from_exception
from app.services.garmin.sync import SyncAlreadyRunningError, sync_garmin
from app.web import context, templates

router = APIRouter(prefix="/settings")


def _manual_sync(account_id: int) -> None:
    with SessionLocal() as session:
        account = session.get(GarminAccount, account_id)
        if account is None:
            return
        try:
            sync_garmin(session, account)
        except SyncAlreadyRunningError:
            return


@router.get("", response_class=HTMLResponse)
def settings_page(
    request: Request,
    session: SessionDep,
    error: str | None = None,
) -> HTMLResponse:
    user = get_or_create_default_user(session)
    account = get_or_create_garmin_account(session, user)
    return templates.TemplateResponse(
        request,
        "settings.html",
        context(request, active_page="settings", account=account, error=error),
    )


@router.post("/garmin/connect")
async def connect_account(
    session: SessionDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> RedirectResponse:
    user = get_or_create_default_user(session)
    account = get_or_create_garmin_account(session, user)
    try:
        await run_in_threadpool(connect_garmin, email.strip(), password)
    except Exception as exc:
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


@router.post("/garmin/sync")
def start_sync(
    background_tasks: BackgroundTasks,
    session: SessionDep,
) -> RedirectResponse:
    user = get_or_create_default_user(session)
    account = get_or_create_garmin_account(session, user)
    if account.connected_at is None:
        query = urlencode({"error": "Garmin ist noch nicht verbunden."})
        return RedirectResponse(f"/settings?{query}", status_code=303)
    account.sync_status = "queued"
    session.commit()
    background_tasks.add_task(_manual_sync, account.id)
    return RedirectResponse("/settings", status_code=303)
