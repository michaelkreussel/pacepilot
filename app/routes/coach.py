import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import date, timedelta
from time import monotonic
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session, sessionmaker

from app.auth import CurrentUser
from app.config import get_settings
from app.database import SessionDep
from app.models import CoachMessage
from app.models.user import utcnow
from app.onboarding import require_data_access
from app.repositories.coach import (
    complete_message,
    conversation_messages,
    create_conversation,
    create_message,
    fail_message,
    find_conversation,
    finish_tool_call,
    list_conversations,
    start_tool_call,
)
from app.services.coach.agent import (
    CoachAgent,
    CoachEvent,
    CoachHistoryMessage,
)
from app.services.coach.dependencies import CoachAgentDep
from app.services.coach.tools import CoachRuntimeContext
from app.web import context, templates

router = APIRouter(prefix="/coach", dependencies=[Depends(require_data_access)])
logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_CHARACTERS = 12_000


def _has_active_response(session: Session, messages: Sequence[CoachMessage]) -> bool:
    stale_before = utcnow() - timedelta(minutes=10)
    for message in messages:
        if message.status == "streaming" and message.created_at < stale_before:
            fail_message(session, message.id, interrupted=True)
    session.flush()
    return any(message.status == "streaming" for message in messages)


def _bounded_history(messages: Sequence[CoachMessage]) -> list[CoachHistoryMessage]:
    history: list[CoachHistoryMessage] = []
    characters = 0
    for message in reversed(messages):
        if message.status != "completed" or message.role not in {"user", "assistant"}:
            continue
        if len(history) >= MAX_HISTORY_MESSAGES:
            break
        remaining = MAX_HISTORY_CHARACTERS - characters
        if remaining <= 0:
            break
        content = message.content[-remaining:]
        history.append(CoachHistoryMessage(message.role, content))
        characters += len(content)
    return list(reversed(history))


def _render_coach(
    request: Request,
    session: Session,
    user: CurrentUser,
    agent: object | None,
    conversation_id: int | None,
) -> HTMLResponse:
    conversations = list_conversations(session, user.id)
    selected = None
    messages: list[CoachMessage] = []
    if conversation_id is not None:
        selected = find_conversation(session, user.id, conversation_id)
        if selected is None:
            raise HTTPException(status_code=404, detail="Chat nicht gefunden")
        loaded = conversation_messages(session, user.id, conversation_id)
        messages = loaded or []
    elif conversations:
        selected = conversations[0]
        loaded = conversation_messages(session, user.id, selected.id)
        messages = loaded or []

    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "coach.html",
        context(
            request,
            active_page="coach",
            configured=agent is not None,
            model=settings.llm_model,
            conversations=conversations,
            conversation=selected,
            messages=messages,
        ),
    )


@router.get("", response_class=HTMLResponse)
def coach(
    request: Request, session: SessionDep, agent: CoachAgentDep, user: CurrentUser
) -> HTMLResponse:
    return _render_coach(request, session, user, agent, None)


@router.get("/{conversation_id}", response_class=HTMLResponse)
def coach_conversation(
    conversation_id: int,
    request: Request,
    session: SessionDep,
    agent: CoachAgentDep,
    user: CurrentUser,
) -> HTMLResponse:
    return _render_coach(request, session, user, agent, conversation_id)


@router.post("/conversations")
def new_conversation(session: SessionDep, user: CurrentUser) -> RedirectResponse:
    conversation = create_conversation(session, user.id)
    session.commit()
    return RedirectResponse(f"/coach/{conversation.id}", status_code=303)


@router.post("/{conversation_id}/delete")
def delete_conversation(
    conversation_id: int,
    session: SessionDep,
    user: CurrentUser,
    selected_conversation_id: Annotated[int | None, Form()] = None,
) -> RedirectResponse:
    conversation = find_conversation(session, user.id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Chat nicht gefunden")

    messages = conversation_messages(session, user.id, conversation_id) or []
    if _has_active_response(session, messages):
        raise HTTPException(
            status_code=409,
            detail="Dieser Chat kann während einer laufenden Antwort nicht gelöscht werden.",
        )

    redirect_id = None
    if selected_conversation_id is not None and selected_conversation_id != conversation_id:
        selected = find_conversation(session, user.id, selected_conversation_id)
        redirect_id = selected.id if selected is not None else None

    session.delete(conversation)
    session.commit()
    location = f"/coach/{redirect_id}" if redirect_id is not None else "/coach"
    return RedirectResponse(location, status_code=303)


def _event(event: str, payload: dict[str, object]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


def _persist_tool_event(
    factory: sessionmaker[Session], assistant_message_id: int, event: CoachEvent
) -> None:
    if event.tool_call_id is None:
        return
    with factory() as session:
        if event.type == "tool_started":
            start_tool_call(
                session,
                assistant_message_id,
                call_id=event.tool_call_id,
                tool_name=event.tool_name or "unknown",
                label=event.label or "Trainingsdaten prüfen",
                input_summary=event.summary,
            )
        elif event.type in {"tool_completed", "tool_failed"}:
            finish_tool_call(
                session,
                assistant_message_id,
                event.tool_call_id,
                error_message=(
                    "Daten konnten nicht geladen werden" if event.type == "tool_failed" else None
                ),
            )
        session.commit()


async def _stream_answer(
    *,
    request: Request,
    agent: CoachAgent,
    history: Sequence[CoachHistoryMessage],
    runtime: CoachRuntimeContext,
    assistant_message_id: int,
) -> AsyncIterator[str]:
    started_at = monotonic()
    answer: list[str] = []
    logger.info(
        "AI coach stream started request_id=%s user_id=%s assistant_message_id=%s "
        "history_messages=%s",
        runtime.request_id,
        runtime.user_id,
        assistant_message_id,
        len(history),
    )
    yield _event("run.started", {"message_id": assistant_message_id})
    try:
        async for event in agent.stream(history, runtime):
            if await request.is_disconnected():
                raise asyncio.CancelledError
            if event.type == "answer_delta" and event.text:
                answer.append(event.text)
                yield _event("answer.delta", {"text": event.text})
            elif event.type == "status" and event.text:
                yield _event("status", {"label": event.text})
            elif event.type in {"tool_started", "tool_completed", "tool_failed"}:
                _persist_tool_event(runtime.session_factory, assistant_message_id, event)
                yield _event(
                    event.type.replace("_", "."),
                    {
                        "id": event.tool_call_id or "",
                        "name": event.tool_name or "",
                        "label": event.label or "Trainingsdaten prüfen",
                        "summary": event.summary,
                    },
                )

        content = "".join(answer).strip()
        with runtime.session_factory() as session:
            complete_message(session, assistant_message_id, content)
            session.commit()
        logger.info(
            "AI coach stream completed request_id=%s user_id=%s assistant_message_id=%s "
            "duration_ms=%s answer_characters=%s",
            runtime.request_id,
            runtime.user_id,
            assistant_message_id,
            round((monotonic() - started_at) * 1000),
            len(content),
        )
        yield _event("answer.completed", {"message_id": assistant_message_id})
    except asyncio.CancelledError:
        with runtime.session_factory() as session:
            fail_message(session, assistant_message_id, interrupted=True)
            session.commit()
        logger.warning(
            "AI coach stream interrupted request_id=%s user_id=%s assistant_message_id=%s "
            "duration_ms=%s",
            runtime.request_id,
            runtime.user_id,
            assistant_message_id,
            round((monotonic() - started_at) * 1000),
        )
        raise
    except Exception as exc:
        with runtime.session_factory() as session:
            fail_message(session, assistant_message_id)
            session.commit()
        logger.exception(
            "AI coach stream failed request_id=%s user_id=%s assistant_message_id=%s "
            "error_type=%s duration_ms=%s",
            runtime.request_id,
            runtime.user_id,
            assistant_message_id,
            type(exc).__name__,
            round((monotonic() - started_at) * 1000),
        )
        raise


@router.post("/{conversation_id}/messages")
async def ask_coach(
    conversation_id: int,
    request: Request,
    session: SessionDep,
    agent: CoachAgentDep,
    user: CurrentUser,
    message: Annotated[str, Form(max_length=4000)],
) -> StreamingResponse:
    message = message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Bitte formuliere eine Frage an den Coach.")

    conversation = find_conversation(session, user.id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Chat nicht gefunden")
    if agent is None:
        raise HTTPException(
            status_code=503,
            detail="Konfiguriere zuerst OpenRouter, bevor du den Coach fragst.",
        )
    existing_messages = conversation_messages(session, user.id, conversation_id) or []
    if _has_active_response(session, existing_messages):
        raise HTTPException(status_code=409, detail="In diesem Chat läuft bereits eine Antwort.")

    if conversation.title == "Neuer Chat":
        conversation.title = message[:157] + ("..." if len(message) > 157 else "")
    create_message(session, conversation, role="user", content=message)
    assistant = create_message(
        session,
        conversation,
        role="assistant",
        status="streaming",
        model_id=get_settings().llm_model,
    )
    session.commit()

    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)
    runtime = CoachRuntimeContext(
        user_id=user.id,
        as_of=date.today(),
        session_factory=factory,
        request_id=request.state.request_id,
    )
    history = [*_bounded_history(existing_messages), CoachHistoryMessage("user", message)]
    return StreamingResponse(
        _stream_answer(
            request=request,
            agent=agent,
            history=history,
            runtime=runtime,
            assistant_message_id=assistant.id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
