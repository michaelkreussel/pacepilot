import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import date, timedelta
from time import monotonic
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.auth import CurrentUser
from app.config import (
    DEFERRED_QUALITY_TEMPLATE_IDS,
    coach_feature_enabled,
    deferred_quality_templates_enabled,
    get_settings,
)
from app.database import SessionDep
from app.models import CoachMessage
from app.models.user import utcnow
from app.onboarding import require_data_access
from app.repositories.coach import (
    complete_message,
    conversation_messages,
    create_conversation,
    fail_message,
    find_conversation,
    list_conversations,
)
from app.services.coach.agent import CoachProviderError
from app.services.coach.conversation import (
    CoachHistoryMessage,
    CoachRuntimeContext,
    prepare_execution,
)
from app.services.coach.dependencies import (
    CoachAgentFactory,
    CoachAgentFactoryDep,
    CoachProviderConfiguredDep,
)
from app.services.coach.presentation import (
    WorkoutArtifactPresentation,
    workout_artifact_presentation,
)
from app.services.planning.registry import registered_workout_formats
from app.services.planning.weekly_planner import (
    WeeklyPlanCandidate,
    WeeklyPlannerError,
    plan_shadow_week,
)
from app.services.planning.workout_proposals import (
    RunningProposalRequest,
    RunningProposalService,
    WorkoutProposalError,
)
from app.services.planning.workout_service import WorkoutTransitionError
from app.services.planning.workout_templates import TemplateExpansionError
from app.services.planning.workout_views import GOAL_TYPE_LABELS, PLAN_ROLE_LABELS
from app.web import context, templates

router = APIRouter(prefix="/coach", dependencies=[Depends(require_data_access)])
logger = logging.getLogger(__name__)


def _proposal_card(
    session: Session, user_id: int, conversation_id: int, message: CoachMessage
) -> WorkoutArtifactPresentation | None:
    run = message.generated_run
    if run is None:
        return None
    return workout_artifact_presentation(session, user_id, conversation_id, run.id)


def _proposal_cards(
    session: Session, user_id: int, conversation_id: int, messages: Sequence[CoachMessage]
) -> dict[int, WorkoutArtifactPresentation]:
    cards: dict[int, WorkoutArtifactPresentation] = {}
    for message in messages:
        card = _proposal_card(session, user_id, conversation_id, message)
        if card is not None:
            cards[message.id] = card
    return cards


def _message_html(
    request: Request,
    item: CoachMessage,
    card: WorkoutArtifactPresentation | None,
    *,
    message_state: str | None = None,
) -> str:
    values = context(request, item=item, card=card)
    if message_state is not None:
        values["message_state"] = message_state
    return templates.get_template("coach/_message.html").render(values)


def _has_active_response(session: Session, messages: Sequence[CoachMessage]) -> bool:
    stale_before = utcnow() - timedelta(minutes=10)
    for message in messages:
        if message.status == "streaming" and message.created_at < stale_before:
            fail_message(session, message.id, interrupted=True)
    session.flush()
    return any(message.status == "streaming" for message in messages)


def _render_coach(
    request: Request,
    session: Session,
    user: CurrentUser,
    configured: bool,
    conversation_id: int | None,
    *,
    proposal_error: str | None = None,
    proposal_date: str | None = None,
    proposal_minutes: str = "45",
    proposal_template_id: str = "easy_run",
    status_code: int = 200,
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
            configured=configured,
            model=settings.llm_model,
            conversations=conversations,
            conversation=selected,
            messages=messages,
            proposal_cards=(
                _proposal_cards(session, user.id, selected.id, messages) if selected else {}
            ),
            today=date.today(),
            proposal_error=proposal_error,
            proposal_date=proposal_date or date.today().isoformat(),
            proposal_minutes=proposal_minutes,
            proposal_template_id=proposal_template_id,
            proposal_templates=[
                {"id": template.id, "label": template.name}
                for template in registered_workout_formats()
                if template.id not in DEFERRED_QUALITY_TEMPLATE_IDS
                or deferred_quality_templates_enabled()
            ],
            proposal_idempotency_key=str(uuid4()),
        ),
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def coach(
    request: Request,
    session: SessionDep,
    configured: CoachProviderConfiguredDep,
    user: CurrentUser,
) -> HTMLResponse:
    return _render_coach(request, session, user, configured, None)


WEEKDAY_LABELS = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)
SKIP_REASON_LABELS = {
    None: "Kein verfügbarer Tag passt für den Langen Lauf",
    "planner.budget_below_easy_minimum": "Zeitbudget unter dem Minimum für einen lockeren Lauf",
    "planner.budget_below_quality_requirement": "Zeitbudget reicht für Steigerungen nicht aus",
    "planner.quality_spacing_violation": "Zu geringer Abstand zu einem Qualitätsreiz",
    "planner.no_measurable_long_run_history": "Keine messbare Long-Run-Historie",
    "planner.long_run_below_template_minimum_after_history_bound": (
        "Long-Run-Grenze liegt unter dem Template-Minimum"
    ),
    "planner.long_run_not_placeable": "Kein verfügbarer Tag passt für den Langen Lauf",
    "planner.long_run_requires_consistent_running_weeks": (
        "Für den Langen Lauf fehlt eine konsistente Laufbasis"
    ),
    "planner.strides_not_eligible": "Steigerungen sind aktuell nicht freigegeben",
}

WARNING_LABELS = {
    "planner.long_run_above_typical_weekly_longest": (
        "Länger als dein typisch längster Wochenlauf"
    ),
    "planner.strides_adjacent_to_long_run": "Steigerungen direkt neben dem Langen Lauf",
    "planner.deferred_quality_development_override": (
        "Development-Testvorlage: vor Annahme besonders sorgfältig prüfen"
    ),
}

CONFIDENCE_LABELS = {
    "high": "Hoch",
    "medium": "Mittel",
    "low": "Niedrig",
    "insufficient": "Unzureichend",
}


@router.get("/planning-shadow", response_class=HTMLResponse)
def planning_shadow(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    week: str | None = None,
) -> HTMLResponse:
    if not coach_feature_enabled(get_settings().coach_plan_generation_enabled, user.id):
        raise HTTPException(status_code=404, detail="Seite nicht gefunden")
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    error: str | None = None
    if week is not None:
        try:
            parsed = date.fromisoformat(week)
        except ValueError:
            error = "Ungültiges Wochendatum."
        else:
            if parsed.weekday() != 0:
                error = "Die Woche muss an einem Montag beginnen."
            else:
                week_start = parsed
    candidate: WeeklyPlanCandidate | None = None
    try:
        candidate = plan_shadow_week(session, user, week_start=week_start)
    except WeeklyPlannerError as exc:
        error = str(exc)

    def context_section(name: str) -> dict[str, object]:
        if candidate is None:
            return {}
        value = candidate.generation_context.get(name)
        return value if isinstance(value, dict) else {}

    sessions_by_weekday = {item.weekday: item for item in candidate.sessions} if candidate else {}
    skipped_by_weekday = (
        {skip.weekday: skip.reason_code for skip in candidate.skipped_days} if candidate else {}
    )
    raw_availability = candidate.generation_context.get("availability") if candidate else None
    availability_rows = (
        [item for item in raw_availability if isinstance(item, dict)]
        if isinstance(raw_availability, list)
        else []
    )
    availability_by_weekday = {
        int(item["weekday"]) for item in availability_rows if "weekday" in item
    }
    baseline_context = context_section("baseline")
    intensity_context = context_section("intensity")
    profile_context = context_section("profile")
    goals_context: list[dict[str, object]] = []
    if candidate is not None:
        raw_goals = candidate.generation_context.get("goals")
        if isinstance(raw_goals, list):
            goals_context = [goal for goal in raw_goals if isinstance(goal, dict)]
    week_days = []
    for weekday, label in enumerate(WEEKDAY_LABELS):
        session_candidate = sessions_by_weekday.get(weekday)
        raw_skip = skipped_by_weekday.get(weekday)
        skip_code = raw_skip if isinstance(raw_skip, str) else None
        week_days.append(
            {
                "weekday": weekday,
                "label": label,
                "date": week_start + timedelta(days=weekday),
                "available": weekday in availability_by_weekday,
                "session": session_candidate,
                "role_label": (
                    PLAN_ROLE_LABELS.get(session_candidate.role) if session_candidate else None
                ),
                "warning_labels": (
                    [WARNING_LABELS.get(w, w) for w in session_candidate.warnings]
                    if session_candidate
                    else []
                ),
                "skip_reason": SKIP_REASON_LABELS.get(skip_code, skip_code)
                if session_candidate is None
                else None,
            }
        )

    week_links = [
        {
            "label": label,
            "week": (week_start + timedelta(days=offset)).isoformat(),
            "current": offset == 0,
        }
        for offset, label in (
            (-7, "Vorherige Woche"),
            (0, "Ausgewählte Woche"),
            (7, "Nächste Woche"),
        )
    ]

    observed_frequency = baseline_context.get("observed_runs_per_week")
    return templates.TemplateResponse(
        request,
        "coach/planning_shadow.html",
        context(
            request,
            active_page="coach",
            week_start=week_start,
            week_end=week_start + timedelta(days=6),
            candidate=candidate,
            week_days=week_days,
            week_links=week_links,
            skip_labels=[
                SKIP_REASON_LABELS.get(
                    skip.reason_code if isinstance(skip.reason_code, str) else None,
                    skip.reason_code,
                )
                for skip in candidate.skipped_days
            ]
            if candidate
            else [],
            error=error,
            confidence_label=(
                CONFIDENCE_LABELS.get(str(baseline_context.get("confidence")), "–")
                if candidate
                else "–"
            ),
            effective_reentry=bool(profile_context.get("effective_reentry")),
            context_consistent_weeks=baseline_context.get("consistent_running_weeks", "–"),
            context_frequency=float(observed_frequency)
            if isinstance(observed_frequency, (int, float))
            else 0.0,
            context_intensity_mode=_intensity_mode_label(str(intensity_context.get("mode", "")))
            if candidate
            else "–",
            goals_label=", ".join(
                GOAL_TYPE_LABELS.get(str(goal.get("event_type")), str(goal.get("event_type")))
                for goal in goals_context
            )
            or "Keine aktiven Ziele erfasst",
            knowledge_version_short=(
                candidate.knowledge_base_version.split("+")[0] if candidate else "–"
            ),
        ),
    )


def _intensity_mode_label(mode: str) -> str:
    labels = {
        "pace_anchor": "Pace-Anker",
        "rpe_talk_test": "RPE/Sprechtest",
        "clarify": "Rückfrage erforderlich",
    }
    return labels.get(mode, mode)


@router.get("/{conversation_id}", response_class=HTMLResponse)
def coach_conversation(
    conversation_id: int,
    request: Request,
    session: SessionDep,
    configured: CoachProviderConfiguredDep,
    user: CurrentUser,
) -> HTMLResponse:
    return _render_coach(request, session, user, configured, conversation_id)


@router.post("/conversations")
def new_conversation(session: SessionDep, user: CurrentUser) -> RedirectResponse:
    conversation = create_conversation(session, user.id)
    session.commit()
    return RedirectResponse(f"/coach/{conversation.id}", status_code=303)


@router.get("/{conversation_id}/runs/{run_id}/proposal-card", response_class=HTMLResponse)
def proposal_card(
    conversation_id: int,
    run_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
) -> HTMLResponse:
    card = workout_artifact_presentation(session, user.id, conversation_id, run_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Vorschlag nicht gefunden")
    return templates.TemplateResponse(
        request,
        "workouts/_coach_proposal_card.html",
        context(request, card=card),
    )


@router.post("/workout-proposals/easy-run")
@router.post("/workout-proposals/running")
async def create_running_proposal(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    configured: CoachProviderConfiguredDep,
) -> Response:
    form = await request.form()
    proposal_date = str(form.get("suggested_for", ""))
    proposal_minutes = str(form.get("available_minutes", ""))
    proposal_template_id = str(form.get("template_id", "easy_run"))
    try:
        proposal = RunningProposalRequest.model_validate(
            {
                "template_id": proposal_template_id,
                "suggested_for": proposal_date,
                "available_minutes": proposal_minutes,
                "idempotency_key": str(form.get("idempotency_key", "")),
            }
        )
        workout = RunningProposalService(session, user, request_id=request.state.request_id).create(
            proposal
        )
    except ValidationError:
        error = "Bitte gib ein gültiges Datum und mindestens 20 verfügbare Minuten an."
    except WorkoutProposalError as exc:
        if exc.code == "proposal.feature_disabled":
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        error = str(exc)
    except (TemplateExpansionError, WorkoutTransitionError) as exc:
        error = str(exc)
    else:
        return RedirectResponse(
            f"/workouts/{workout.id}?notice=Workout-Vorschlag erstellt", status_code=303
        )
    return _render_coach(
        request,
        session,
        user,
        configured,
        None,
        proposal_error=error,
        proposal_date=proposal_date,
        proposal_minutes=proposal_minutes,
        proposal_template_id=proposal_template_id,
        status_code=422,
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


def _proposal_event_payload(runtime: CoachRuntimeContext) -> dict[str, object]:
    if runtime.conversation_id is None or runtime.assistant_run_id is None:
        raise RuntimeError("Proposal event has no local assistant run")
    with runtime.session_factory() as session:
        card = workout_artifact_presentation(
            session,
            runtime.user_id,
            runtime.conversation_id,
            runtime.assistant_run_id,
        )
    if card is None:
        raise RuntimeError("Proposal tool completed without a persisted artifact")
    return {
        "workout_id": card.workout_id,
        "run_id": card.assistant_run_id,
        "card_url": (
            f"/coach/{runtime.conversation_id}/runs/{card.assistant_run_id}/proposal-card"
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
                "run_id": runtime.assistant_run_id,
                "conversation_title": conversation_title,
                "user_html": _message_html(request, user_message, None),
                "assistant_html": _message_html(request, assistant_message, card),
                "failure_html": _message_html(
                    request, assistant_message, card, message_state="failed"
                ),
            }
        yield _event("run.started", started_payload)
        agent = agent_factory()
        async for event in agent.stream(history, runtime):
            if await request.is_disconnected():
                raise asyncio.CancelledError
            if event.type == "answer_text" and event.text:
                answer.append(event.text)
                yield _event("answer.delta", {"text": event.text})
            elif event.type == "artifact_available" and event.artifact_type == "workout":
                if not proposal_emitted:
                    yield _event("proposal.created", _proposal_event_payload(runtime))
                    proposal_emitted = True
            elif event.type == "completed":
                content = "".join(answer).strip()
                if not content:
                    raise CoachProviderError("Coach provider completed without answer text")
                with runtime.session_factory() as session:
                    complete_message(session, assistant_message_id, content)
                    session.commit()
                    assistant_message = session.get(CoachMessage, assistant_message_id)
                    if assistant_message is None:
                        raise RuntimeError("Completed Coach message is missing")
                    card = _proposal_card(
                        session, runtime.user_id, conversation_id, assistant_message
                    )
                    completed_html = _message_html(request, assistant_message, card)
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
                raise CoachProviderError("Coach provider execution failed")
        raise CoachProviderError("Coach provider stream ended without completion")
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
            assistant_message = session.get(CoachMessage, assistant_message_id)
            if assistant_message is None:
                raise RuntimeError("Failed Coach message is missing") from exc
            card = _proposal_card(session, runtime.user_id, conversation_id, assistant_message)
            failed_html = _message_html(request, assistant_message, card)
        logger.exception(
            "AI coach stream failed request_id=%s user_id=%s assistant_message_id=%s "
            "error_type=%s duration_ms=%s",
            runtime.request_id,
            runtime.user_id,
            assistant_message_id,
            type(exc).__name__,
            round((monotonic() - started_at) * 1000),
        )
        yield _event("error", {"message_id": assistant_message_id, "html": failed_html})


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
    if _has_active_response(session, existing_messages):
        raise HTTPException(status_code=409, detail="In diesem Chat läuft bereits eine Antwort.")

    execution = prepare_execution(
        session,
        conversation,
        existing_messages,
        user_id=user.id,
        question=message,
        model_id=get_settings().llm_model,
        request_id=request.state.request_id,
        as_of=date.today(),
    )
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
