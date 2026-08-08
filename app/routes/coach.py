from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.auth import CurrentUser
from app.config import get_settings
from app.database import SessionDep
from app.services.training_agent.backend import TrainingAgentError, TrainingSnapshot
from app.services.training_agent.context import build_training_snapshot
from app.services.training_agent.dependencies import TrainingAgentDep
from app.web import context, templates

router = APIRouter(prefix="/coach")


def _training_snapshot(session: SessionDep, user: CurrentUser) -> TrainingSnapshot:
    return build_training_snapshot(session, user.id)


TrainingSnapshotDep = Annotated[TrainingSnapshot, Depends(_training_snapshot)]


@router.get("", response_class=HTMLResponse)
def coach(request: Request, agent: TrainingAgentDep, _: CurrentUser) -> HTMLResponse:
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "coach.html",
        context(
            request,
            active_page="coach",
            configured=agent is not None,
            model=settings.llm_model,
            message="",
            answer=None,
            error=None,
        ),
    )


@router.post("", response_class=HTMLResponse)
async def ask_coach(
    request: Request,
    agent: TrainingAgentDep,
    snapshot: TrainingSnapshotDep,
    message: Annotated[str, Form(max_length=4000)],
) -> HTMLResponse:
    settings = get_settings()
    message = message.strip()
    answer = None
    error = None
    status_code = 200

    if not message:
        error = "Bitte formuliere eine Frage an den Coach."
        status_code = 422
    elif agent is None:
        error = "Konfiguriere zuerst OpenRouter, bevor du den Coach fragst."
        status_code = 503
    else:
        try:
            answer = await agent.respond(message, snapshot)
        except TrainingAgentError as exc:
            error = str(exc)
            status_code = 502

    return templates.TemplateResponse(
        request,
        "coach.html",
        context(
            request,
            active_page="coach",
            configured=agent is not None,
            model=settings.llm_model,
            message=message,
            answer=answer,
            error=error,
        ),
        status_code=status_code,
    )
