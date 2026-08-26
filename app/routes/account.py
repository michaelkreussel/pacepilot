from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.auth import CurrentUser
from app.database import SessionDep
from app.services.account_lifecycle import (
    AccountExportBusyError,
    create_user_export,
    delete_user_account,
    remove_export,
)
from app.services.garmin.locks import GarminAccountBusyError

router = APIRouter(prefix="/account")
DELETE_CONFIRMATION = "KONTO LÖSCHEN"


@router.get("/export", response_class=FileResponse)
def export_account_data(
    background_tasks: BackgroundTasks,
    session: SessionDep,
    user: CurrentUser,
) -> FileResponse:
    try:
        export_path = create_user_export(session, user)
    except (AccountExportBusyError, GarminAccountBusyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background_tasks.add_task(remove_export, export_path)
    return FileResponse(
        export_path,
        media_type="application/zip",
        filename="pacepilot-datenexport.zip",
        background=background_tasks,
    )


@router.post("/delete", response_class=RedirectResponse, status_code=303)
def delete_account(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    confirmation: Annotated[str, Form()],
) -> RedirectResponse:
    if confirmation.strip() != DELETE_CONFIRMATION:
        return RedirectResponse(
            "/settings?error=Die+Best%C3%A4tigung+stimmt+nicht.",
            status_code=303,
        )
    try:
        delete_user_account(session, user)
    except GarminAccountBusyError:
        return RedirectResponse(
            "/settings?error=Eine+Garmin-Operation+l%C3%A4uft+noch.",
            status_code=303,
        )
    request.session.clear()
    return RedirectResponse("/login?account_deleted=1", status_code=303)
