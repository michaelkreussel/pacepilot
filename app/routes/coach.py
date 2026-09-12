import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict
from datetime import date
from time import monotonic
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.auth import CurrentUser
from app.config import get_settings
from app.database import SessionDep
from app.models import CoachMessage
from app.onboarding import require_data_access
from app.repositories.coach import (
    complete_message,
    conversation_message_page,
    conversation_messages,
    create_conversation,
    fail_message,
    find_assistant_message,
    find_conversation,
    list_conversations,
)
from app.services.coach import COACH_PROMPT_TEMPLATE_VERSION, COACH_TOOL_CONTRACT_VERSION
from app.services.coach.agent import CoachProviderError
from app.services.coach.conversation import (
    ActiveResponseConflictError,
    CoachHistoryMessage,
    CoachRuntimeContext,
    prepare_execution,
    repair_stale_responses,
)
from app.services.coach.dependencies import (
    CoachAgentFactory,
    CoachAgentFactoryDep,
    CoachProviderConfiguredDep,
)
from app.services.coach.presentation import (
    PlanArtifactCard,
    PlanningArtifactPresentation,
    WorkoutArtifactPresentation,
    plan_artifact_presentations,
    planning_artifact_presentations,
    workout_artifact_presentation,
    workout_artifact_presentations,
)
from app.services.planning.daily_recommendation import (
    DailyRecommendation,
    recommend_today,
    save_recommendation,
)
from app.services.planning.planning_commands import (
    GoalUpdateInput,
    PlanningInputCommandError,
    PlanningInputCommands,
    ReferencedGoalChangeConfirmation,
)
from app.services.planning.workout_service import WorkoutServiceError
from app.web import context, templates

router = APIRouter(prefix="/coach", dependencies=[Depends(require_data_access)])
logger = logging.getLogger(__name__)


def _proposal_card(
    session: Session, user_id: int, conversation_id: int, message: CoachMessage
) -> WorkoutArtifactPresentation | None:
    return workout_artifact_presentation(session, user_id, conversation_id, message.id)


def _proposal_cards(
    session: Session, user_id: int, messages: Sequence[CoachMessage]
) -> dict[int, WorkoutArtifactPresentation]:
    return workout_artifact_presentations(session, user_id, messages)


def _message_html(
    request: Request,
    item: CoachMessage,
    card: WorkoutArtifactPresentation | None,
    planning_artifacts: Sequence[PlanningArtifactPresentation] = (),
    plan_artifacts: Sequence[PlanArtifactCard] = (),
    *,
    message_state: str | None = None,
) -> str:
    values = context(
        request,
        item=item,
        card=card,
        planning_artifacts=planning_artifacts,
        plan_artifacts=plan_artifacts,
    )
    if message_state is not None:
        values["message_state"] = message_state
    return templates.get_template("coach/_message.html").render(values)


def _render_coach(
    request: Request,
    session: Session,
    user: CurrentUser,
    configured: bool,
    conversation_id: int | None,
    *,
    message_before: int | None = None,
    status_code: int = 200,
    today: DailyRecommendation | None = None,
    available_minutes: int | None = None,
    recommendation_notice: str | None = None,
) -> HTMLResponse:
    conversations = list_conversations(session, user.id)
    selected = None
    messages: list[CoachMessage] = []
    older_messages_before = None
    if conversation_id is not None:
        selected = find_conversation(session, user.id, conversation_id)
        if selected is None:
            raise HTTPException(status_code=404, detail="Chat nicht gefunden")
        if repair_stale_responses(session, user.id, conversation_id):
            session.commit()
        page = conversation_message_page(session, user.id, conversation_id, before=message_before)
        messages = list(page.messages)
        older_messages_before = page.older_before
    elif conversations:
        selected = conversations[0]
        if repair_stale_responses(session, user.id, selected.id):
            session.commit()
        page = conversation_message_page(session, user.id, selected.id)
        messages = list(page.messages)
        older_messages_before = page.older_before

    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "coach.html",
        context(
            request,
            active_page="coach",
            configured=configured,
            today=today
            or recommend_today(
                session, user, as_of=date.today(), available_minutes=available_minutes
            ),
            recommendation_minutes=available_minutes,
            recommendation_notice=recommendation_notice,
            model=settings.llm_model,
            conversations=conversations,
            conversation=selected,
            messages=messages,
            older_messages_before=older_messages_before,
            viewing_older_messages=message_before is not None,
            proposal_cards=(_proposal_cards(session, user.id, messages) if selected else {}),
            planning_artifact_cards=(
                planning_artifact_presentations(session, user.id, messages) if selected else {}
            ),
            plan_artifact_cards=(
                plan_artifact_presentations(session, user.id, messages) if selected else {}
            ),
        ),
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def coach(
    request: Request,
    session: SessionDep,
    configured: CoachProviderConfiguredDep,
    user: CurrentUser,
    available_minutes: Annotated[int | None, Query(ge=0, le=1440)] = None,
) -> HTMLResponse:
    return _render_coach(
        request, session, user, configured, None, available_minutes=available_minutes
    )


@router.get("/today")
def today_preview(
    session: SessionDep,
    user: CurrentUser,
    available_minutes: Annotated[int | None, Query(ge=0, le=1440)] = None,
) -> dict[str, object]:
    return recommend_today(
        session, user, as_of=date.today(), available_minutes=available_minutes
    ).summary()


@router.post("/today/save")
def save_today(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    configured: CoachProviderConfiguredDep,
    context_fingerprint: Annotated[str, Form(min_length=64, max_length=64)],
    available_minutes: Annotated[int | None, Form(ge=0, le=1440)] = None,
):
    try:
        result = save_recommendation(
            session,
            user,
            as_of=date.today(),
            expected_fingerprint=context_fingerprint,
            available_minutes=available_minutes,
        )
    except WorkoutServiceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(result, DailyRecommendation):
        return _render_coach(
            request,
            session,
            user,
            configured,
            None,
            today=result,
            available_minutes=available_minutes,
            status_code=409,
            recommendation_notice=(
                "Deine Empfehlung hat sich geändert. Bitte prüfe die aktuelle Vorschau."
            ),
        )
    return RedirectResponse(f"/workouts/{result.id}", status_code=303)


@router.get("/{conversation_id:int}", response_class=HTMLResponse)
def coach_conversation(
    conversation_id: int,
    request: Request,
    session: SessionDep,
    configured: CoachProviderConfiguredDep,
    user: CurrentUser,
    before: Annotated[int | None, Query(gt=0)] = None,
) -> HTMLResponse:
    return _render_coach(
        request,
        session,
        user,
        configured,
        conversation_id,
        message_before=before,
    )


@router.post("/conversations")
def new_conversation(session: SessionDep, user: CurrentUser) -> RedirectResponse:
    conversation = create_conversation(session, user.id)
    session.commit()
    return RedirectResponse(f"/coach/{conversation.id}", status_code=303)


@router.get(
    "/{conversation_id}/messages/{assistant_message_id}/proposal-card",
    response_class=HTMLResponse,
)
def proposal_card(
    conversation_id: int,
    assistant_message_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
) -> HTMLResponse:
    card = workout_artifact_presentation(session, user.id, conversation_id, assistant_message_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Vorschlag nicht gefunden")
    return templates.TemplateResponse(
        request,
        "workouts/_coach_proposal_card.html",
        context(request, card=card),
    )


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

    repair_stale_responses(session, user.id, conversation_id)
    messages = conversation_messages(session, user.id, conversation_id) or []
    if any(message.status == "streaming" for message in messages):
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


@router.post("/{conversation_id}/messages/{message_id}/planning-goal-confirmation")
def confirm_planning_goal_change(
    conversation_id: int,
    message_id: int,
    session: SessionDep,
    user: CurrentUser,
) -> RedirectResponse:
    message = find_assistant_message(session, user.id, conversation_id, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Zieländerung nicht gefunden")
    artifact_index = next(
        (
            index
            for index, artifact in enumerate(message.artifacts_json)
            if artifact.get("resource") == "goal"
            and artifact.get("status") == "confirmation_required"
        ),
        None,
    )
    if artifact_index is None:
        raise HTTPException(status_code=409, detail="Diese Zieländerung ist nicht mehr offen.")
    artifact = message.artifacts_json[artifact_index]
    request_payload = artifact.get("request")
    confirmation_payload = artifact.get("confirmation")
    operation = artifact.get("operation")
    if not isinstance(request_payload, dict) or not isinstance(confirmation_payload, dict):
        raise HTTPException(status_code=409, detail="Die Zielbestätigung ist unvollständig.")
    try:
        confirmation = ReferencedGoalChangeConfirmation.model_validate(confirmation_payload)
        commands = PlanningInputCommands(session, user, as_of=date.today(), commit=False)
        if operation == "update_planning_goal":
            goal_id = request_payload.get("goal_id")
            changes = request_payload.get("changes")
            if not isinstance(goal_id, int) or not isinstance(changes, dict):
                raise ValueError("invalid goal update confirmation")
            fact = commands.update_goal(
                goal_id,
                GoalUpdateInput.model_validate(changes),
                confirmation=confirmation,
            )
        elif operation == "deactivate_planning_goal":
            goal_id = request_payload.get("goal_id")
            if not isinstance(goal_id, int):
                raise ValueError("invalid goal deactivation confirmation")
            fact = commands.deactivate_goal(goal_id, confirmation=confirmation)
        else:
            raise ValueError("unsupported goal confirmation operation")
    except (PlanningInputCommandError, ValidationError, ValueError) as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Die Zielbestätigung ist veraltet oder ungültig.",
        ) from exc
    result = asdict(fact)
    if fact.target_date is not None:
        result["target_date"] = fact.target_date.isoformat()
    updated_artifact = {
        "type": "planning_input",
        "resource": "goal",
        "operation": operation,
        "result": result,
    }
    artifacts = list(message.artifacts_json)
    artifacts[artifact_index] = updated_artifact
    message.artifacts_json = artifacts
    session.commit()
    return RedirectResponse(f"/coach/{conversation_id}", status_code=303)


def _artifact_event_payload(runtime: CoachRuntimeContext) -> dict[str, object]:
    if runtime.conversation_id is None or runtime.assistant_message_id is None:
        raise RuntimeError("Proposal event has no assistant message")
    with runtime.session_factory() as session:
        card = workout_artifact_presentation(
            session,
            runtime.user_id,
            runtime.conversation_id,
            runtime.assistant_message_id,
        )
    if card is None:
        raise RuntimeError("Proposal tool completed without a persisted artifact")
    return {
        "workout_id": card.workout_id,
        "source_message_id": card.source_assistant_message_id,
        "card_url": (
            f"/coach/{runtime.conversation_id}/messages/"
            f"{card.source_assistant_message_id}/proposal-card"
        ),
    }


async def _stream_answer(
    *,
    request: Request,
    agent_factory: CoachAgentFactory,
    history: Sequence[CoachHistoryMessage],
    runtime: CoachRuntimeContext,
    assistant_message_id: int,
    conversation_title: str,
) -> AsyncIterator[str]:
    started_at = monotonic()
    answer: list[str] = []
    proposal_emitted = False
    failure_category = "internal_error"
    if runtime.conversation_id is None or runtime.user_message_id is None:
        raise RuntimeError("Coach stream context is incomplete")
    conversation_id = runtime.conversation_id
    logger.info(
        "AI coach stream started request_id=%s user_id=%s assistant_message_id=%s "
        "history_messages=%s",
        runtime.request_id,
        runtime.user_id,
        assistant_message_id,
        len(history),
    )
    try:
        with runtime.session_factory() as session:
            user_message = session.get(CoachMessage, runtime.user_message_id)
            assistant_message = session.get(CoachMessage, assistant_message_id)
            if user_message is None or assistant_message is None:
                raise RuntimeError("Coach stream messages are missing")
            card = _proposal_card(session, runtime.user_id, conversation_id, assistant_message)
            started_payload: dict[str, object] = {
                "message_id": assistant_message_id,
                "conversation_title": conversation_title,
                "user_html": _message_html(request, user_message, None),
                "assistant_html": _message_html(request, assistant_message, card),
                "failure_html": _message_html(
                    request, assistant_message, card, message_state="interrupted"
                ),
            }
        yield _event("answer.started", started_payload)
        failure_category = "provider_error"
        agent = agent_factory()
        async for event in agent.stream(history, runtime):
            if await request.is_disconnected():
                raise asyncio.CancelledError
            if event.type == "answer_text" and event.text:
                answer.append(event.text)
                yield _event("answer.delta", {"text": event.text})
            elif event.type == "artifact_available" and event.artifact_type == "workout":
                if not proposal_emitted:
                    failure_category = "internal_error"
                    yield _event("artifact.available", _artifact_event_payload(runtime))
                    proposal_emitted = True
                    failure_category = "provider_error"
            elif event.type == "completed":
                content = "".join(answer).strip()
                if not content:
                    failure_category = "missing_final_answer"
                    raise CoachProviderError("Coach provider completed without answer text")
                failure_category = "internal_error"
                with runtime.session_factory() as session:
                    complete_message(session, assistant_message_id, content)
                    session.commit()
                    assistant_message = session.get(CoachMessage, assistant_message_id)
                    if assistant_message is None:
                        raise RuntimeError("Completed Coach message is missing")
                    card = _proposal_card(
                        session, runtime.user_id, conversation_id, assistant_message
                    )
                    planning_artifacts = planning_artifact_presentations(
                        session, runtime.user_id, [assistant_message]
                    ).get(assistant_message.id, ())
                    plan_artifacts = plan_artifact_presentations(
                        session, runtime.user_id, [assistant_message]
                    ).get(assistant_message.id, ())
                    completed_html = _message_html(
                        request, assistant_message, card, planning_artifacts, plan_artifacts
                    )
                logger.info(
                    "AI coach stream completed request_id=%s user_id=%s assistant_message_id=%s "
                    "duration_ms=%s answer_characters=%s",
                    runtime.request_id,
                    runtime.user_id,
                    assistant_message_id,
                    round((monotonic() - started_at) * 1000),
                    len(content),
                )
                yield _event(
                    "answer.completed",
                    {"message_id": assistant_message_id, "html": completed_html},
                )
                return
            elif event.type == "failed":
                failure_category = (
                    "missing_final_answer"
                    if event.failure_category == "missing_final_answer"
                    else "provider_error"
                )
                raise CoachProviderError("Coach provider execution failed")
        failure_category = "provider_error"
        raise CoachProviderError("Coach provider stream ended without completion")
    except asyncio.CancelledError:
        with runtime.session_factory() as session:
            fail_message(session, assistant_message_id, failure_category="interrupted")
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
            fail_message(
                session,
                assistant_message_id,
                failure_category=failure_category,
            )
            session.commit()
            assistant_message = session.get(CoachMessage, assistant_message_id)
            if assistant_message is None:
                raise RuntimeError("Failed Coach message is missing") from exc
            card = _proposal_card(session, runtime.user_id, conversation_id, assistant_message)
            planning_artifacts = planning_artifact_presentations(
                session, runtime.user_id, [assistant_message]
            ).get(assistant_message.id, ())
            plan_artifacts = plan_artifact_presentations(
                session, runtime.user_id, [assistant_message]
            ).get(assistant_message.id, ())
            failed_html = _message_html(
                request, assistant_message, card, planning_artifacts, plan_artifacts
            )
        logger.warning(
            "AI coach stream failed request_id=%s user_id=%s assistant_message_id=%s "
            "failure_category=%s duration_ms=%s",
            runtime.request_id,
            runtime.user_id,
            assistant_message_id,
            failure_category,
            round((monotonic() - started_at) * 1000),
        )
        yield _event("answer.failed", {"message_id": assistant_message_id, "html": failed_html})


@router.post("/{conversation_id}/messages")
async def ask_coach(
    conversation_id: int,
    request: Request,
    session: SessionDep,
    agent_factory: CoachAgentFactoryDep,
    user: CurrentUser,
    message: Annotated[str, Form(max_length=4000)],
) -> StreamingResponse:
    message = message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Bitte formuliere eine Frage an den Coach.")

    conversation = find_conversation(session, user.id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Chat nicht gefunden")
    if agent_factory is None:
        raise HTTPException(
            status_code=503,
            detail="Konfiguriere zuerst OpenRouter, bevor du den Coach fragst.",
        )
    existing_messages = conversation_messages(session, user.id, conversation_id) or []
    try:
        execution = prepare_execution(
            session,
            conversation,
            existing_messages,
            user_id=user.id,
            question=message,
            model_id=get_settings().llm_model,
            request_id=request.state.request_id,
            prompt_template_version=COACH_PROMPT_TEMPLATE_VERSION,
            operation_contract_version=COACH_TOOL_CONTRACT_VERSION,
            as_of=date.today(),
        )
    except ActiveResponseConflictError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="In diesem Chat läuft bereits eine Antwort."
        ) from exc
    session.commit()

    return StreamingResponse(
        _stream_answer(
            request=request,
            agent_factory=agent_factory,
            history=execution.history,
            runtime=execution.runtime,
            assistant_message_id=execution.assistant_message_id,
            conversation_title=conversation.title,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
