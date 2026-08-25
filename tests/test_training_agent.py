import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from langchain.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.logging import FILE_HANDLER, configure_logging
from app.main import app
from app.models import (
    Activity,
    CoachAssistantRun,
    CoachConversation,
    CoachMessage,
    CoachToolCall,
    DailyHealth,
    User,
    Workout,
    WorkoutEvent,
)
from app.models.user import utcnow
from app.repositories.coach import create_assistant_run, create_message
from app.routes.coach import _stream_answer
from app.services.coach import agent as coach_agent_module
from app.services.coach.agent import (
    CoachEvent,
    CoachHistoryMessage,
    LangChainCoachAgent,
)
from app.services.coach.dependencies import get_coach_agent
from app.services.coach.tools import (
    CoachRuntimeContext,
    coach_tools,
    create_running_workout_proposal,
    get_current_recovery_state,
    get_health_day,
    get_subjective_context,
)


class FakeCoachAgent:
    def __init__(self) -> None:
        self.calls: list[list[CoachHistoryMessage]] = []

    async def stream(
        self,
        messages: Sequence[CoachHistoryMessage],
        runtime: CoachRuntimeContext,
    ) -> AsyncIterator[CoachEvent]:
        del runtime
        self.calls.append(list(messages))
        yield CoachEvent("status", text="Aktuelle Erholung wird geprüft")
        yield CoachEvent(
            "tool_started",
            tool_call_id="call-health",
            tool_name="get_current_recovery_state",
            label="Aktuelle Erholung prüfen",
        )
        yield CoachEvent(
            "tool_completed",
            tool_call_id="call-health",
            tool_name="get_current_recovery_state",
            label="Aktuelle Erholung prüfen",
        )
        yield CoachEvent("answer_delta", text="Du wirkst heute etwas weniger erholt. ")
        yield CoachEvent("answer_delta", text="Dein Ruhepuls liegt leicht über deinem Basiswert.")


def test_global_logging_writes_module_records_to_rotating_file(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "pacepilot.log"
    configure_logging(log_path)
    logging.getLogger("app.test").error("global file logging test")
    logging.getLogger("uvicorn.error").warning("uvicorn logging test")
    file_handler = next(
        handler for handler in logging.getLogger().handlers if handler.get_name() == FILE_HANDLER
    )
    file_handler.flush()

    content = log_path.read_text(encoding="utf-8")
    assert "ERROR app.test global file logging test" in content
    assert "WARNING uvicorn.error uvicorn logging test" in content


def test_openrouter_timeout_is_converted_to_sdk_milliseconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_create_agent(model: object, *_: Any, **__: Any) -> object:
        captured["model"] = model
        return object()

    monkeypatch.setattr(coach_agent_module, "create_agent", fake_create_agent)

    LangChainCoachAgent(api_key="test-key", model_id="test/model", timeout_seconds=60)

    model: Any = captured["model"]
    assert model.request_timeout == 60_000
    assert model.max_tokens == 2000


def _new_chat(client: TestClient) -> int:
    response = client.post("/coach/conversations", follow_redirects=False)
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[1])


def _running_history(session: Session, user_id: int) -> None:
    for index, age in enumerate((0, 7, 14, 21, 28, 35)):
        day = date.today() - timedelta(days=age)
        session.add(
            Activity(
                user_id=user_id,
                garmin_activity_id=f"coach-phase9-run-{index}",
                name=f"Lauf {index}",
                activity_type="running",
                started_at=datetime.combine(day, time(8)),
                duration_s=2400,
                distance_m=6000,
                average_hr=145,
                max_hr=170,
                synced_at=utcnow(),
            )
        )
    session.flush()


def test_coach_streams_and_persists_conversation(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", False)
    fake = FakeCoachAgent()
    app.dependency_overrides[get_coach_agent] = lambda: fake
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Wie erholt bin ich heute?"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: status" in response.text
    assert "event: tool.started" in response.text
    assert "event: tool.completed" in response.text
    assert "event: answer.delta" in response.text
    assert "Du wirkst heute etwas weniger erholt" in response.text
    assert fake.calls[0] == [CoachHistoryMessage("user", "Wie erholt bin ich heute?")]

    with session_factory() as session:
        conversation = session.get(CoachConversation, conversation_id)
        assert conversation is not None
        assert conversation.title == "Wie erholt bin ich heute?"
        messages = list(
            session.scalars(
                select(CoachMessage)
                .where(CoachMessage.conversation_id == conversation_id)
                .order_by(CoachMessage.id)
            )
        )
        assert [(message.role, message.status) for message in messages] == [
            ("user", "completed"),
            ("assistant", "completed"),
        ]
        assert messages[1].content.endswith("leicht über deinem Basiswert.")
        tool_call = session.scalar(
            select(CoachToolCall).where(CoachToolCall.message_id == messages[1].id)
        )
        assert tool_call is not None
        assert tool_call.tool_name == "get_current_recovery_state"
        assert tool_call.status == "completed"
        assert session.scalar(select(Workout)) is None

    page = client.get(f"/coach/{conversation_id}").text
    assert 'aria-label="Neuen Chat starten"' in page
    assert 'aria-label="Chat löschen"' in page
    assert "data-coach-message-list" in page
    assert "Nur lesend" in page
    assert "data-coach-activity" in page
    assert page.index("Aktuelle Erholung prüfen") < page.index(
        "Du wirkst heute etwas weniger erholt"
    )


def test_coach_tool_creates_one_durable_server_rendered_proposal(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", True)

    class ProposalAgent:
        runtime: CoachRuntimeContext | None = None

        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            self.runtime = runtime
            function: Any = cast(Any, create_running_workout_proposal).func
            rejected = json.loads(
                function(
                    runtime=SimpleNamespace(context=runtime),
                    suggested_for=runtime.as_of - timedelta(days=1),
                    available_minutes=45,
                )
            )
            assert rejected == {
                "status": "not_created",
                "error": {
                    "code": "proposal.date_in_past",
                    "message": "Das vorgeschlagene Datum darf nicht in der Vergangenheit liegen.",
                },
            }
            first = function(
                runtime=SimpleNamespace(context=runtime),
                suggested_for=date.today() + timedelta(days=1),
                available_minutes=45,
            )
            second = function(
                runtime=SimpleNamespace(context=runtime),
                suggested_for=date.today() + timedelta(days=1),
                available_minutes=45,
            )
            assert json.loads(first)["artifact"] == json.loads(second)["artifact"]
            yield CoachEvent(
                "tool_started",
                tool_call_id="provider-call-a",
                tool_name="create_running_workout_proposal",
                label="Easy-Run-Vorschlag erstellen",
            )
            yield CoachEvent(
                "tool_completed",
                tool_call_id="provider-call-a",
                tool_name="create_running_workout_proposal",
                label="Easy-Run-Vorschlag erstellen",
            )
            yield CoachEvent("proposal_created")
            yield CoachEvent("proposal_created")
            yield CoachEvent("answer_delta", text="Ich habe einen Vorschlag vorbereitet.")

    fake = ProposalAgent()
    app.dependency_overrides[get_coach_agent] = lambda: fake
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        _running_history(session, user.id)
        session.commit()
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Erstelle mir morgen einen lockeren Lauf für 45 Minuten."},
    )

    assert response.status_code == 200
    assert response.text.count("event: proposal.created") == 1
    assert '"card_url":"/coach/' in response.text
    assert fake.runtime is not None
    assert f'"run_id":{fake.runtime.assistant_run_id}' in response.text

    function = cast(Any, create_running_workout_proposal).func
    with session_factory() as session:
        run = session.get(CoachAssistantRun, fake.runtime.assistant_run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.workout_id is not None
        workout = session.get(Workout, run.workout_id)
        assert workout is not None
        assert workout.originating_conversation_id == conversation_id
        assert workout.originating_user_message_id == run.user_message_id
        assert workout.originating_assistant_message_id == run.assistant_message_id
        assert workout.approval_status == "proposed"
        assert workout.scheduled_for is None
        assert workout.accepted_revision_id is None
        assert (
            session.scalar(
                select(WorkoutEvent).where(
                    WorkoutEvent.workout_id == workout.id,
                    WorkoutEvent.action == "propose",
                )
            )
            is not None
        )
        assert len(list(session.scalars(select(Workout)))) == 1
        workout_id = workout.id

    page = client.get(f"/coach/{conversation_id}")
    assert page.status_code == 200
    assert "Trainingsvorschlag" in page.text
    assert "Unbestätigt" in page.text
    assert f'href="/workouts/{workout_id}"' in page.text
    card = client.get(
        f"/coach/{conversation_id}/runs/{fake.runtime.assistant_run_id}/proposal-card"
    )
    assert card.status_code == 200
    assert f'data-workout-id="{workout_id}"' in card.text
    assert (
        client.get(f"/coach/999/runs/{fake.runtime.assistant_run_id}/proposal-card").status_code
        == 404
    )

    with session_factory() as session:
        workout = session.get(Workout, workout_id)
        assert workout is not None and workout.current_revision_id is not None
        workout.accepted_revision_id = workout.current_revision_id
        workout.approval_status = "accepted"
        workout.local_schedule_status = "scheduled"
        workout.scheduled_for = date.today() + timedelta(days=1)
        session.commit()
    updated_card = client.get(
        f"/coach/{conversation_id}/runs/{fake.runtime.assistant_run_id}/proposal-card"
    )
    assert "Eingeplant" in updated_card.text
    assert "im lokalen Kalender eingeplant" in updated_card.text
    assert "weder angenommen noch eingeplant" not in updated_card.text

    conflict = json.loads(
        function(
            runtime=SimpleNamespace(context=fake.runtime),
            suggested_for=date.today() + timedelta(days=2),
            available_minutes=45,
        )
    )
    assert conflict["status"] == "not_created"
    assert conflict["error"]["code"] == "proposal.idempotency_conflict"
    with session_factory() as session:
        assert len(list(session.scalars(select(Workout)))) == 1

    deleted = client.post(f"/coach/{conversation_id}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    with session_factory() as session:
        assert session.get(CoachAssistantRun, fake.runtime.assistant_run_id) is None
        workout = session.get(Workout, workout_id)
        assert workout is not None
        assert workout.originating_conversation_id is None
        assert workout.originating_user_message_id is None
        assert workout.originating_assistant_message_id is None


def test_invalid_proposal_date_returns_completed_stream_without_artifact(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", True)

    class InvalidDateAgent:
        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            function: Any = cast(Any, create_running_workout_proposal).func
            result = json.loads(
                function(
                    runtime=SimpleNamespace(context=runtime),
                    suggested_for=runtime.as_of - timedelta(days=1),
                    available_minutes=45,
                )
            )
            assert result["status"] == "not_created"
            assert result["error"]["code"] == "proposal.date_in_past"
            yield CoachEvent(
                "tool_started",
                tool_call_id="invalid-date-call",
                tool_name="create_running_workout_proposal",
                label="Easy-Run-Vorschlag erstellen",
            )
            yield CoachEvent(
                "tool_completed",
                tool_call_id="invalid-date-call",
                tool_name="create_running_workout_proposal",
                label="Easy-Run-Vorschlag erstellen",
            )
            yield CoachEvent(
                "answer_delta",
                text="Das Datum liegt in der Vergangenheit. Welches zukünftige Datum meinst du?",
            )

    app.dependency_overrides[get_coach_agent] = lambda: InvalidDateAgent()
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Erstelle mir gestern einen lockeren Lauf für 45 Minuten."},
    )

    assert response.status_code == 200
    assert "event: answer.completed" in response.text
    assert "event: proposal.created" not in response.text
    assert "Welches zukünftige Datum meinst du?" in response.text
    with session_factory() as session:
        assert session.scalar(select(Workout)) is None
        run = session.scalar(
            select(CoachAssistantRun).where(CoachAssistantRun.conversation_id == conversation_id)
        )
        assert run is not None
        assert run.status == "completed"
        assert run.workout_id is None


def test_proposal_tool_schema_exposes_no_runtime_or_workout_definition() -> None:
    schema_model: Any = create_running_workout_proposal.tool_call_schema
    schema = schema_model.model_json_schema()
    assert set(schema["properties"]) == {"suggested_for", "available_minutes"}
    serialized = json.dumps(schema)
    assert "user_id" not in serialized
    assert "assistant_run_id" not in serialized
    assert "idempotency" not in serialized
    assert "WorkoutDefinition" not in serialized


def test_agent_registers_exactly_one_bounded_mutation_tool() -> None:
    read_only = {tool.name for tool in coach_tools(workout_proposals_enabled=False)}
    enabled = {tool.name for tool in coach_tools(workout_proposals_enabled=True)}
    assert enabled - read_only == {"create_running_workout_proposal"}
    assert (
        not {
            "accept_workout",
            "schedule_workout",
            "publish_workout",
            "push_workout",
        }
        & enabled
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_mode", "expected_status"),
    (("provider", "failed"), ("disconnect", "interrupted")),
)
async def test_proposal_survives_stream_failure_after_commit(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
    expected_status: str,
) -> None:
    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", True)

    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return failure_mode == "disconnect"

    class FailingAfterProposalAgent:
        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            function: Any = cast(Any, create_running_workout_proposal).func
            function(
                runtime=SimpleNamespace(context=runtime),
                suggested_for=date.today() + timedelta(days=1),
                available_minutes=35,
            )
            yield CoachEvent("proposal_created")
            if failure_mode == "provider":
                raise RuntimeError("provider failed after proposal")

    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        _running_history(session, user.id)
        conversation = CoachConversation(user_id=user.id, title="Fehler nach Vorschlag")
        session.add(conversation)
        session.flush()
        user_message = create_message(
            session, conversation, role="user", content="Plane einen Lauf."
        )
        assistant_message = create_message(
            session, conversation, role="assistant", status="streaming"
        )
        run = create_assistant_run(
            session,
            conversation,
            user_message,
            assistant_message,
            model_id="test/model",
            request_id="phase9-provider-failure",
        )
        session.commit()
        user_id = user.id
        conversation_id = conversation.id
        user_message_id = user_message.id
        assistant_message_id = assistant_message.id
        run_id = run.id

    runtime = CoachRuntimeContext(
        user_id,
        date.today(),
        session_factory,
        request_id="phase9-provider-failure",
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        assistant_run_id=run_id,
    )
    stream = _stream_answer(
        request=cast(Any, ConnectedRequest()),
        agent=FailingAfterProposalAgent(),
        history=[CoachHistoryMessage("user", "Plane einen Lauf.")],
        runtime=runtime,
        assistant_message_id=assistant_message_id,
    )
    events: list[str] = []
    expected_error = RuntimeError if failure_mode == "provider" else asyncio.CancelledError
    with pytest.raises(expected_error):
        async for event in stream:
            events.append(event)

    assert any("event: proposal.created" in event for event in events) == (
        failure_mode == "provider"
    )
    with session_factory() as session:
        failed_run = session.get(CoachAssistantRun, run_id)
        assert failed_run is not None
        assert failed_run.status == expected_status
        assert failed_run.workout_id is not None
        workout_id = failed_run.workout_id
        assert len(list(session.scalars(select(Workout)))) == 1

    reloaded = client.get(f"/coach/{conversation_id}")
    assert reloaded.status_code == 200
    assert "Diese Antwort wurde nicht abgeschlossen" in reloaded.text
    assert f'href="/workouts/{workout_id}"' in reloaded.text


def test_follow_up_includes_bounded_conversation_history(client: TestClient) -> None:
    fake = FakeCoachAgent()
    app.dependency_overrides[get_coach_agent] = lambda: fake
    conversation_id = _new_chat(client)

    first = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Wie erholt bin ich heute?"},
    )
    second = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Und was bedeutet das für mein Training?"},
    )

    assert first.status_code == second.status_code == 200
    assert [(item.role, item.content) for item in fake.calls[1]] == [
        ("user", "Wie erholt bin ich heute?"),
        (
            "assistant",
            "Du wirkst heute etwas weniger erholt. "
            "Dein Ruhepuls liegt leicht über deinem Basiswert.",
        ),
        ("user", "Und was bedeutet das für mein Training?"),
    ]


def test_coach_requires_configured_backend(client: TestClient) -> None:
    app.dependency_overrides[get_coach_agent] = lambda: None
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Wie erholt bin ich?"},
    )

    assert response.status_code == 503
    assert response.json()["detail"].startswith("Konfiguriere zuerst OpenRouter")


def test_conversations_are_user_scoped(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as session:
        other_user = User(
            display_name="Andere Person",
            onboarding_notice_acknowledged_at=utcnow(),
            onboarding_completed_at=utcnow(),
            onboarding_completed_version=1,
        )
        session.add(other_user)
        session.flush()
        conversation = CoachConversation(user_id=other_user.id, title="Privater Chat")
        session.add(conversation)
        session.commit()
        conversation_id = conversation.id

    assert client.get(f"/coach/{conversation_id}").status_code == 404
    assert (
        client.post(
            f"/coach/{conversation_id}/messages", data={"message": "Zeige den Chat."}
        ).status_code
        == 404
    )
    assert client.post(f"/coach/{conversation_id}/delete").status_code == 404
    assert "Privater Chat" not in client.get("/coach").text


def test_delete_conversation_cascades_and_preserves_selection(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    selected_id = _new_chat(client)
    deleted_id = _new_chat(client)
    with session_factory() as session:
        conversation = session.get(CoachConversation, deleted_id)
        assert conversation is not None
        assistant = CoachMessage(
            conversation=conversation,
            role="assistant",
            content="Gespeicherte Antwort",
            status="completed",
            completed_at=utcnow(),
        )
        session.add(assistant)
        session.flush()
        tool_call = CoachToolCall(
            message=assistant,
            call_id="delete-test",
            tool_name="get_health_day",
            label="Gesundheitstag geprüft",
            status="completed",
            completed_at=utcnow(),
        )
        session.add(tool_call)
        session.commit()
        message_id = assistant.id
        tool_call_id = tool_call.id

    response = client.post(
        f"/coach/{deleted_id}/delete",
        data={"selected_conversation_id": str(selected_id)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/coach/{selected_id}"
    with session_factory() as session:
        assert session.get(CoachConversation, deleted_id) is None
        assert session.get(CoachMessage, message_id) is None
        assert session.get(CoachToolCall, tool_call_id) is None

    response = client.post(
        f"/coach/{selected_id}/delete",
        data={"selected_conversation_id": str(selected_id)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/coach"
    assert "Dein persönlicher Gesundheitscoach" in client.get("/coach").text


def test_active_conversation_cannot_be_deleted(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    conversation_id = _new_chat(client)
    with session_factory() as session:
        conversation = session.get(CoachConversation, conversation_id)
        assert conversation is not None
        session.add(
            CoachMessage(
                conversation=conversation,
                role="assistant",
                status="streaming",
            )
        )
        session.commit()

    response = client.post(f"/coach/{conversation_id}/delete")

    assert response.status_code == 409
    assert "laufenden Antwort" in response.json()["detail"]
    with session_factory() as session:
        assert session.get(CoachConversation, conversation_id) is not None


def test_health_day_tool_uses_runtime_user_scope(
    session_factory: sessionmaker[Session],
) -> None:
    day = date(2026, 8, 11)
    with session_factory() as session:
        first = User(display_name="Erste Person")
        second = User(display_name="Zweite Person")
        session.add_all((first, second))
        session.flush()
        session.add_all(
            (
                DailyHealth(user_id=first.id, day=day, resting_hr=48, hrv_average=55),
                DailyHealth(user_id=second.id, day=day, resting_hr=72, hrv_average=20),
            )
        )
        session.commit()
        first_id = first.id

    context = CoachRuntimeContext(first_id, day, session_factory)
    runtime: Any = SimpleNamespace(context=context)
    tool_function: Any = cast(Any, get_health_day).func

    payload = json.loads(tool_function(day=day, runtime=runtime))

    assert payload["resting_hr"] == 48
    assert payload["hrv_average"] == 55
    assert payload["day"] == "2026-08-11"


def test_subjective_context_tool_uses_runtime_user_scope(
    session_factory: sessionmaker[Session],
) -> None:
    day = date(2026, 8, 11)
    with session_factory() as session:
        first = User(display_name="Erste Person")
        second = User(display_name="Zweite Person")
        session.add_all((first, second))
        session.flush()
        session.add_all(
            [
                Activity(
                    user_id=first.id,
                    garmin_activity_id="run-1",
                    name="Lauf",
                    activity_type="running",
                    started_at=utcnow().replace(year=2026, month=8, day=11),
                    workout_rpe=7,
                    workout_feel=4,
                ),
                Activity(
                    user_id=second.id,
                    garmin_activity_id="private-run",
                    name="Privater Lauf",
                    activity_type="running",
                    started_at=utcnow().replace(year=2026, month=8, day=11),
                    workout_rpe=10,
                    workout_feel=1,
                ),
            ]
        )
        session.commit()
        first_id = first.id

    runtime: Any = SimpleNamespace(context=CoachRuntimeContext(first_id, day, session_factory))
    tool_function: Any = cast(Any, get_subjective_context).func

    payload = json.loads(tool_function(runtime=runtime))

    assert "daily_checkins" not in payload
    assert [item["name"] for item in payload["recent_activity_feedback"]] == ["Lauf"]
    assert payload["recent_activity_feedback"][0]["effort"] == 7
    assert payload["recent_activity_feedback"][0]["effort_source"] == "garmin"


def test_langgraph_injects_runtime_context_into_coach_tools(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Runtime Test")
        session.add(user)
        session.commit()
        user_id = user.id

    builder: Any = StateGraph(cast(Any, MessagesState), context_schema=CoachRuntimeContext)
    builder.add_node("tools", ToolNode([get_current_recovery_state]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()
    result = graph.invoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "runtime-test",
                            "name": "get_current_recovery_state",
                            "args": {},
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        },
        context=CoachRuntimeContext(user_id, date(2026, 8, 11), session_factory),
    )

    tool_message = result["messages"][-1]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.status == "success"
    assert '"as_of":"2026-08-11"' in str(tool_message.content)


@pytest.mark.asyncio
async def test_langchain_backend_maps_tokens_and_tool_lifecycle(
    session_factory: sessionmaker[Session],
) -> None:
    class FakeGraph:
        async def astream(
            self, inputs: dict[str, Any], **kwargs: Any
        ) -> AsyncIterator[dict[str, Any]]:
            assert kwargs["stream_mode"] == ["messages", "updates"]
            assert kwargs["version"] == "v2"
            assert inputs["messages"][0] == {
                "role": "system",
                "content": (
                    "Vertrauenswürdiger PacePilot-Serverkontext: Heute ist 2026-08-11. "
                    "'Morgen' ist 2026-08-12. 'Übermorgen' ist 2026-08-13. Dieser Kontext "
                    "hat Vorrang vor Annahmen über das aktuelle Datum."
                ),
            }
            assert inputs["messages"][1] == {
                "role": "user",
                "content": "Wie ist meine HRV?",
            }
            yield {
                "type": "updates",
                "ns": (),
                "data": {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "id": "tool-1",
                                        "name": "get_health_trends",
                                        "args": {"days": 28, "metrics": ["hrv"]},
                                        "type": "tool_call",
                                    }
                                ],
                            )
                        ]
                    }
                },
            }
            yield {
                "type": "updates",
                "ns": (),
                "data": {
                    "tools": {
                        "messages": [
                            ToolMessage(
                                content='{"hrv":55}',
                                tool_call_id="tool-1",
                                name="get_health_trends",
                            )
                        ]
                    }
                },
            }
            yield {
                "type": "messages",
                "ns": (),
                "data": (
                    AIMessageChunk(content="Deine HRV ist stabil."),
                    {"langgraph_node": "model"},
                ),
            }

    backend = LangChainCoachAgent.__new__(LangChainCoachAgent)
    backend._model_id = "test/model"
    backend._agent = FakeGraph()
    runtime = CoachRuntimeContext(1, date(2026, 8, 11), session_factory)

    events = [
        event
        async for event in backend.stream(
            [CoachHistoryMessage("user", "Wie ist meine HRV?")], runtime
        )
    ]

    assert [event.type for event in events] == [
        "status",
        "tool_started",
        "tool_completed",
        "status",
        "answer_delta",
    ]
    assert events[1].summary == "Zeitraum: 28 Tage"
    assert events[-1].text == "Deine HRV ist stabil."


@pytest.mark.asyncio
async def test_langchain_backend_maps_only_valid_proposal_artifact(
    session_factory: sessionmaker[Session],
) -> None:
    class FakeGraph:
        async def astream(self, *_: Any, **__: Any) -> AsyncIterator[dict[str, Any]]:
            yield {
                "type": "updates",
                "ns": (),
                "data": {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "id": "proposal-call",
                                        "name": "create_running_workout_proposal",
                                        "args": {
                                            "suggested_for": "2026-08-25",
                                            "available_minutes": 45,
                                        },
                                        "type": "tool_call",
                                    }
                                ],
                            )
                        ]
                    }
                },
            }
            yield {
                "type": "updates",
                "ns": (),
                "data": {
                    "tools": {
                        "messages": [
                            ToolMessage(
                                content=(
                                    '{"status":"created","artifact":{"type":"workout_proposal"}}'
                                ),
                                tool_call_id="proposal-call",
                                name="create_running_workout_proposal",
                            )
                        ]
                    }
                },
            }
            yield {
                "type": "messages",
                "ns": (),
                "data": (
                    AIMessageChunk(content="Der Vorschlag ist bereit."),
                    {"langgraph_node": "model"},
                ),
            }

    backend = LangChainCoachAgent.__new__(LangChainCoachAgent)
    backend._model_id = "test/model"
    backend._agent = FakeGraph()
    events = [
        event
        async for event in backend.stream(
            [CoachHistoryMessage("user", "Plane einen Easy Run.")],
            CoachRuntimeContext(1, date(2026, 8, 24), session_factory),
        )
    ]

    assert [event.type for event in events].count("proposal_created") == 1
    assert events[-1] == CoachEvent("answer_delta", text="Der Vorschlag ist bereit.")


@pytest.mark.asyncio
async def test_langchain_backend_logs_and_propagates_provider_errors(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingGraph:
        async def astream(self, *_: Any, **__: Any) -> AsyncIterator[dict[str, Any]]:
            raise RuntimeError("secret provider detail")
            yield {"type": "updates", "ns": (), "data": {}}

    backend = LangChainCoachAgent.__new__(LangChainCoachAgent)
    backend._model_id = "test/model"
    backend._agent = FailingGraph()
    runtime = CoachRuntimeContext(1, date(2026, 8, 11), session_factory)
    logger = Mock()
    monkeypatch.setattr(coach_agent_module, "logger", logger)

    private_question = "Mein privater Gesundheitswert ist 123."
    with pytest.raises(RuntimeError, match="secret provider detail"):
        async for _ in backend.stream([CoachHistoryMessage("user", private_question)], runtime):
            pass

    logger.exception.assert_called_once()
    log_call = logger.exception.call_args
    assert "AI coach agent failed" in log_call.args[0]
    assert "RuntimeError" in log_call.args
    assert private_question not in repr(log_call)


@pytest.mark.asyncio
async def test_streaming_route_marks_failure_and_propagates_original_exception(
    session_factory: sessionmaker[Session],
) -> None:
    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    class FailingAgent:
        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages, runtime
            yield CoachEvent("status", text="Start")
            raise ValueError("original stream failure")

    with session_factory() as session:
        user = User(display_name="Stream Test")
        session.add(user)
        session.flush()
        conversation = CoachConversation(user_id=user.id, title="Test")
        session.add(conversation)
        session.flush()
        assistant = CoachMessage(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            status="streaming",
        )
        session.add(assistant)
        session.commit()
        user_id = user.id
        assistant_id = assistant.id

    stream = _stream_answer(
        request=cast(Any, ConnectedRequest()),
        agent=FailingAgent(),
        history=[CoachHistoryMessage("user", "Test")],
        runtime=CoachRuntimeContext(user_id, date(2026, 8, 11), session_factory),
        assistant_message_id=assistant_id,
    )
    with pytest.raises(ValueError, match="original stream failure"):
        async for _ in stream:
            pass

    with session_factory() as session:
        failed_message = session.get(CoachMessage, assistant_id)
        assert failed_message is not None
        assert failed_message.status == "failed"
