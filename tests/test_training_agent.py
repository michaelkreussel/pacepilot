import json
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime
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

from app.logging import FILE_HANDLER, configure_logging
from app.main import app
from app.models import (
    Activity,
    AthleteGoal,
    AthleteManualAnchor,
    AthleteProfile,
    CoachConversation,
    CoachMessage,
    CoachToolCall,
    DailyHealth,
    User,
    Workout,
)
from app.models.user import utcnow
from app.routes.coach import _stream_answer
from app.services.coach import agent as coach_agent_module
from app.services.coach.agent import (
    CoachEvent,
    CoachHistoryMessage,
    LangChainCoachAgent,
)
from app.services.coach.dependencies import get_coach_agent
from app.services.coach.tools import (
    COACH_TOOLS,
    CoachRuntimeContext,
    get_athlete_planning_context,
    get_current_recovery_state,
    get_health_day,
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


def test_coach_streams_and_persists_conversation(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
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
    assert "data-coach-activity" in page
    assert page.index("Aktuelle Erholung prüfen") < page.index(
        "Du wirkst heute etwas weniger erholt"
    )


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


def test_planning_context_tool_is_registered_without_model_arguments() -> None:
    assert get_athlete_planning_context in COACH_TOOLS
    tool_schema: Any = get_athlete_planning_context.tool_call_schema
    schema = tool_schema.model_json_schema()
    assert schema["properties"] == {}
    assert "runtime" not in schema
    assert "user_id" not in schema
    assert "as_of" not in schema


def test_planning_context_tool_is_user_scoped_as_of_bound_and_read_only(
    session_factory: sessionmaker[Session],
) -> None:
    as_of = date(2026, 8, 11)
    with session_factory() as session:
        first = User(display_name="Planung Eins")
        second = User(display_name="Planung Zwei")
        session.add_all((first, second))
        session.flush()
        session.add_all(
            (
                AthleteProfile(user_id=first.id, primary_sport="running"),
                AthleteGoal(
                    user_id=first.id,
                    sport="running",
                    event_name="Erstes Ziel",
                    target_date=date(2026, 10, 4),
                    distance_m=10_000,
                    target_duration_s=2_400,
                ),
                AthleteManualAnchor(
                    user_id=first.id,
                    sport="running",
                    metric="threshold_hr",
                    value=171,
                    observed_on=as_of,
                ),
                AthleteGoal(
                    user_id=second.id,
                    sport="running",
                    event_name="Privates zweites Ziel",
                    target_date=date(2026, 11, 1),
                    distance_m=21_097.5,
                ),
                AthleteManualAnchor(
                    user_id=second.id,
                    sport="running",
                    metric="max_hr",
                    value=233,
                    observed_on=as_of,
                ),
                Activity(
                    user_id=first.id,
                    garmin_activity_id="planning-past",
                    name="Vergangener Lauf",
                    activity_type="running",
                    started_at=datetime(2026, 8, 10, 9),
                    distance_m=8_000,
                    duration_s=2_800,
                ),
                Activity(
                    user_id=first.id,
                    garmin_activity_id="planning-future",
                    name="Zukünftiger Lauf",
                    activity_type="running",
                    started_at=datetime(2026, 8, 12, 9),
                    distance_m=30_000,
                    duration_s=10_000,
                ),
                Workout(
                    user_id=first.id,
                    name="Unveränderter Entwurf",
                    sport="running",
                    scheduled_for=date(2026, 8, 12),
                ),
            )
        )
        session.commit()
        first_id = first.id

    runtime: Any = SimpleNamespace(context=CoachRuntimeContext(first_id, as_of, session_factory))
    tool_function: Any = cast(Any, get_athlete_planning_context).func

    payload = json.loads(tool_function(runtime=runtime))

    assert payload["schema_version"] == "athlete-planning-context.v1"
    assert payload["as_of"] == "2026-08-11"
    planning = payload["planning_context"]
    assert planning["schema_version"] == "planning-context.v2"
    assert planning["as_of"] == "2026-08-11"
    assert planning["goal"]["event_name"] == "Erstes Ziel"
    assert planning["training_capacity"]["running_distance_28d_m"] == 8_000
    assert planning["planning_limits"]["schema_version"] == "running-planning-limits.v1"
    threshold = next(item for item in planning["performance"] if item["key"] == "threshold_hr")
    assert threshold["value"] == 171
    assert payload["recovery_state"]["as_of"] == "2026-08-11"
    serialized = json.dumps(payload)
    assert "Privates zweites Ziel" not in serialized
    assert "233" not in serialized

    with session_factory() as session:
        workouts = list(session.scalars(select(Workout)))
        assert len(workouts) == 1
        assert workouts[0].name == "Unveränderter Entwurf"
        assert workouts[0].status == "draft"


def test_planning_context_tool_does_not_read_or_expose_activity_source_data(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    as_of = date(2026, 8, 11)
    sentinels = (
        "raw-private-sentinel.json.gz",
        "details-private-sentinel.json.gz",
        "fit-private-sentinel.fit",
    )
    with session_factory() as session:
        user = User(display_name="Datenschutz")
        session.add(user)
        session.flush()
        session.add(
            Activity(
                user_id=user.id,
                garmin_activity_id="private-source-files",
                name="Lauf",
                activity_type="running",
                started_at=datetime(2026, 8, 10, 9),
                distance_m=10_000,
                duration_s=3_600,
                raw_file=sentinels[0],
                details_file=sentinels[1],
                fit_file=sentinels[2],
            )
        )
        session.commit()
        user_id = user.id

    def fail_if_detail_data_is_read(*_: object, **__: object) -> object:
        raise AssertionError("planning context tool must not read detail evidence")

    monkeypatch.setattr(
        "app.services.analytics.detail_evidence._fit_samples", fail_if_detail_data_is_read
    )
    monkeypatch.setattr(
        "app.services.analytics.detail_evidence._sampled_detail_samples",
        fail_if_detail_data_is_read,
    )
    runtime: Any = SimpleNamespace(context=CoachRuntimeContext(user_id, as_of, session_factory))
    tool_function: Any = cast(Any, get_athlete_planning_context).func

    serialized = tool_function(runtime=runtime)

    assert all(sentinel not in serialized for sentinel in sentinels)
    for private_field in (
        '"raw_file"',
        '"details_file"',
        '"fit_file"',
        '"latitude"',
        '"longitude"',
        '"polyline"',
    ):
        assert private_field not in serialized


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
        async def astream(self, *_: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
            assert kwargs["stream_mode"] == ["messages", "updates"]
            assert kwargs["version"] == "v2"
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
