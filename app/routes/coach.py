import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from time import monotonic
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.auth import CurrentUser
from app.config import get_settings
from app.database import SessionDep
from app.onboarding import require_data_access
from app.services.analytics.coach_data import CoachDataService
from app.services.training_agent.backend import (
    CoachEvent,
    ConversationTurn,
    TrainingAgent,
    TrainingAgentError,
)
from app.services.training_agent.dependencies import TrainingAgentDep
from app.services.training_agent.log_config import ensure_coach_file_logging
from app.services.training_agent.orchestrator import CoachOrchestrator
from app.services.training_agent.tools import CoachTools
from app.web import context, templates

router = APIRouter(prefix="/coach", dependencies=[Depends(require_data_access)])
logger = logging.getLogger("uvicorn.error")


def _loggable_arguments(arguments: dict[str, object] | None) -> dict[str, object]:
    if not arguments:
        return {}
    allowed = {"activity_id", "days", "metrics", "recent_workouts"}
    return {key: value for key, value in arguments.items() if key in allowed}


class CoachHistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class CoachRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[CoachHistoryItem] = Field(default_factory=list, max_length=6)


def _capabilities(session: SessionDep, user: CurrentUser) -> CoachTools:
    return CoachTools(CoachDataService(session, user.id, user.display_name))


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


async def _collect_answer(agent: TrainingAgent, message: str, capabilities: CoachTools) -> str:
    chunks: list[str] = []
    async for event in CoachOrchestrator(agent).stream(message, capabilities):
        if event.type == "final_response" and event.content:
            if event.replace:
                chunks = [event.content]
            else:
                chunks.append(event.content)
    return "".join(chunks).strip()


@router.post("", response_class=HTMLResponse)
async def ask_coach(
    request: Request,
    agent: TrainingAgentDep,
    capabilities: Annotated[CoachTools, Depends(_capabilities)],
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
            answer = await _collect_answer(agent, message, capabilities)
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


@router.post("/stream")
async def stream_coach(
    payload: CoachRequest,
    agent: TrainingAgentDep,
    capabilities: Annotated[CoachTools, Depends(_capabilities)],
    user: CurrentUser,
) -> StreamingResponse:
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Bitte formuliere eine Frage an den Coach.")
    if agent is None:
        raise HTTPException(
            status_code=503,
            detail="Konfiguriere zuerst OpenRouter, bevor du den Coach fragst.",
        )

    ensure_coach_file_logging(logger)
    run_id = uuid4().hex[:12]

    async def generate() -> AsyncIterator[str]:
        started_at = monotonic()
        completed = False
        logger.info(
            "Coach run started run_id=%s user_id=%s history_turns=%s message_chars=%s",
            run_id,
            user.id,
            len(payload.history),
            len(message),
        )
        try:
            history = tuple(ConversationTurn(item.role, item.content) for item in payload.history)
            async for event in CoachOrchestrator(agent).stream(message, capabilities, history):
                event = replace(event, run_id=run_id)
                if event.type == "tool_started":
                    logger.info(
                        "Coach tool started run_id=%s tool=%s arguments=%s",
                        run_id,
                        event.tool,
                        _loggable_arguments(event.arguments),
                    )
                elif event.type == "tool_result_summary":
                    logger.info(
                        "Coach tool completed run_id=%s tool=%s elapsed_ms=%s",
                        run_id,
                        event.tool,
                        round((monotonic() - started_at) * 1000),
                    )
                elif event.type == "tool_error":
                    logger.warning(
                        "Coach tool failed run_id=%s tool=%s",
                        run_id,
                        event.tool,
                    )
                elif event.type == "waiting":
                    logger.debug(
                        "Coach run waiting run_id=%s phase=%s elapsed_seconds=%s",
                        run_id,
                        event.phase,
                        event.elapsed_seconds,
                    )
                elif event.type == "final_response" and event.done:
                    completed = True
                    logger.info(
                        "Coach run completed run_id=%s elapsed_ms=%s",
                        run_id,
                        round((monotonic() - started_at) * 1000),
                    )
                yield event.as_json_line()
        except asyncio.CancelledError:
            logger.info(
                "Coach run disconnected run_id=%s elapsed_ms=%s",
                run_id,
                round((monotonic() - started_at) * 1000),
            )
            raise
        except TrainingAgentError as exc:
            logger.exception(
                "Coach run failed run_id=%s elapsed_ms=%s",
                run_id,
                round((monotonic() - started_at) * 1000),
            )
            yield CoachEvent("error", str(exc), run_id=run_id, done=True).as_json_line()
        except Exception:
            logger.exception(
                "Unexpected Coach stream failure run_id=%s elapsed_ms=%s",
                run_id,
                round((monotonic() - started_at) * 1000),
            )
            yield CoachEvent(
                "error",
                "Der Coach ist unerwartet abgebrochen. Nutze die Run-ID für die Fehlersuche.",
                run_id=run_id,
                done=True,
            ).as_json_line()
        finally:
            if not completed:
                logger.debug(
                    "Coach run closed without completion run_id=%s elapsed_ms=%s",
                    run_id,
                    round((monotonic() - started_at) * 1000),
                )

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Coach-Run-ID": run_id,
        },
    )
