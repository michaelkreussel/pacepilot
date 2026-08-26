import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from time import monotonic
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.auth import CurrentUser
from app.config import get_settings
from app.database import SessionDep
from app.models import CoachMessage, Workout, WorkoutRevision
from app.models.user import utcnow
from app.onboarding import require_data_access
from app.repositories.coach import (
    complete_message,
    conversation_messages,
    create_assistant_run,
    create_conversation,
    create_message,
    fail_message,
    find_assistant_run,
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
from app.services.planning.weekly_planner import (
    WeeklyPlanCandidate,
    WeeklyPlannerError,
    plan_shadow_week,
)
from app.services.planning.workout_definition import (
    HeartRateRangeTarget,
    RpeRangeTarget,
    StepBlockV2,
    workout_metrics,
)
from app.services.planning.workout_proposals import (
    EasyRunProposalRequest,
    RunningProposalService,
    WorkoutProposalError,
)
from app.services.planning.workout_service import WorkoutTransitionError
from app.services.planning.workout_templates import TemplateExpansionError
from app.web import context, templates

router = APIRouter(prefix="/coach", dependencies=[Depends(require_data_access)])
logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_CHARACTERS = 12_000


@dataclass(frozen=True)
class CoachProposalCard:
    workout_id: int
    assistant_run_id: int
    name: str
    suggested_for: date | None
    duration_minutes: int
    target_label: str
    status_label: str
    status_description: str


def _proposal_card(
    session: Session, user_id: int, conversation_id: int, run_id: int
) -> CoachProposalCard | None:
    run = find_assistant_run(session, user_id, conversation_id, run_id)
    if run is None or run.workout_id is None:
        return None
    workout = session.get(Workout, run.workout_id)
    if (
        workout is None
        or workout.user_id != user_id
        or workout.deleted_at is not None
        or workout.source_type != "coach_single"
        or workout.originating_conversation_id != conversation_id
        or workout.originating_user_message_id != run.user_message_id
        or workout.originating_assistant_message_id != run.assistant_message_id
        or workout.current_revision_id is None
    ):
        return None
    revision = session.get(WorkoutRevision, workout.current_revision_id)
    if revision is None or revision.workout_id != workout.id:
        return None
    definition = revision.definition_model
    target_label = "Lokale Intensitätsleitplanken"
    if len(definition.blocks) == 1 and isinstance(definition.blocks[0], StepBlockV2):
        target = definition.blocks[0].target
        if isinstance(target, HeartRateRangeTarget):
            target_label = f"HF {target.lower_bpm}–{target.upper_bpm} bpm"
        elif isinstance(target, RpeRangeTarget):
            target_label = f"RPE {target.lower_rpe}–{target.upper_rpe}"
    if workout.approval_status == "rejected":
        status_label = "Abgelehnt"
        status_description = "Dieser Vorschlag wurde abgelehnt und wird nicht ausgeführt."
    elif workout.status == "pushed":
        status_label = "An Uhr gesendet"
        status_description = "Die angenommene Revision wurde an das Garmin-Gerät gesendet."
    elif workout.status == "published":
        status_label = "Bei Garmin"
        status_description = "Die angenommene Revision wurde zu Garmin übertragen."
    elif workout.local_schedule_status == "scheduled":
        status_label = "Eingeplant"
        status_description = "Die angenommene Revision ist im lokalen Kalender eingeplant."
    elif workout.accepted_revision_id is not None:
        status_label = "Angenommen"
        status_description = "Die geprüfte Revision wurde angenommen, aber nicht eingeplant."
    else:
        status_label = "Unbestätigt"
        status_description = (
            "Der deterministische Vorschlag ist weder angenommen noch eingeplant. "
            "Prüfe ihn vor jeder weiteren Aktion."
        )
    return CoachProposalCard(
        workout_id=workout.id,
        assistant_run_id=run.id,
        name=revision.name,
        suggested_for=revision.suggested_for,
        duration_minutes=round(workout_metrics(definition).duration_seconds / 60),
        target_label=target_label,
        status_label=status_label,
        status_description=status_description,
    )


def _proposal_cards(
    session: Session, user_id: int, conversation_id: int, messages: Sequence[CoachMessage]
) -> dict[int, CoachProposalCard]:
    cards: dict[int, CoachProposalCard] = {}
    for message in messages:
        run = message.generated_run
        if run is None:
            continue
        card = _proposal_card(session, user_id, conversation_id, run.id)
        if card is not None:
            cards[message.id] = card
    return cards


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
    *,
    proposal_error: str | None = None,
    proposal_date: str | None = None,
    proposal_minutes: str = "45",
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
            configured=agent is not None,
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
            proposal_idempotency_key=str(uuid4()),
        ),
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def coach(
    request: Request, session: SessionDep, agent: CoachAgentDep, user: CurrentUser
) -> HTMLResponse:
    return _render_coach(request, session, user, agent, None)


WEEKDAY_LABELS = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)
ROLE_LABELS = {
    "long_run": "Langer Lauf",
    "strides": "Steigerungen",
    "easy_run": "Lockerer Lauf",
}
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
}

GOAL_TYPE_LABELS = {
    "general_fitness": "Allgemeine Fitness",
    "5k": "5 km",
    "10k": "10 km",
    "half_marathon": "Halbmarathon",
    "marathon": "Marathon",
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
    if not get_settings().coach_plan_generation_enabled:
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
                    ROLE_LABELS.get(session_candidate.role) if session_candidate else None
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
    agent: CoachAgentDep,
    user: CurrentUser,
) -> HTMLResponse:
    return _render_coach(request, session, user, agent, conversation_id)


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
    card = _proposal_card(session, user.id, conversation_id, run_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Vorschlag nicht gefunden")
    return templates.TemplateResponse(
        request,
        "workouts/_coach_proposal_card.html",
        context(request, card=card),
    )


@router.post("/workout-proposals/easy-run")
async def create_easy_run_proposal(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    agent: CoachAgentDep,
) -> Response:
    form = await request.form()
    proposal_date = str(form.get("suggested_for", ""))
    proposal_minutes = str(form.get("available_minutes", ""))
    try:
        proposal = EasyRunProposalRequest.model_validate(
            {
                "suggested_for": proposal_date,
                "available_minutes": proposal_minutes,
                "idempotency_key": str(form.get("idempotency_key", "")),
            }
        )
        workout = RunningProposalService(
            session, user, request_id=request.state.request_id
        ).create_easy_run(proposal)
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
            f"/workouts/{workout.id}?notice=Easy-Run-Vorschlag erstellt", status_code=303
        )
    return _render_coach(
        request,
        session,
        user,
        agent,
        None,
        proposal_error=error,
        proposal_date=proposal_date,
        proposal_minutes=proposal_minutes,
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


def _proposal_event_payload(runtime: CoachRuntimeContext) -> dict[str, object]:
    if runtime.conversation_id is None or runtime.assistant_run_id is None:
        raise RuntimeError("Proposal event has no local assistant run")
    with runtime.session_factory() as session:
        card = _proposal_card(
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
    agent: CoachAgent,
    history: Sequence[CoachHistoryMessage],
    runtime: CoachRuntimeContext,
    assistant_message_id: int,
) -> AsyncIterator[str]:
    started_at = monotonic()
    answer: list[str] = []
    proposal_emitted = False
    logger.info(
        "AI coach stream started request_id=%s user_id=%s assistant_message_id=%s "
        "history_messages=%s",
        runtime.request_id,
        runtime.user_id,
        assistant_message_id,
        len(history),
    )
    yield _event(
        "run.started",
        {"message_id": assistant_message_id, "run_id": runtime.assistant_run_id},
    )
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
            elif event.type == "proposal_created":
                if not proposal_emitted:
                    yield _event("proposal.created", _proposal_event_payload(runtime))
                    proposal_emitted = True

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
    user_message = create_message(session, conversation, role="user", content=message)
    assistant = create_message(
        session,
        conversation,
        role="assistant",
        status="streaming",
        model_id=get_settings().llm_model,
    )
    assistant_run = create_assistant_run(
        session,
        conversation,
        user_message,
        assistant,
        model_id=get_settings().llm_model,
        request_id=request.state.request_id,
    )
    session.commit()

    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)
    runtime = CoachRuntimeContext(
        user_id=user.id,
        as_of=date.today(),
        session_factory=factory,
        request_id=request.state.request_id,
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        assistant_message_id=assistant.id,
        assistant_run_id=assistant_run.id,
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
