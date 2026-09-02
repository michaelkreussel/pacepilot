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
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.logging import FILE_HANDLER, configure_logging
from app.main import app
from app.models import (
    Activity,
    AthleteAvailability,
    AthleteGoal,
    CoachConversation,
    CoachMessage,
    CoachToolCall,
    DailyHealth,
    GarminSyncState,
    PerformanceAnchor,
    PostSessionFeedback,
    PreSessionFeedback,
    TrainingCycle,
    TrainingCycleRevision,
    User,
    Workout,
    WorkoutEvent,
    WorkoutRevision,
)
from app.models.user import utcnow
from app.routes import coach as coach_route_module
from app.services.analytics.health_trends import HEALTH_METRICS
from app.services.coach import dependencies as coach_dependencies_module
from app.services.coach import provider as coach_provider_module
from app.services.coach import tools as coach_operations
from app.services.coach.agent import CoachEvent
from app.services.coach.conversation import CoachHistoryMessage, CoachRuntimeContext
from app.services.coach.dependencies import get_coach_agent_factory
from app.services.coach.presentation import (
    planning_artifact_presentations,
    workout_artifact_presentation,
)
from app.services.coach.provider import OpenRouterCoachProvider, coach_tools
from app.services.coach.tools import (
    create_planning_anchor,
    create_planning_goal,
    create_running_workout_proposal,
    get_adaptive_context,
    get_health_day,
    get_planning_inputs,
    get_progress,
    get_revisable_running_workouts,
    get_subjective_context,
    revise_running_workout_proposal,
    set_planning_availability,
    update_planning_profile,
)
from app.services.planning import workout_proposals as workout_proposals_module
from app.services.planning.planning_commands import (
    GoalUpdateInput,
    PerformanceAnchorUpdateInput,
    PlanningProfileUpdateInput,
)
from app.services.planning.registry import WORKOUT_FORMAT_IDS
from app.services.planning.safety_triage import IllnessSignal, PainInput
from app.services.planning.workout_proposals import RunningProposalRequest, RunningProposalService
from app.services.planning.workout_revision import AcceptRevisionCommand, RevisionIdentity
from app.services.planning.workout_service import WorkoutService
from app.services.planning.workout_views import workout_lifecycle_projection


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
        yield CoachEvent("answer_text", text="Du wirkst heute etwas weniger erholt. ")
        yield CoachEvent("answer_text", text="Dein Ruhepuls liegt leicht über deinem Basiswert.")
        yield CoachEvent("completed")


def _sse_payload(stream: str, event_name: str) -> dict[str, object]:
    for block in stream.split("\n\n"):
        lines = block.splitlines()
        if f"event: {event_name}" not in lines:
            continue
        data = "\n".join(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        return cast(dict[str, object], json.loads(data))
    raise AssertionError(f"Missing SSE event: {event_name}")


@pytest.mark.parametrize(
    ("approval_status", "status", "schedule_status", "accepted_revision_id", "key", "label"),
    [
        ("proposed", "draft", "unscheduled", None, "draft", "Unbestätigt"),
        ("accepted", "confirmed", "unscheduled", 1, "accepted", "Angenommen"),
        ("accepted", "confirmed", "scheduled", 1, "scheduled", "Eingeplant"),
        ("accepted", "published", "scheduled", 1, "published", "Bei Garmin"),
        ("accepted", "pushed", "scheduled", 1, "pushed", "An Uhr gesendet"),
        ("rejected", "draft", "unscheduled", None, "rejected", "Abgelehnt"),
    ],
)
def test_workout_lifecycle_projection_is_canonical(
    approval_status: str,
    status: str,
    schedule_status: str,
    accepted_revision_id: int | None,
    key: str,
    label: str,
) -> None:
    workout = cast(
        Workout,
        SimpleNamespace(
            approval_status=approval_status,
            status=status,
            local_schedule_status=schedule_status,
            accepted_revision_id=accepted_revision_id,
        ),
    )

    projection = workout_lifecycle_projection(workout)

    assert (projection.key, projection.label) == (key, label)


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
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class EmptyAgent:
        async def astream(self, *_: Any, **__: Any) -> AsyncIterator[object]:
            if False:
                yield object()

    def fake_create_agent(model: object, *_: Any, **__: Any) -> EmptyAgent:
        captured["model"] = model
        return EmptyAgent()

    monkeypatch.setattr(coach_provider_module, "create_agent", fake_create_agent)

    provider = OpenRouterCoachProvider(
        api_key="test-key", model_id="test/model", timeout_seconds=60
    )
    assert captured == {}

    async def collect() -> list[CoachEvent]:
        return [
            event
            async for event in provider.stream(
                [CoachHistoryMessage("user", "Test")],
                CoachRuntimeContext(1, date(2026, 8, 11), session_factory),
            )
        ]

    assert asyncio.run(collect()) == [CoachEvent("failed", failure_category="missing_final_answer")]

    model: Any = captured["model"]
    assert type(model).__name__ == "_ToolMarkupAdapter"
    assert model.inner.request_timeout == 60_000
    assert model.inner.max_tokens == 4000
    assert model.inner.reasoning == {"effort": "low"}
    assert model.inner.openrouter_provider == {
        "order": ["z-ai"],
        "allow_fallbacks": False,
    }


def test_provider_failure_logs_only_a_privacy_safe_category(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingAgent:
        async def astream(self, *_: Any, **__: Any) -> AsyncIterator[object]:
            raise RuntimeError("private provider response detail")
            yield object()

    monkeypatch.setattr(
        coach_provider_module,
        "create_agent",
        lambda *_args, **_kwargs: FailingAgent(),
    )
    provider = OpenRouterCoachProvider(
        api_key="test-key", model_id="test/model", timeout_seconds=60
    )
    caplog.set_level(logging.WARNING, logger=coach_provider_module.__name__)

    async def collect() -> list[CoachEvent]:
        return [
            event
            async for event in provider.stream(
                [CoachHistoryMessage("user", "Test")],
                CoachRuntimeContext(1, date(2026, 8, 28), session_factory),
            )
        ]

    assert asyncio.run(collect()) == [CoachEvent("failed", failure_category="provider_error")]
    assert "failure_category=provider_error" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "error_source=test_training_agent.astream:" in caplog.text
    assert "private provider response detail" not in caplog.text


class _ReasoningFakeChatModel(BaseChatModel):
    """Streams reasoning via additional_kwargs before the visible answer text."""

    answer: str = "Antwort."

    @property
    def _llm_type(self) -> str:
        return "reasoning-fake"

    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        del tools, kwargs
        return self

    async def _astream(
        self,
        messages: Any,
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del messages, stop, kwargs
        for reasoning_token in ("Denke", " nach."):
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    additional_kwargs={"reasoning_content": reasoning_token},
                )
            )
            if run_manager is not None:
                await run_manager.on_llm_new_token("", chunk=chunk)
            yield chunk
        for answer_token in self.answer:
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=answer_token))
            if run_manager is not None:
                await run_manager.on_llm_new_token(answer_token, chunk=chunk)
            yield chunk


class _BindingRecordingFakeChatModel(BaseChatModel):
    """Uses the default bind_tools so binding produces a real RunnableBinding."""

    observed_astream_kwargs: list[dict[str, Any]] = []
    observed_generate_kwargs: list[dict[str, Any]] = []

    @property
    def _llm_type(self) -> str:
        return "binding-recording-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self.bind(
            tools=[convert_to_openai_tool(tool) for tool in tools],
            **kwargs,
        )

    def _generate(
        self,
        messages: Any,
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        del messages, run_manager
        self.observed_generate_kwargs.append(kwargs)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    async def _astream(
        self,
        messages: Any,
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del messages, stop
        self.observed_astream_kwargs.append(kwargs)
        if run_manager is not None:
            await run_manager.on_llm_new_token(
                "ok", chunk=ChatGenerationChunk(message=AIMessageChunk(content="ok"))
            )
        yield ChatGenerationChunk(message=AIMessageChunk(content="ok", chunk_position="last"))


@pytest.mark.asyncio
async def test_tool_markup_adapter_preserves_bound_tools_on_model_requests() -> None:
    @tool
    def get_recent_activities(limit: int = 5) -> str:
        """List recent activities."""

        return "[]"

    inner = _BindingRecordingFakeChatModel()
    bound = coach_provider_module._ToolMarkupAdapter(inner=inner).bind_tools(
        [get_recent_activities]
    )

    chunks = [chunk async for chunk in bound.astream([("user", "Test")])]
    assert [chunk.content for chunk in chunks] == ["ok"]
    assert inner.observed_astream_kwargs and "tools" in inner.observed_astream_kwargs[0]

    message = await bound.ainvoke([("user", "Test")])
    assert message.content == "ok"
    assert inner.observed_generate_kwargs and "tools" in inner.observed_generate_kwargs[0]


class _ToolCallingFakeChatModel(BaseChatModel):
    tool_name: str
    tool_args: dict[str, Any]
    answer: str
    observed_tool_result: str | None = None

    @property
    def _llm_type(self) -> str:
        return "tool-calling-fake"

    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        del tools, kwargs
        return self

    async def _astream(
        self,
        messages: Any,
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del stop, kwargs
        tool_result = next(
            (message for message in reversed(messages) if isinstance(message, ToolMessage)),
            None,
        )
        if tool_result is None:
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": self.tool_name,
                            "args": json.dumps(self.tool_args),
                            "id": "fake-tool-call",
                            "index": 0,
                        }
                    ],
                    chunk_position="last",
                )
            )
        else:
            self.observed_tool_result = str(tool_result.content)
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(content=self.answer, chunk_position="last")
            )
        if run_manager is not None:
            await run_manager.on_llm_new_token(chunk.text, chunk=chunk)
        yield chunk


class _DeferredThenToolCallingFakeChatModel(BaseChatModel):
    invocation_count: int = 0
    first_answer: str = "Ich schaue kurz deine letzte Aktivität raus."

    @property
    def _llm_type(self) -> str:
        return "deferred-then-tool-calling-fake"

    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        del tools, kwargs
        return self

    async def _astream(
        self,
        messages: Any,
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del stop, kwargs
        self.invocation_count += 1
        tool_result = next(
            (message for message in reversed(messages) if isinstance(message, ToolMessage)),
            None,
        )
        if self.invocation_count == 1:
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(
                    content=self.first_answer,
                    chunk_position="last",
                )
            )
        elif tool_result is None:
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "record_post_session_feedback",
                            "args": "{}",
                            "id": "deferred-tool-call",
                            "index": 0,
                        }
                    ],
                    chunk_position="last",
                )
            )
        else:
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(
                    content="Dein Feedback wurde gespeichert.",
                    chunk_position="last",
                )
            )
        if run_manager is not None:
            await run_manager.on_llm_new_token(chunk.text, chunk=chunk)
        yield chunk


class _FailingFakeChatModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "failing-fake"

    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        del tools, kwargs
        return self

    async def _astream(self, *_: Any, **__: Any) -> AsyncIterator[ChatGenerationChunk]:
        raise RuntimeError("secret provider detail")
        yield ChatGenerationChunk(message=AIMessageChunk(content=""))


def test_agent_stream_never_leaks_reasoning_into_answer(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coach_provider_module, "ChatOpenRouter", lambda **_: _ReasoningFakeChatModel()
    )
    agent = OpenRouterCoachProvider(api_key="test-key", model_id="fake/model", timeout_seconds=5)

    with session_factory() as session:
        user = User(display_name="Reasoning Runner")
        session.add(user)
        session.commit()
        user_id = user.id
    runtime = CoachRuntimeContext(
        user_id=user_id,
        as_of=date.today(),
        session_factory=session_factory,
    )

    async def collect() -> list[CoachEvent]:
        return [
            event
            async for event in agent.stream(
                [CoachHistoryMessage(role="user", content="Hallo")], runtime
            )
        ]

    events = asyncio.run(collect())

    answer = "".join(event.text for event in events if event.type == "answer_text" and event.text)
    assert answer == "Antwort."
    assert "Denke" not in answer
    assert events[-1] == CoachEvent("completed")


def test_agent_stream_fails_when_model_emits_unparseable_tool_call_markup(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _ToolCallingFakeChatModel(
        tool_name="get_planning_inputs",
        tool_args={},
        answer='<｜DSML｜invoke name="get_planning_inputs">{"weekday": }',
    )
    monkeypatch.setattr(coach_provider_module, "ChatOpenRouter", lambda **_: model)
    agent = OpenRouterCoachProvider(api_key="test-key", model_id="fake/model", timeout_seconds=5)

    with session_factory() as session:
        user = User(display_name="Markup Runner")
        session.add(user)
        session.commit()
        user_id = user.id
    runtime = CoachRuntimeContext(
        user_id=user_id,
        as_of=date.today(),
        session_factory=session_factory,
    )

    async def collect() -> list[CoachEvent]:
        return [
            event
            async for event in agent.stream(
                [CoachHistoryMessage(role="user", content="Mittwochs kann ich 75 Minuten?")],
                runtime,
            )
        ]

    events = asyncio.run(collect())

    assert events[-1] == CoachEvent("failed", failure_category="tool_call_format")
    assert not any(event.type == "completed" for event in events)


def test_dsml_tool_call_markup_is_parsed_into_bounded_tool_calls() -> None:
    parsed = coach_provider_module._parse_dsml_tool_calls(
        "<｜DSML｜tool_calls>"
        '<｜DSML｜invoke name="get_planning_inputs"></｜DSML｜invoke>'
        "</｜DSML｜tool_calls>"
    )
    assert parsed == [
        {"name": "get_planning_inputs", "args": {}, "id": "dsml-tool-1", "type": "tool_call"}
    ]

    with_args = coach_provider_module._parse_dsml_tool_calls(
        '<｜DSML｜invoke name="set_planning_availability">'
        '{"weekday": 2, "available": true, "available_minutes": 75}'
        "</｜DSML｜invoke>"
    )
    assert with_args == [
        {
            "name": "set_planning_availability",
            "args": {"weekday": 2, "available": True, "available_minutes": 75},
            "id": "dsml-tool-1",
            "type": "tool_call",
        }
    ]

    assert coach_provider_module._parse_dsml_tool_calls("Normale Antwort.") is None
    assert (
        coach_provider_module._parse_dsml_tool_calls(
            '<｜DSML｜invoke name="get_planning_inputs">{"weekday": }</｜DSML｜invoke>'
        )
        is None
    )


def test_agent_converts_model_tool_call_markup_into_structured_tool_call(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DsmlFakeChatModel(BaseChatModel):
        observed_tool_result: str | None = None

        @property
        def _llm_type(self) -> str:
            return "dsml-fake"

        def _generate(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            del tools, kwargs
            return self

        async def _astream(
            self,
            messages: Any,
            stop: Any = None,
            run_manager: Any = None,
            **kwargs: Any,
        ) -> AsyncIterator[ChatGenerationChunk]:
            del stop, kwargs
            tool_result = next(
                (message for message in reversed(messages) if isinstance(message, ToolMessage)),
                None,
            )
            if tool_result is None:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=(
                            "<｜DSML｜tool_calls>"
                            '<｜DSML｜invoke name="get_planning_inputs"></｜DSML｜invoke>'
                            "</｜DSML｜tool_calls>"
                        )
                    )
                )
            else:
                self.observed_tool_result = str(tool_result.content)
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="Hier sind deine aktuellen Planungsdaten.",
                        chunk_position="last",
                    )
                )

    model = _DsmlFakeChatModel()
    monkeypatch.setattr(coach_provider_module, "ChatOpenRouter", lambda **_: model)
    agent = OpenRouterCoachProvider(api_key="test-key", model_id="fake/model", timeout_seconds=5)

    with session_factory() as session:
        user = User(display_name="DSML Runner")
        session.add(user)
        session.commit()
        user_id = user.id
    runtime = CoachRuntimeContext(
        user_id=user_id,
        as_of=date(2026, 8, 29),
        session_factory=session_factory,
    )

    async def collect() -> list[CoachEvent]:
        return [
            event
            async for event in agent.stream(
                [CoachHistoryMessage(role="user", content="Welche Planungsdaten habe ich?")],
                runtime,
            )
        ]

    events = asyncio.run(collect())

    assert events[-1] == CoachEvent("completed")
    assert model.observed_tool_result is not None
    assert '"goals"' in model.observed_tool_result
    answer = "".join(event.text for event in events if event.type == "answer_text" and event.text)
    assert answer == "Hier sind deine aktuellen Planungsdaten."
    assert "DSML" not in answer


def test_agent_stream_fails_when_final_answer_is_missing_after_tool_call(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ScriptedAgentGraph:
        async def astream(self, *_args: Any, **kwargs: Any) -> AsyncIterator[object]:
            del kwargs
            yield (
                "messages",
                (
                    AIMessageChunk(content="Ich prüfe kurz deine Planungsdaten."),
                    {"langgraph_node": "model"},
                ),
            )
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "name": "get_planning_inputs",
                                        "args": {},
                                        "id": "scripted-tool-1",
                                        "type": "tool_call",
                                    }
                                ],
                            )
                        ]
                    }
                },
            )
            yield (
                "updates",
                {
                    "tools": {
                        "messages": [
                            ToolMessage(
                                content='{"goals": []}',
                                name="get_planning_inputs",
                                tool_call_id="scripted-tool-1",
                            )
                        ]
                    }
                },
            )

    monkeypatch.setattr(
        coach_provider_module, "create_agent", lambda *_, **__: _ScriptedAgentGraph()
    )
    agent = OpenRouterCoachProvider(api_key="test-key", model_id="fake/model", timeout_seconds=5)

    with session_factory() as session:
        user = User(display_name="Silent End Runner")
        session.add(user)
        session.commit()
        user_id = user.id
    runtime = CoachRuntimeContext(
        user_id=user_id,
        as_of=date.today(),
        session_factory=session_factory,
    )

    async def collect() -> list[CoachEvent]:
        return [
            event
            async for event in agent.stream(
                [CoachHistoryMessage(role="user", content="Mittwochs kann ich 75 Minuten?")],
                runtime,
            )
        ]

    events = asyncio.run(collect())

    assert events[-1] == CoachEvent("failed", failure_category="missing_final_answer")
    assert not any(event.type == "completed" for event in events)
    answer = "".join(event.text for event in events if event.type == "answer_text" and event.text)
    assert "Ich prüfe kurz deine Planungsdaten." in answer


def _new_chat(client: TestClient) -> int:
    response = client.post("/coach/conversations", follow_redirects=False)
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[1])


def _running_history(session: Session, user_id: int, *, as_of: date | None = None) -> None:
    as_of = as_of or date.today()
    for index, age in enumerate((0, 7, 14, 21, 28, 35)):
        day = as_of - timedelta(days=age)
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
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: fake
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Wie erholt bin ich heute?"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: answer.delta" in response.text
    assert "event: answer.completed" in response.text
    assert "Du wirkst heute etwas weniger erholt" in response.text
    assert fake.calls[0] == [CoachHistoryMessage("user", "Wie erholt bin ich heute?")]
    started = _sse_payload(response.text, "run.started")
    completed = _sse_payload(response.text, "answer.completed")
    assert started["conversation_title"] == "Wie erholt bin ich heute?"
    assert 'data-message-state="streaming"' in cast(str, started["assistant_html"])
    assert 'data-message-state="completed"' in cast(str, completed["html"])

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
        assert session.scalar(select(Workout)) is None

    page = client.get(f"/coach/{conversation_id}").text
    assert 'aria-label="Neuen Chat starten"' in page
    assert 'aria-label="Chat löschen"' in page
    assert "data-coach-message-list" in page
    assert "Nur lesend" in page
    assert "data-coach-activity" in page
    assert cast(str, started["user_html"]) in page
    assert cast(str, completed["html"]) in page


def test_coach_tool_creates_one_durable_server_rendered_proposal(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", True)
    coaching_date = date(2026, 8, 28)

    class TurnStartDate(date):
        @classmethod
        def today(cls) -> date:
            return coaching_date

    class AfterMidnightDate(date):
        @classmethod
        def today(cls) -> date:
            return coaching_date + timedelta(days=1)

    monkeypatch.setattr(coach_route_module, "date", TurnStartDate)
    monkeypatch.setattr(workout_proposals_module, "date", AfterMidnightDate)

    class ProposalAgent:
        runtime: CoachRuntimeContext | None = None

        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            self.runtime = runtime
            rejected = json.loads(
                create_running_workout_proposal(
                    runtime=runtime,
                    suggested_for=runtime.as_of - timedelta(days=1),
                    available_minutes=60,
                )
            )
            assert rejected == {
                "status": "not_created",
                "error": {
                    "code": "proposal.date_in_past",
                    "message": "Das vorgeschlagene Datum darf nicht in der Vergangenheit liegen.",
                },
            }
            first = create_running_workout_proposal(
                runtime=runtime,
                suggested_for=runtime.as_of + timedelta(days=1),
                available_minutes=60,
            )
            second = create_running_workout_proposal(
                runtime=runtime,
                suggested_for=runtime.as_of + timedelta(days=1),
                available_minutes=60,
            )
            assert json.loads(first)["artifact"] == json.loads(second)["artifact"]
            yield CoachEvent("artifact_available", artifact_type="workout")
            yield CoachEvent("artifact_available", artifact_type="workout")
            yield CoachEvent("answer_text", text="Ich habe einen Vorschlag vorbereitet.")
            yield CoachEvent("completed")

    fake = ProposalAgent()
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: fake
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        _running_history(session, user.id, as_of=coaching_date)
        latest_run = session.scalar(
            select(Activity).where(Activity.user_id == user.id).order_by(Activity.started_at.desc())
        )
        assert latest_run is not None
        latest_run.workout_rpe = 8
        session.commit()
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Erstelle mir morgen einen lockeren Lauf für 60 Minuten."},
    )

    assert response.status_code == 200
    assert response.text.count("event: proposal.created") == 1
    assert '"card_url":"/coach/' in response.text
    assert fake.runtime is not None
    assert fake.runtime.as_of == coaching_date
    assistant_message_id = fake.runtime.assistant_message_id
    user_message_id = fake.runtime.user_message_id
    assert assistant_message_id is not None
    assert user_message_id is not None
    assert f'"source_message_id":{assistant_message_id}' in response.text

    with session_factory() as session:
        assistant_message = session.get(CoachMessage, assistant_message_id)
        assert assistant_message is not None
        assert assistant_message.status == "completed"
        workout = session.scalar(
            select(Workout).where(Workout.source_assistant_message_id == assistant_message_id)
        )
        assert workout is not None
        assert workout.originating_conversation_id == conversation_id
        assert workout.originating_user_message_id == user_message_id
        assert workout.originating_assistant_message_id == assistant_message_id
        assert workout.source_assistant_message_id == assistant_message_id
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
        propose_event = session.scalar(
            select(WorkoutEvent).where(
                WorkoutEvent.workout_id == workout.id,
                WorkoutEvent.action == "propose",
            )
        )
        assert propose_event is not None
        assert propose_event.idempotency_key == (
            f"coach-message:{assistant_message_id}:{assistant_message.created_at.isoformat()}:"
            "create_running_workout_proposal:v2"
        )
        assert len(list(session.scalars(select(Workout)))) == 1
        workout_id = workout.id
        revision = session.get(WorkoutRevision, workout.current_revision_id)
        assert revision is not None
        assert revision.generation_context_json is not None
        assert revision.generation_context_json["as_of"] == coaching_date.isoformat()
        assert (
            revision.model_provider,
            revision.model_id,
            revision.prompt_template_version,
        ) == (
            "openrouter",
            assistant_message.model_id,
            assistant_message.prompt_template_version,
        )
        artifact = workout_artifact_presentation(
            session,
            user.id,
            conversation_id,
            assistant_message_id,
        )
        assert artifact is not None
        assert (
            artifact.artifact_type,
            artifact.workout_id,
            artifact.source_assistant_message_id,
            artifact.revision_id,
            artifact.accepted_revision_id,
        ) == ("workout", workout.id, assistant_message.id, revision.id, None)
        assert artifact.lifecycle.key == "draft"
        assert [
            (action.key, action.endpoint, action.revision_id, action.scheduled_for)
            for action in artifact.lifecycle_actions
        ] == [
            ("accept", f"/workouts/{workout.id}/confirm", revision.id, revision.suggested_for),
            ("reject", f"/workouts/{workout.id}/reject", revision.id, revision.suggested_for),
        ]
        assert artifact.warning is not None
        assert artifact.warning.outcome == "caution"
        assert artifact.warning.evidence is not None
        assert artifact.warning.evidence.assessed_on == coaching_date
        assert artifact.warning.coverage_percent is not None
        assert artifact.warning.recommendation
        assert artifact.warning.safer_alternative
        assert artifact.warning_acknowledgement is None

        other_user = User(display_name="Andere Person")
        session.add(other_user)
        session.flush()
        assert (
            workout_artifact_presentation(
                session,
                other_user.id,
                conversation_id,
                assistant_message_id,
            )
            is None
        )

    page = client.get(f"/coach/{conversation_id}")
    assert page.status_code == 200
    assert "Trainingsvorschlag" in page.text
    assert "60 Minuten" in page.text
    assert "Unbestätigt" in page.text
    assert f'href="/workouts/{workout_id}"' in page.text
    card = client.get(f"/coach/{conversation_id}/messages/{assistant_message_id}/proposal-card")
    assert card.status_code == 200
    assert f'data-workout-id="{workout_id}"' in card.text
    assert (
        client.get(f"/coach/999/messages/{assistant_message_id}/proposal-card").status_code == 404
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
        f"/coach/{conversation_id}/messages/{assistant_message_id}/proposal-card"
    )
    assert "Eingeplant" in updated_card.text
    assert "im lokalen Kalender eingeplant" in updated_card.text
    assert "weder angenommen noch eingeplant" not in updated_card.text

    conflict = json.loads(
        create_running_workout_proposal(
            runtime=fake.runtime,
            suggested_for=date.today() + timedelta(days=2),
            available_minutes=60,
        )
    )
    assert conflict["status"] == "not_created"
    assert conflict["error"]["code"] == "proposal.idempotency_conflict"
    with session_factory() as session:
        assert len(list(session.scalars(select(Workout)))) == 1

    deleted = client.post(f"/coach/{conversation_id}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    with session_factory() as session:
        assert session.get(CoachMessage, assistant_message_id) is None
        workout = session.get(Workout, workout_id)
        assert workout is not None
        assert workout.originating_conversation_id is None
        assert workout.originating_user_message_id is None
        assert workout.originating_assistant_message_id is None
        assert workout.source_assistant_message_id is None


def test_conversation_updates_availability_with_durable_server_artifact(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    class AvailabilityAgent:
        runtime: CoachRuntimeContext | None = None

        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            self.runtime = runtime
            result = json.loads(
                set_planning_availability(
                    runtime=runtime,
                    weekday=2,
                    available=True,
                    available_minutes=75,
                )
            )
            assert result == {
                "status": "updated",
                "artifact": {"type": "planning_input", "resource": "availability"},
            }
            assert (
                json.loads(
                    set_planning_availability(
                        runtime=runtime,
                        weekday=2,
                        available=True,
                        available_minutes=75,
                    )
                )
                == result
            )
            yield CoachEvent("artifact_available", artifact_type="planning_input")
            yield CoachEvent("answer_text", text="Deine Verfügbarkeit wurde gespeichert.")
            yield CoachEvent("completed")

    fake = AvailabilityAgent()
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: fake
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Mittwochs kann ich 75 Minuten trainieren."},
    )

    assert response.status_code == 200
    completed = _sse_payload(response.text, "answer.completed")
    completed_html = cast(str, completed["html"])
    assert "Verfügbarkeit aktualisiert" in completed_html
    assert "Mittwoch" in completed_html
    assert "75 Minuten" in completed_html
    assert fake.runtime is not None and fake.runtime.assistant_message_id is not None

    with session_factory() as session:
        availability = session.scalar(select(AthleteAvailability))
        assert availability is not None
        assert (
            availability.weekday,
            availability.available,
            availability.available_minutes,
        ) == (2, True, 75)
        assistant = session.get(CoachMessage, fake.runtime.assistant_message_id)
        assert assistant is not None
        assert assistant.artifacts_json == [
            {
                "type": "planning_input",
                "resource": "availability",
                "operation": "set_planning_availability",
                "request": {"weekday": 2, "available": True, "available_minutes": 75},
                "result": {"weekday": 2, "available": True, "available_minutes": 75},
            }
        ]
        user = session.scalar(select(User))
        assert user is not None
        other_user = User(display_name="Andere Person")
        session.add(other_user)
        session.flush()
        assert planning_artifact_presentations(session, other_user.id, [assistant]) == {}

    reloaded = client.get(f"/coach/{conversation_id}")
    assert reloaded.status_code == 200
    assert completed_html in reloaded.text


def test_conversation_records_pre_and_post_feedback_with_durable_artifacts(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        workout = Workout(
            user_id=user.id,
            name="Morgenlauf",
            sport="running",
            definition_version=1,
            definition={"blocks": []},
            source_type="manual",
        )
        activity = Activity(
            user_id=user.id,
            garmin_activity_id="coach-feedback-run",
            name="Abendlauf",
            activity_type="running",
            started_at=utcnow(),
        )
        session.add_all([workout, activity])
        session.commit()
        workout_id = workout.id
        activity_id = activity.id

    class FeedbackAgent:
        call_count = 0
        later_context: dict[str, object] | None = None

        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            if self.call_count == 0:
                result = json.loads(
                    coach_operations.record_pre_session_feedback(
                        runtime,
                        workout_id=workout_id,
                        pain=PainInput(
                            present=True,
                            location="Knie",
                            severity=3,
                            alters_gait=False,
                            worsens_with_activity=False,
                        ),
                        illness_signal=IllnessSignal.NONE,
                        available_minutes=35,
                        notes="Heute nur locker.",
                    )
                )
                assert result == {
                    "status": "recorded",
                    "artifact": {"type": "feedback", "resource": "pre_session"},
                }
                assert (
                    json.loads(
                        coach_operations.record_pre_session_feedback(
                            runtime,
                            workout_id=workout_id,
                            pain=PainInput(
                                present=True,
                                location="Knie",
                                severity=3,
                                alters_gait=False,
                                worsens_with_activity=False,
                            ),
                            illness_signal=IllnessSignal.NONE,
                            available_minutes=35,
                            notes="Heute nur locker.",
                        )
                    )
                    == result
                )
                answer = "Dein Feedback vor dem Training wurde gespeichert."
            else:
                result = json.loads(
                    coach_operations.record_post_session_feedback(
                        runtime,
                        activity_id=activity_id,
                        completion_percent=80,
                        session_rpe=8,
                        overall_feel=2,
                        pain=PainInput(present=False),
                        stopped_reason="Zu müde",
                        notes="Bewusst verkürzt.",
                    )
                )
                assert result == {
                    "status": "recorded",
                    "artifact": {"type": "feedback", "resource": "post_session"},
                }
                assert (
                    json.loads(
                        coach_operations.record_post_session_feedback(
                            runtime,
                            activity_id=activity_id,
                            completion_percent=80,
                            session_rpe=8,
                            overall_feel=2,
                            pain=PainInput(present=False),
                            stopped_reason="Zu müde",
                            notes="Bewusst verkürzt.",
                        )
                    )
                    == result
                )
                self.later_context = json.loads(get_subjective_context(runtime))
                answer = "Dein Feedback nach dem Training wurde gespeichert."
            self.call_count += 1
            yield CoachEvent("artifact_available", artifact_type="feedback")
            yield CoachEvent("answer_text", text=answer)
            yield CoachEvent("completed")

    fake = FeedbackAgent()
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: fake
    conversation_id = _new_chat(client)

    pre_response = client.post(
        f"/coach/{conversation_id}/messages",
        data={
            "message": (
                "Vor Workout "
                f"{workout_id}: Knie 3 von 10, kein verändertes Gangbild, nicht krank, "
                "35 Minuten verfügbar. Heute nur locker."
            )
        },
    )
    post_response = client.post(
        f"/coach/{conversation_id}/messages",
        data={
            "message": (
                f"Aktivität {activity_id}: 80 Prozent, RPE 8, Gefühl 2 von 5, "
                "keine Schmerzen, wegen Müdigkeit bewusst verkürzt."
            )
        },
    )

    assert pre_response.status_code == post_response.status_code == 200
    pre_html = cast(str, _sse_payload(pre_response.text, "answer.completed")["html"])
    post_html = cast(str, _sse_payload(post_response.text, "answer.completed")["html"])
    assert "Feedback vor dem Training gespeichert" in pre_html
    assert f"Workout #{workout_id}" in pre_html
    assert "35 Minuten" in pre_html
    assert "Knie · 3/10" in pre_html
    assert "Feedback nach dem Training gespeichert" in post_html
    assert f"Aktivität #{activity_id}" in post_html
    assert "80 %" in post_html
    assert "8/10" in post_html
    assert "Zu müde" in post_html

    assert fake.later_context is not None
    pre_context = cast(list[dict[str, object]], fake.later_context["recent_pre_session_feedback"])
    post_context = cast(list[dict[str, object]], fake.later_context["recent_post_session_feedback"])
    activity_context = cast(list[dict[str, object]], fake.later_context["recent_activity_feedback"])
    assert pre_context[0]["workout_id"] == workout_id
    assert pre_context[0]["pain_severity"] == 3
    assert pre_context[0]["available_minutes"] == 35
    assert post_context[0]["activity_id"] == activity_id
    assert post_context[0]["completion_percent"] == 80
    assert post_context[0]["stopped_reason"] == "Zu müde"
    assert activity_context[0]["effort"] == 8
    assert activity_context[0]["feel"] == 2

    with session_factory() as session:
        pre = list(session.scalars(select(PreSessionFeedback)))
        post = list(session.scalars(select(PostSessionFeedback)))
        assert len(pre) == len(post) == 1
        assistants = list(
            session.scalars(
                select(CoachMessage)
                .where(
                    CoachMessage.conversation_id == conversation_id,
                    CoachMessage.role == "assistant",
                )
                .order_by(CoachMessage.id)
            )
        )
        assert [item.artifacts_json[0]["resource"] for item in assistants] == [
            "pre_session",
            "post_session",
        ]
        pre_result = cast(dict[str, object], assistants[0].artifacts_json[0]["result"])
        post_result = cast(dict[str, object], assistants[1].artifacts_json[0]["result"])
        assert pre_result["feedback_id"] == pre[0].id
        assert post_result["feedback_id"] == post[0].id
        other_user = User(display_name="Andere Person")
        session.add(other_user)
        session.flush()
        assert planning_artifact_presentations(session, other_user.id, assistants) == {}

    reloaded = client.get(f"/coach/{conversation_id}")
    assert reloaded.status_code == 200
    assert pre_html in reloaded.text
    assert post_html in reloaded.text


@pytest.mark.parametrize("ambiguous_kind", ["pain", "illness"])
def test_ambiguous_conversational_health_feedback_asks_once_and_writes_nothing(
    ambiguous_kind: str,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        workout = Workout(
            user_id=user.id,
            name="Testlauf",
            sport="running",
            definition_version=1,
            definition={"blocks": []},
            source_type="manual",
        )
        session.add(workout)
        session.commit()
        workout_id = workout.id

    class AmbiguousFeedbackAgent:
        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            if ambiguous_kind == "pain":
                result = json.loads(
                    coach_operations.record_pre_session_feedback(
                        runtime,
                        workout_id=workout_id,
                        pain=PainInput(present=True, location="Knie"),
                    )
                )
            else:
                result = json.loads(
                    coach_operations.record_pre_session_feedback(
                        runtime,
                        workout_id=workout_id,
                        illness_signal=IllnessSignal.UNKNOWN,
                    )
                )
            assert result["status"] == "needs_clarification"
            assert set(result) == {"status", "question"}
            yield CoachEvent("answer_text", text=cast(str, result["question"]))
            yield CoachEvent("completed")

    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: AmbiguousFeedbackAgent()
    conversation_id = _new_chat(client)
    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Ich habe Beschwerden."},
    )

    assert response.status_code == 200
    assert response.text.count("event: answer.delta") == 1
    with session_factory() as session:
        assert list(session.scalars(select(PreSessionFeedback))) == []
        assistant = session.scalar(
            select(CoachMessage).where(
                CoachMessage.conversation_id == conversation_id,
                CoachMessage.role == "assistant",
            )
        )
        assert assistant is not None and assistant.artifacts_json == []


def test_conversational_feedback_rejects_cross_user_targets(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        other = User(display_name="Andere Person")
        session.add(other)
        session.flush()
        workout = Workout(
            user_id=other.id,
            name="Fremder Lauf",
            sport="running",
            definition_version=1,
            definition={"blocks": []},
            source_type="manual",
        )
        activity = Activity(
            user_id=other.id,
            garmin_activity_id="foreign-feedback-run",
            name="Fremde Aktivität",
            activity_type="running",
            started_at=utcnow(),
        )
        session.add_all([workout, activity])
        session.commit()
        workout_id = workout.id
        activity_id = activity.id

    class CrossUserFeedbackAgent:
        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            pre = json.loads(
                coach_operations.record_pre_session_feedback(
                    runtime,
                    workout_id=workout_id,
                    available_minutes=45,
                )
            )
            post = json.loads(
                coach_operations.record_post_session_feedback(
                    runtime,
                    activity_id=activity_id,
                    session_rpe=7,
                )
            )
            assert pre["error"]["code"] == "feedback.workout_not_found"
            assert post["error"]["code"] == "feedback.activity_not_found"
            yield CoachEvent("answer_text", text="Dieses Training wurde nicht gefunden.")
            yield CoachEvent("completed")

    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: CrossUserFeedbackAgent()
    conversation_id = _new_chat(client)
    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Speichere Feedback für fremde Daten."},
    )

    assert response.status_code == 200
    assert "event: answer.completed" in response.text
    assert "Dieses Training wurde nicht gefunden." in response.text
    with session_factory() as session:
        assert list(session.scalars(select(PreSessionFeedback))) == []
        assert list(session.scalars(select(PostSessionFeedback))) == []
        assistant = session.scalar(
            select(CoachMessage).where(
                CoachMessage.conversation_id == conversation_id,
                CoachMessage.role == "assistant",
            )
        )
        assert assistant is not None and assistant.artifacts_json == []


def test_ambiguous_availability_returns_one_question_and_persists_nothing(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    class AmbiguousAvailabilityAgent:
        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            result = json.loads(
                set_planning_availability(
                    runtime=runtime,
                    weekday=4,
                    available=True,
                    available_minutes=None,
                )
            )
            assert result == {
                "status": "needs_clarification",
                "question": "Wie viele Minuten kannst du am Freitag trainieren?",
            }
            yield CoachEvent("answer_text", text=result["question"])
            yield CoachEvent("completed")

    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: AmbiguousAvailabilityAgent()
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Freitags kann ich trainieren."},
    )

    assert response.status_code == 200
    assert response.text.count("Wie viele Minuten kannst du am Freitag trainieren?") == 2
    with session_factory() as session:
        assert session.scalar(select(AthleteAvailability)) is None
        assistant = session.scalar(
            select(CoachMessage).where(
                CoachMessage.conversation_id == conversation_id,
                CoachMessage.role == "assistant",
            )
        )
        assert assistant is not None
        assert assistant.artifacts_json == []


def test_distinct_availability_changes_in_one_run_each_persist(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    class TwoAvailabilityAgent:
        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            assert (
                json.loads(
                    set_planning_availability(
                        runtime=runtime, weekday=0, available=True, available_minutes=45
                    )
                )["status"]
                == "updated"
            )
            assert (
                json.loads(
                    set_planning_availability(
                        runtime=runtime, weekday=2, available=True, available_minutes=60
                    )
                )["status"]
                == "updated"
            )
            assert (
                json.loads(
                    set_planning_availability(
                        runtime=runtime, weekday=0, available=True, available_minutes=45
                    )
                )["status"]
                == "updated"
            )
            yield CoachEvent("answer_text", text="Beide Verfügbarkeiten wurden gespeichert.")
            yield CoachEvent("completed")

    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: TwoAvailabilityAgent()
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Montags kann ich 45 Minuten und mittwochs 60 Minuten trainieren."},
    )

    assert response.status_code == 200
    with session_factory() as session:
        availability = {
            row.weekday: row.available_minutes
            for row in session.scalars(select(AthleteAvailability))
        }
        assert availability == {0: 45, 2: 60}
        assistant = session.scalar(
            select(CoachMessage).where(
                CoachMessage.conversation_id == conversation_id,
                CoachMessage.role == "assistant",
            )
        )
        assert assistant is not None
        assert len(assistant.artifacts_json) == 2


def test_conversation_reads_and_updates_planning_inputs_with_server_artifacts(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coaching_date = date(2026, 8, 29)

    class PlanningAgent:
        read_result: dict[str, object] | None = None

        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            assert (
                json.loads(
                    create_planning_goal(
                        runtime=runtime,
                        event_type="10k",
                        event_name="Stadtlauf",
                        target_date=date(2026, 10, 18),
                    )
                )["status"]
                == "updated"
            )
            assert (
                json.loads(
                    update_planning_profile(
                        runtime=runtime,
                        changes=PlanningProfileUpdateInput(
                            experience_level="intermediate",
                            preferred_long_run_weekday=6,
                        ),
                    )
                )["status"]
                == "updated"
            )
            assert (
                json.loads(
                    create_planning_anchor(
                        runtime=runtime,
                        kind="race",
                        distance_m=5000,
                        duration_s=1500,
                        achieved_on=date(2026, 8, 1),
                    )
                )["status"]
                == "updated"
            )
            self.read_result = json.loads(get_planning_inputs(runtime=runtime))
            yield CoachEvent("answer_text", text="Deine Planungsdaten wurden aktualisiert.")
            yield CoachEvent("completed")

    fake = PlanningAgent()
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: fake

    class CoachingDate(date):
        @classmethod
        def today(cls) -> date:
            return coaching_date

    monkeypatch.setattr(coach_route_module, "date", CoachingDate)
    conversation_id = _new_chat(client)
    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Setze mein Ziel, Profil und meine aktuelle 5-km-Leistung."},
    )

    assert response.status_code == 200
    completed_html = cast(str, _sse_payload(response.text, "answer.completed")["html"])
    assert "Ziel aktualisiert" in completed_html
    assert "Trainingsprofil aktualisiert" in completed_html
    assert "Leistungsanker aktualisiert" in completed_html
    assert "Stadtlauf" in completed_html
    assert "Sonntag" in completed_html
    assert "5,00 km" in completed_html
    assert fake.read_result is not None
    assert fake.read_result["as_of"] == coaching_date.isoformat()
    assert len(cast(list[object], fake.read_result["goals"])) == 1
    assert len(cast(list[object], fake.read_result["performance_anchors"])) == 1

    reloaded = client.get(f"/coach/{conversation_id}")
    assert reloaded.status_code == 200
    assert completed_html in reloaded.text


def test_planning_tool_schemas_expose_only_bounded_user_choices() -> None:
    tools = {tool.name: tool for tool in coach_tools(workout_proposals_enabled=False)}
    expected = {
        "get_planning_inputs": set(),
        "create_planning_goal": {"event_type", "event_name", "target_date"},
        "update_planning_goal": {"goal_id", "changes"},
        "deactivate_planning_goal": {"goal_id"},
        "update_planning_profile": {"changes"},
        "set_planning_availability": {
            "weekday",
            "available",
            "available_minutes",
        },
        "deactivate_planning_availability": {"weekday"},
        "create_planning_anchor": {
            "kind",
            "distance_m",
            "duration_s",
            "achieved_on",
            "reliable",
            "notes",
        },
        "update_planning_anchor": {"anchor_id", "changes"},
        "deactivate_planning_anchor": {"anchor_id"},
    }
    assert expected.keys() <= tools.keys()
    for name, properties in expected.items():
        schema_model: Any = tools[name].tool_call_schema
        schema = schema_model.model_json_schema()
        assert set(schema["properties"]) == properties
        serialized = json.dumps(schema)
        assert "user_id" not in serialized
        assert "conversation_id" not in serialized
        assert "assistant_message_id" not in serialized
        assert "accepted_cycles" not in serialized
        assert "idempotency" not in serialized
    goal_schema_model: Any = tools["create_planning_goal"].tool_call_schema
    anchor_schema_model: Any = tools["create_planning_anchor"].tool_call_schema
    assert goal_schema_model.model_json_schema()["properties"]["event_type"]["enum"] == [
        "general_fitness",
        "5k",
        "10k",
        "half_marathon",
        "marathon",
    ]
    assert anchor_schema_model.model_json_schema()["properties"]["kind"]["enum"] == [
        "race",
        "time_trial",
        "manual",
    ]


def test_referenced_goal_update_requires_exact_server_artifact_confirmation(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    original_date = date(2026, 10, 11)
    changed_date = date(2026, 10, 18)
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        goal = AthleteGoal(user_id=user.id, event_type="10k", target_date=original_date)
        session.add(goal)
        session.flush()
        cycle = TrainingCycle(
            user_id=user.id,
            goal_id=goal.id,
            event_type="10k",
            start_date=date(2026, 8, 17),
            target_date=original_date,
        )
        session.add(cycle)
        session.flush()
        revision = TrainingCycleRevision(
            cycle_id=cycle.id,
            owner_user_id=user.id,
            revision_number=1,
            event_type="10k",
            start_date=cycle.start_date,
            target_date=cycle.target_date,
            planner_version="test",
            knowledge_base_version="test",
            input_fingerprint="b" * 64,
            confidence="high",
            phase_plan_json=[],
            assumptions_json={},
            impact_json={},
            validation_report_json={"valid": True},
        )
        session.add(revision)
        session.flush()
        cycle.current_revision_id = revision.id
        cycle.accepted_revision_id = revision.id
        session.commit()
        goal_id = goal.id
        cycle_id = cycle.id
        revision_id = revision.id

    class ReferencedGoalAgent:
        runtime: CoachRuntimeContext | None = None

        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            self.runtime = runtime
            result = json.loads(
                coach_operations.update_planning_goal(
                    runtime,
                    goal_id,
                    GoalUpdateInput(target_date=changed_date),
                )
            )
            assert result == {
                "status": "confirmation_required",
                "artifact": {"type": "planning_input", "resource": "goal"},
            }
            assert (
                json.loads(
                    coach_operations.update_planning_goal(
                        runtime,
                        goal_id,
                        GoalUpdateInput(target_date=changed_date),
                    )
                )
                == result
            )
            yield CoachEvent("answer_text", text="Bitte bestätige die Zieländerung.")
            yield CoachEvent("completed")

    fake = ReferencedGoalAgent()
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: fake
    conversation_id = _new_chat(client)
    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Verschiebe mein 10-km-Ziel auf den 18. Oktober."},
    )

    assert response.status_code == 200
    completed_html = cast(str, _sse_payload(response.text, "answer.completed")["html"])
    assert "Bestätigung erforderlich" in completed_html
    assert "Zieldatum auf 2026-10-18 ändern" in completed_html
    assert "Dieses Ziel wird im angenommenen Trainingszyklus verwendet." in completed_html
    assert fake.runtime is not None and fake.runtime.assistant_message_id is not None
    endpoint = (
        f"/coach/{conversation_id}/messages/{fake.runtime.assistant_message_id}/"
        "planning-goal-confirmation"
    )
    assert f'action="{endpoint}"' in completed_html

    with session_factory() as session:
        goal = session.get(AthleteGoal, goal_id)
        assistant = session.get(CoachMessage, fake.runtime.assistant_message_id)
        assert goal is not None and goal.target_date == original_date
        assert assistant is not None
        artifact = assistant.artifacts_json[0]
        assert artifact["status"] == "confirmation_required"
        assert artifact["confirmation"] == {
            "goal_id": goal_id,
            "operation": "update",
            "accepted_cycles": [{"cycle_id": cycle_id, "accepted_revision_id": revision_id}],
        }

    csrf_token = client.headers.pop("X-CSRF-Token")
    try:
        blocked = client.post(endpoint, follow_redirects=False)
    finally:
        client.headers["X-CSRF-Token"] = csrf_token
    assert blocked.status_code == 403
    with session_factory() as session:
        goal = session.get(AthleteGoal, goal_id)
        assert goal is not None and goal.target_date == original_date

    confirmed = client.post(endpoint, follow_redirects=False)
    assert confirmed.status_code == 303
    assert confirmed.headers["location"] == f"/coach/{conversation_id}"
    with session_factory() as session:
        goal = session.get(AthleteGoal, goal_id)
        assistant = session.get(CoachMessage, fake.runtime.assistant_message_id)
        assert goal is not None and goal.target_date == changed_date
        assert assistant is not None
        result = assistant.artifacts_json[0].get("result")
        assert isinstance(result, dict)
        assert result["target_date"] == changed_date.isoformat()
        assert "confirmation" not in assistant.artifacts_json[0]

    reloaded = client.get(f"/coach/{conversation_id}")
    assert "Ziel aktualisiert" in reloaded.text
    assert "Bestätigung erforderlich" not in reloaded.text


def test_ambiguous_planning_dates_return_focused_questions_without_storage(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Ambiguous Planning")
        session.add(user)
        session.commit()
        user_id = user.id
    runtime = CoachRuntimeContext(user_id, date(2026, 8, 29), session_factory)

    goal_result = json.loads(
        create_planning_goal(runtime=runtime, event_type="10k", target_date=None)
    )
    anchor_result = json.loads(
        create_planning_anchor(
            runtime=runtime,
            kind="race",
            distance_m=5000,
            duration_s=1500,
            achieved_on=None,
        )
    )

    assert goal_result == {
        "status": "needs_clarification",
        "question": "Für welches Datum soll dieses Ziel gelten?",
    }
    assert anchor_result == {
        "status": "needs_clarification",
        "question": "An welchem Datum hast du diese Leistung erreicht?",
    }
    with session_factory() as session:
        assert session.scalar(select(AthleteGoal)) is None
        assert session.scalar(select(PerformanceAnchor)) is None


def test_planning_mutations_reject_cross_user_entity_ids(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        owner = session.scalar(select(User))
        assert owner is not None
        other = User(display_name="Other Planning Owner")
        session.add(other)
        session.flush()
        goal = AthleteGoal(
            user_id=other.id,
            event_type="10k",
            target_date=date(2026, 11, 1),
        )
        anchor = PerformanceAnchor(
            user_id=other.id,
            kind="race",
            distance_m=5000,
            duration_s=1500,
            achieved_on=date(2026, 8, 1),
        )
        session.add_all([goal, anchor])
        other_conversation = CoachConversation(user_id=other.id, title="Private Planung")
        session.add(other_conversation)
        session.flush()
        other_message = CoachMessage(
            conversation_id=other_conversation.id,
            role="assistant",
            status="completed",
            artifacts_json=[
                {
                    "type": "planning_input",
                    "resource": "goal",
                    "operation": "deactivate_planning_goal",
                    "status": "confirmation_required",
                    "request": {"goal_id": goal.id},
                    "confirmation": {},
                }
            ],
        )
        session.add(other_message)
        session.commit()
        goal_id = goal.id
        anchor_id = anchor.id
        other_conversation_id = other_conversation.id
        other_message_id = other_message.id

    class CrossUserAgent:
        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            goal_result = json.loads(
                coach_operations.update_planning_goal(
                    runtime,
                    goal_id,
                    GoalUpdateInput(event_name="Übernommen"),
                )
            )
            anchor_result = json.loads(
                coach_operations.update_planning_anchor(
                    runtime,
                    anchor_id,
                    PerformanceAnchorUpdateInput(notes="Übernommen"),
                )
            )
            assert goal_result["error"]["code"] == "planning.goal_not_found"
            assert anchor_result["error"]["code"] == "planning.anchor_not_found"
            yield CoachEvent("answer_text", text="Diese Planungsdaten wurden nicht gefunden.")
            yield CoachEvent("completed")

    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: CrossUserAgent()
    conversation_id = _new_chat(client)
    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Ändere fremde Planungsdaten."},
    )

    assert response.status_code == 200
    cross_user_confirmation = client.post(
        f"/coach/{other_conversation_id}/messages/{other_message_id}/planning-goal-confirmation",
        follow_redirects=False,
    )
    assert cross_user_confirmation.status_code == 404
    with session_factory() as session:
        goal = session.get(AthleteGoal, goal_id)
        anchor = session.get(PerformanceAnchor, anchor_id)
        assistant = session.scalar(
            select(CoachMessage).where(
                CoachMessage.conversation_id == conversation_id,
                CoachMessage.role == "assistant",
            )
        )
        assert goal is not None and goal.event_name is None
        assert anchor is not None and anchor.notes is None
        assert assistant is not None and assistant.artifacts_json == []


def test_conversational_planning_mutation_requires_csrf(client: TestClient) -> None:
    conversation_id = _new_chat(client)
    csrf_token = client.headers.pop("X-CSRF-Token")
    try:
        response = client.post(
            f"/coach/{conversation_id}/messages",
            data={"message": "Setze meine Verfügbarkeit."},
        )
    finally:
        client.headers["X-CSRF-Token"] = csrf_token

    assert response.status_code == 403


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
            result = json.loads(
                create_running_workout_proposal(
                    runtime=runtime,
                    suggested_for=runtime.as_of - timedelta(days=1),
                    available_minutes=45,
                )
            )
            assert result["status"] == "not_created"
            assert result["error"]["code"] == "proposal.date_in_past"
            yield CoachEvent(
                "answer_text",
                text="Das Datum liegt in der Vergangenheit. Welches zukünftige Datum meinst du?",
            )
            yield CoachEvent("completed")

    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: InvalidDateAgent()
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
        assistant = session.scalar(
            select(CoachMessage).where(
                CoachMessage.conversation_id == conversation_id,
                CoachMessage.role == "assistant",
            )
        )
        assert assistant is not None
        assert assistant.status == "completed"
        assert session.scalar(select(Workout)) is None


def test_proposal_tool_schema_exposes_no_runtime_or_workout_definition() -> None:
    proposal_tool = next(
        tool
        for tool in coach_tools(workout_proposals_enabled=True)
        if tool.name == "create_running_workout_proposal"
    )
    schema_model: Any = proposal_tool.tool_call_schema
    schema = schema_model.model_json_schema()
    assert set(schema["properties"]) == {
        "suggested_for",
        "available_minutes",
        "template_id",
    }
    serialized = json.dumps(schema)
    assert "user_id" not in serialized
    assert "assistant_run_id" not in serialized
    assert "idempotency" not in serialized
    assert "WorkoutDefinition" not in serialized
    assert schema["properties"]["template_id"]["enum"] == list(WORKOUT_FORMAT_IDS)

    revision_tool = next(
        tool
        for tool in coach_tools(workout_proposals_enabled=True)
        if tool.name == "revise_running_workout_proposal"
    )
    revision_schema_model: Any = revision_tool.tool_call_schema
    revision_schema = revision_schema_model.model_json_schema()
    assert set(revision_schema["properties"]) == {
        "workout_id",
        "revision_id",
        "suggested_for",
        "available_minutes",
        "edit_scope",
    }
    serialized_revision = json.dumps(revision_schema)
    assert "user_id" not in serialized_revision
    assert "idempotency" not in serialized_revision
    assert "lock_version" not in serialized_revision
    assert "content_hash" not in serialized_revision
    assert "WorkoutDefinition" not in serialized_revision


def test_conversation_revises_accepted_workout_without_replacing_it(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", True)
    coaching_date = date.today()
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        workout = RunningProposalService(session, user, as_of=coaching_date).create(
            RunningProposalRequest(
                template_id="easy_run",
                suggested_for=coaching_date + timedelta(days=1),
                available_minutes=40,
                idempotency_key="bc11-agent-create",
            )
        )
        revision = session.get(WorkoutRevision, workout.current_revision_id)
        assert revision is not None
        service = WorkoutService(session, user)
        context_fingerprint = service.acceptance_context(workout.id).fingerprint
        service.accept(
            workout.id,
            AcceptRevisionCommand(
                identity=RevisionIdentity(
                    revision_id=revision.id,
                    revision_number=revision.revision_number,
                    content_hash=revision.content_hash,
                    lock_version=workout.lock_version,
                ),
                context_fingerprint=context_fingerprint,
            ),
        )
        workout_id = workout.id
        accepted_revision_id = revision.id

    class RevisionAgent:
        runtime: CoachRuntimeContext | None = None
        revision_result: dict[str, object] | None = None

        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            self.runtime = runtime
            references = json.loads(get_revisable_running_workouts(runtime))
            assert references[0]["workout_id"] == workout_id
            assert references[0]["revision_id"] == accepted_revision_id
            unsupported = json.loads(
                revise_running_workout_proposal(
                    runtime,
                    workout_id=workout_id,
                    revision_id=accepted_revision_id,
                    edit_scope="unsupported",
                )
            )
            assert unsupported["status"] == "not_revised"
            assert unsupported["supported_alternative"]["command"] == (
                "create_running_workout_proposal"
            )
            revised = json.loads(
                revise_running_workout_proposal(
                    runtime,
                    workout_id=workout_id,
                    revision_id=accepted_revision_id,
                    suggested_for=coaching_date + timedelta(days=2),
                    available_minutes=30,
                )
            )
            self.revision_result = revised
            replay = json.loads(
                revise_running_workout_proposal(
                    runtime,
                    workout_id=workout_id,
                    revision_id=accepted_revision_id,
                    suggested_for=coaching_date + timedelta(days=2),
                    available_minutes=30,
                )
            )
            assert replay == revised
            if revised.get("status") == "revised":
                yield CoachEvent("artifact_available", artifact_type="workout")
            yield CoachEvent("answer_text", text="Die neue Revision ist bereit.")
            yield CoachEvent("completed")

    fake = RevisionAgent()
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: fake
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Verschiebe den Lauf auf den 22. August und kürze ihn auf 30 Minuten."},
    )

    assert response.status_code == 200
    assert fake.revision_result == {
        "status": "revised",
        "artifact": {"type": "workout_proposal"},
    }
    assert response.text.count("event: proposal.created") == 1
    assert fake.runtime is not None
    assistant_message_id = fake.runtime.assistant_message_id
    assert assistant_message_id is not None
    with session_factory() as session:
        workout = session.get(Workout, workout_id)
        assert workout is not None
        assert workout.accepted_revision_id == accepted_revision_id
        assert workout.current_revision_id != accepted_revision_id
        assert workout.materialized_revision_id == accepted_revision_id
        assert workout.source_assistant_message_id == assistant_message_id
        revisions = list(
            session.scalars(
                select(WorkoutRevision)
                .where(WorkoutRevision.workout_id == workout_id)
                .order_by(WorkoutRevision.revision_number)
            )
        )
        assert len(revisions) == 2
        artifact = workout_artifact_presentation(
            session,
            workout.user_id,
            conversation_id,
            assistant_message_id,
        )
        assert artifact is not None
        assert artifact.revision_id == revisions[1].id
        assert artifact.accepted_revision_id == accepted_revision_id
        assert artifact.lifecycle_actions[0].key == "accept"
        assert artifact.lifecycle_actions[0].label == "Angenommenes Workout ersetzen"
    detail = client.get(f"/workouts/{workout_id}")
    assert detail.status_code == 200
    assert "Angenommenes Workout ersetzen" in detail.text


def test_conversational_revision_rejects_foreign_workout_and_incomplete_runtime(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", True)
    as_of = date(2026, 8, 20)
    with session_factory() as session:
        user = User(display_name="Owner")
        other = User(display_name="Other")
        session.add_all([user, other])
        session.flush()
        workout = RunningProposalService(session, other, as_of=as_of).create(
            RunningProposalRequest(
                template_id="easy_run",
                suggested_for=date(2026, 8, 21),
                available_minutes=40,
                idempotency_key="bc11-foreign-create",
            )
        )
        revision_id = workout.current_revision_id
        assert revision_id is not None
        conversation = CoachConversation(user_id=user.id, title="BC11")
        session.add(conversation)
        session.flush()
        user_message = CoachMessage(
            conversation_id=conversation.id,
            role="user",
            content="Ändere das Workout.",
            status="completed",
        )
        assistant_message = CoachMessage(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            status="streaming",
        )
        session.add_all([user_message, assistant_message])
        session.commit()
        user_id = user.id
        workout_id = workout.id
        conversation_id = conversation.id
        user_message_id = user_message.id
        assistant_message_id = assistant_message.id

    incomplete = CoachRuntimeContext(
        user_id=user_id,
        as_of=as_of,
        session_factory=session_factory,
    )
    with pytest.raises(ValueError, match="runtime is incomplete"):
        revise_running_workout_proposal(
            incomplete,
            workout_id=workout_id,
            revision_id=revision_id,
            available_minutes=30,
        )

    runtime = CoachRuntimeContext(
        user_id=user_id,
        as_of=as_of,
        session_factory=session_factory,
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
    )
    assert json.loads(get_revisable_running_workouts(runtime)) == []
    foreign = json.loads(
        revise_running_workout_proposal(
            runtime,
            workout_id=workout_id,
            revision_id=revision_id,
            available_minutes=30,
        )
    )
    assert foreign["status"] == "not_revised"
    assert foreign["error"]["code"] == "workout.not_found"
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkoutRevision)
                .where(WorkoutRevision.workout_id == workout_id)
            )
            == 1
        )


def test_health_trend_tool_schema_uses_analytics_metric_choices() -> None:
    health_trends_tool = next(
        tool
        for tool in coach_tools(workout_proposals_enabled=False)
        if tool.name == "get_health_trends"
    )
    schema_model: Any = health_trends_tool.tool_call_schema
    schema = schema_model.model_json_schema()

    assert schema["properties"]["metrics"]["items"]["enum"] == list(HEALTH_METRICS)


def test_agent_registers_only_bounded_conversational_mutation_tools() -> None:
    read_only = {tool.name for tool in coach_tools(workout_proposals_enabled=False)}
    enabled = {tool.name for tool in coach_tools(workout_proposals_enabled=True)}
    assert {
        "create_planning_goal",
        "update_planning_goal",
        "deactivate_planning_goal",
        "update_planning_profile",
        "set_planning_availability",
        "deactivate_planning_availability",
        "create_planning_anchor",
        "update_planning_anchor",
        "deactivate_planning_anchor",
    } <= read_only
    assert enabled - read_only == {
        "create_running_workout_proposal",
        "get_revisable_running_workouts",
        "revise_running_workout_proposal",
    }
    assert (
        not {
            "accept_workout",
            "schedule_workout",
            "publish_workout",
            "push_workout",
        }
        & enabled
    )


def test_proposal_survives_provider_failure_after_commit(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", True)

    class FailingAfterProposalAgent:
        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            create_running_workout_proposal(
                runtime=runtime,
                suggested_for=date.today() + timedelta(days=1),
                available_minutes=35,
            )
            yield CoachEvent("artifact_available", artifact_type="workout")
            yield CoachEvent("failed")

    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        _running_history(session, user.id)
        session.commit()

    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: FailingAfterProposalAgent()
    conversation_id = _new_chat(client)
    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Plane einen Lauf."},
    )
    assert response.status_code == 200
    failed = _sse_payload(response.text, "error")
    assert 'data-message-state="failed"' in cast(str, failed["html"])

    with session_factory() as session:
        messages = list(
            session.scalars(
                select(CoachMessage)
                .where(CoachMessage.conversation_id == conversation_id)
                .order_by(CoachMessage.id)
            )
        )
        assert [(message.role, message.status) for message in messages] == [
            ("user", "completed"),
            ("assistant", "failed"),
        ]
        workout = session.scalar(
            select(Workout).where(Workout.originating_conversation_id == conversation_id)
        )
        assert workout is not None
        workout_id = workout.id
        assert len(list(session.scalars(select(Workout)))) == 1

    reloaded = client.get(f"/coach/{conversation_id}")
    assert reloaded.status_code == 200
    assert (
        "Diese Antwort konnte nicht abgeschlossen werden. Bitte versuche es erneut."
        in reloaded.text
    )
    assert "Unbestätigt" in reloaded.text
    assert f'href="/workouts/{workout_id}"' in reloaded.text
    assert cast(str, failed["html"]) in reloaded.text


def test_planning_artifact_survives_provider_failure_after_commit(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    class FailingAfterPlanningAgent:
        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            set_planning_availability(runtime, 5, True, 90)
            yield CoachEvent("artifact_available", artifact_type="planning_input")
            yield CoachEvent("failed")

    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: FailingAfterPlanningAgent()
    conversation_id = _new_chat(client)
    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Samstags kann ich 90 Minuten trainieren."},
    )

    assert response.status_code == 200
    failed_html = cast(str, _sse_payload(response.text, "error")["html"])
    assert "Verfügbarkeit aktualisiert" in failed_html
    assert "Samstag" in failed_html
    assert "90 Minuten" in failed_html
    with session_factory() as session:
        availability = session.scalar(
            select(AthleteAvailability).where(AthleteAvailability.weekday == 5)
        )
        assistant = session.scalar(select(CoachMessage).where(CoachMessage.role == "assistant"))
        assert availability is not None and availability.available_minutes == 90
        assert assistant is not None and assistant.status == "failed"
        assert len(assistant.artifacts_json) == 1

    reloaded = client.get(f"/coach/{conversation_id}")
    assert reloaded.status_code == 200
    assert failed_html in reloaded.text


def test_follow_up_includes_bounded_conversation_history(client: TestClient) -> None:
    fake = FakeCoachAgent()
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: fake
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
    app.dependency_overrides[get_coach_agent_factory] = lambda: None
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Wie erholt bin ich?"},
    )

    assert response.status_code == 503
    assert response.json()["detail"].startswith("Konfiguriere zuerst OpenRouter")


def test_provider_is_constructed_only_for_an_accepted_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "test-model")
    constructed_agents: list[FakeCoachAgent] = []

    def build_agent(**_: object) -> FakeCoachAgent:
        agent = FakeCoachAgent()
        constructed_agents.append(agent)
        return agent

    monkeypatch.setattr(coach_dependencies_module, "OpenRouterCoachProvider", build_agent)
    assert client.get("/coach").status_code == 200
    conversation_id = _new_chat(client)
    assert client.get(f"/coach/{conversation_id}").status_code == 200
    assert constructed_agents == []

    missing = client.post(
        "/coach/999/messages",
        data={"message": "Wie erholt bin ich?"},
    )
    assert missing.status_code == 404
    assert constructed_agents == []

    accepted = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Wie erholt bin ich?"},
    )
    assert accepted.status_code == 200
    assert len(constructed_agents) == 1


def test_provider_construction_failure_marks_claimed_answer_failed(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "test-model")

    def fail_construction(**_: object) -> None:
        raise RuntimeError("secret provider construction detail")

    monkeypatch.setattr(coach_provider_module, "ChatOpenRouter", fail_construction)
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Wie erholt bin ich?"},
    )
    assert response.status_code == 200
    assert "event: error" in response.text

    with session_factory() as session:
        messages = list(
            session.scalars(
                select(CoachMessage)
                .where(CoachMessage.conversation_id == conversation_id)
                .order_by(CoachMessage.id)
            )
        )
        assert [(message.role, message.status) for message in messages] == [
            ("user", "completed"),
            ("assistant", "failed"),
        ]


def test_completed_local_event_without_answer_marks_message_failed(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    class EmptyAnswerAgent:
        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages, runtime
            yield CoachEvent("completed")

    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: EmptyAnswerAgent()
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Wie erholt bin ich?"},
    )
    assert response.status_code == 200
    assert "event: error" in response.text

    with session_factory() as session:
        assistant = session.scalar(
            select(CoachMessage).where(
                CoachMessage.conversation_id == conversation_id,
                CoachMessage.role == "assistant",
            )
        )
        assert assistant is not None
        assert (assistant.status, assistant.content) == ("failed", "")


def test_working_presentation_failure_marks_claimed_answer_failed(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = coach_route_module._message_html
    attempts = 0

    def fail_first_render(*args: Any, **kwargs: Any) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("message presentation failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(coach_route_module, "_message_html", fail_first_render)
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: FakeCoachAgent()
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Wie erholt bin ich?"},
    )

    assert response.status_code == 200
    assert "event: error" in response.text
    with session_factory() as session:
        assistant = session.scalar(
            select(CoachMessage).where(
                CoachMessage.conversation_id == conversation_id,
                CoachMessage.role == "assistant",
            )
        )
        assert assistant is not None
        assert assistant.status == "failed"


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

    runtime = CoachRuntimeContext(first_id, day, session_factory)

    payload = json.loads(get_health_day(day=day, runtime=runtime))

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

    runtime = CoachRuntimeContext(first_id, day, session_factory)

    payload = json.loads(get_subjective_context(runtime=runtime))

    assert "daily_checkins" not in payload
    assert [item["name"] for item in payload["recent_activity_feedback"]] == ["Lauf"]
    assert payload["recent_activity_feedback"][0]["effort"] == 7
    assert payload["recent_activity_feedback"][0]["effort_source"] == "garmin"


@pytest.mark.parametrize(
    ("sync_status", "expected_status", "expected_workouts"),
    [
        (None, "not_synced", None),
        ("partial", "partial", 1),
        ("unsupported", "unsupported", None),
        ("empty", "empty", 0),
    ],
)
def test_adaptive_context_is_focus_bounded_and_preserves_coverage_meaning(
    session_factory: sessionmaker[Session],
    sync_status: str | None,
    expected_status: str,
    expected_workouts: int | None,
) -> None:
    day = date(2026, 8, 11)
    with session_factory() as session:
        user = User(display_name="Adaptive context")
        session.add(user)
        session.flush()
        if sync_status is not None:
            session.add(
                GarminSyncState(
                    user_id=user.id,
                    resource="activities",
                    status=sync_status,
                    backfill_complete=sync_status == "empty",
                    oldest_synced_date=day if sync_status == "empty" else None,
                    newest_synced_date=day,
                )
            )
        if sync_status == "partial":
            session.add(
                Activity(
                    user_id=user.id,
                    garmin_activity_id="adaptive-visible",
                    name="Sichtbarer Lauf",
                    activity_type="running",
                    started_at=datetime(2026, 8, 10, 8),
                    duration_s=1_800,
                )
            )
        session.commit()
        user_id = user.id

    runtime = CoachRuntimeContext(user_id, day, session_factory)
    recovery = json.loads(get_adaptive_context(runtime, focus="recovery", days=7))
    planning = json.loads(get_adaptive_context(runtime, focus="planning", days=7))
    next_session = json.loads(get_adaptive_context(runtime, focus="next_session", days=7))

    assert set(recovery) == {
        "as_of",
        "focus",
        "period_days",
        "recovery",
        "health_coverage",
        "training_load",
        "completed_work",
        "effective_feedback",
    }
    assert recovery["training_load"]["data_status"] == expected_status
    assert recovery["training_load"]["workouts"] == expected_workouts
    assert set(planning) == {
        "as_of",
        "focus",
        "period_days",
        "planning",
        "progress",
        "scheduled_work",
    }
    assert set(next_session) == set(recovery) | {
        "planning",
        "progress",
        "scheduled_work",
    }


def test_adaptive_context_prompt_requires_evidence_and_one_material_question() -> None:
    prompt = coach_provider_module.ADAPTIVE_CONTEXT_PROMPT.lower()

    assert "get_adaptive_context" in prompt
    assert "vor jeder materiellen empfehlung" in prompt
    assert "nicht-materiellen annahme" in prompt
    assert "stelle genau eine" in prompt
    assert "fokussierte frage" in prompt
    assert "rohkontext" in prompt
    assert "zweite modellprüfung" in prompt


def test_progress_tool_is_bounded_and_uses_runtime_authority(
    session_factory: sessionmaker[Session],
) -> None:
    day = date(2026, 8, 11)
    with session_factory() as session:
        first = User(display_name="Erste Person")
        second = User(display_name="Zweite Person")
        session.add_all((first, second))
        session.flush()
        first_goal = AthleteGoal(
            user_id=first.id,
            event_type="10k",
            target_date=date(2026, 10, 1),
        )
        hidden_goal = AthleteGoal(
            user_id=second.id,
            event_type="marathon",
            target_date=date(2026, 11, 1),
        )
        session.add_all((first_goal, hidden_goal))
        session.flush()
        session.add_all(
            [
                Activity(
                    user_id=first.id,
                    garmin_activity_id="visible-progress",
                    name="Sichtbar",
                    activity_type="running",
                    started_at=datetime(2026, 8, 10, 8),
                    duration_s=1_800,
                ),
                Activity(
                    user_id=second.id,
                    garmin_activity_id="hidden-progress",
                    name="Verborgen",
                    activity_type="running",
                    started_at=datetime(2026, 8, 10, 8),
                    duration_s=9_999,
                ),
                GarminSyncState(
                    user_id=first.id,
                    resource="activities",
                    status="ok",
                    backfill_complete=True,
                    oldest_synced_date=date(2026, 7, 1),
                    newest_synced_date=day,
                ),
            ]
        )
        session.commit()
        first_id = first.id
        first_goal_id = first_goal.id
        hidden_goal_id = hidden_goal.id

    runtime = CoachRuntimeContext(first_id, day, session_factory)
    payload = json.loads(get_progress(runtime=runtime, days=7, goal_id=first_goal_id))
    hidden = json.loads(get_progress(runtime=runtime, days=7, goal_id=hidden_goal_id))
    adaptive = json.loads(
        get_adaptive_context(runtime, focus="progress", days=7, goal_id=first_goal_id)
    )
    hidden_adaptive = json.loads(
        get_adaptive_context(runtime, focus="progress", days=7, goal_id=hidden_goal_id)
    )
    tool = {item.name: item for item in coach_tools(workout_proposals_enabled=False)}[
        "get_progress"
    ]
    schema_model: Any = tool.tool_call_schema
    schema = schema_model if isinstance(schema_model, dict) else schema_model.model_json_schema()

    assert payload["period"] == {"start": "2026-08-05", "end": "2026-08-11", "days": 7}
    assert payload["goal"]["id"] == first_goal_id
    assert adaptive["progress"]["goal"]["id"] == first_goal_id
    assert payload["comparison"]["observed_activity_sessions"] == 1
    assert "Verborgen" not in json.dumps(payload, ensure_ascii=False)
    assert hidden == {"status": "not_found"}
    assert hidden_adaptive == {"status": "not_found"}
    assert set(schema["properties"]) == {"days", "goal_id"}
    assert schema["properties"]["days"]["minimum"] == 7
    assert schema["properties"]["days"]["maximum"] == 84


def test_agent_stream_uses_trusted_runtime_context(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with session_factory() as session:
        user = User(display_name="Runtime Test")
        session.add(user)
        session.commit()
        user_id = user.id

    model = _ToolCallingFakeChatModel(
        tool_name="get_current_recovery_state",
        tool_args={},
        answer="Deine Erholungsdaten wurden geprüft.",
    )
    monkeypatch.setattr(coach_provider_module, "ChatOpenRouter", lambda **_: model)
    agent = OpenRouterCoachProvider(api_key="test-key", model_id="fake/model", timeout_seconds=5)

    async def collect() -> list[CoachEvent]:
        return [
            event
            async for event in agent.stream(
                [CoachHistoryMessage("user", "Wie ist meine Erholung?")],
                CoachRuntimeContext(user_id, date(2026, 8, 11), session_factory),
            )
        ]

    events = asyncio.run(collect())

    assert model.observed_tool_result is not None
    assert '"as_of":"2026-08-11"' in model.observed_tool_result
    assert "".join(event.text or "" for event in events if event.type == "answer_text") == (
        "Deine Erholungsdaten wurden geprüft."
    )
    assert events[-1] == CoachEvent("completed")


@pytest.mark.asyncio
async def test_langchain_backend_continues_after_read_only_json_list_tool_result(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @tool("get_recent_activities")
    def fake_recent_activities_tool() -> str:
        """Return a deterministic list of recent activities for adapter testing."""
        return '[{"id":42,"name":"Krafttraining"}]'

    model = _ToolCallingFakeChatModel(
        tool_name="get_recent_activities",
        tool_args={},
        answer="Die Aktivität wurde gefunden.",
    )
    monkeypatch.setattr(coach_provider_module, "ChatOpenRouter", lambda **_: model)
    monkeypatch.setattr(
        coach_provider_module, "coach_tools", lambda **_: [fake_recent_activities_tool]
    )
    agent = OpenRouterCoachProvider(api_key="test-key", model_id="fake/model", timeout_seconds=5)

    events = [
        event
        async for event in agent.stream(
            [CoachHistoryMessage("user", "Zeige meine letzte Aktivität.")],
            CoachRuntimeContext(1, date(2026, 8, 24), session_factory),
        )
    ]

    assert all(event.type != "artifact_available" for event in events)
    assert events[-2:] == [
        CoachEvent("answer_text", text="Die Aktivität wurde gefunden."),
        CoachEvent("completed"),
    ]


@pytest.mark.asyncio
async def test_langchain_backend_maps_only_valid_proposal_artifact(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @tool("create_running_workout_proposal")
    def fake_proposal_tool() -> str:
        """Create a deterministic workout proposal for adapter testing."""
        return '{"status":"created","artifact":{"type":"workout_proposal"}}'

    model = _ToolCallingFakeChatModel(
        tool_name="create_running_workout_proposal",
        tool_args={},
        answer="Der Vorschlag ist bereit.",
    )
    monkeypatch.setattr(coach_provider_module, "ChatOpenRouter", lambda **_: model)
    monkeypatch.setattr(coach_provider_module, "coach_tools", lambda **_: [fake_proposal_tool])
    agent = OpenRouterCoachProvider(
        api_key="test-key",
        model_id="fake/model",
        timeout_seconds=5,
        workout_proposals_enabled=True,
    )
    events = [
        event
        async for event in agent.stream(
            [CoachHistoryMessage("user", "Plane einen Easy Run.")],
            CoachRuntimeContext(1, date(2026, 8, 24), session_factory),
        )
    ]

    assert [event.type for event in events].count("artifact_available") == 1
    assert events[-2:] == [
        CoachEvent("answer_text", text="Der Vorschlag ist bereit."),
        CoachEvent("completed"),
    ]


@pytest.mark.asyncio
async def test_langchain_backend_maps_revised_workout_artifact(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @tool("revise_running_workout_proposal")
    def fake_revision_tool() -> str:
        """Create a deterministic workout revision for adapter testing."""
        return '{"status":"revised","artifact":{"type":"workout_proposal"}}'

    model = _ToolCallingFakeChatModel(
        tool_name="revise_running_workout_proposal",
        tool_args={},
        answer="Die neue Revision ist bereit.",
    )
    monkeypatch.setattr(coach_provider_module, "ChatOpenRouter", lambda **_: model)
    monkeypatch.setattr(coach_provider_module, "coach_tools", lambda **_: [fake_revision_tool])
    agent = OpenRouterCoachProvider(
        api_key="test-key",
        model_id="fake/model",
        timeout_seconds=5,
        workout_proposals_enabled=True,
    )

    events = [
        event
        async for event in agent.stream(
            [CoachHistoryMessage("user", "Kürze den Entwurf.")],
            CoachRuntimeContext(1, date(2026, 8, 24), session_factory),
        )
    ]

    assert CoachEvent("artifact_available", artifact_type="workout") in events
    assert events[-2:] == [
        CoachEvent("answer_text", text="Die neue Revision ist bereit."),
        CoachEvent("completed"),
    ]


@pytest.mark.asyncio
async def test_langchain_backend_maps_valid_planning_artifact(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @tool("set_planning_availability")
    def fake_planning_tool() -> str:
        """Store one deterministic planning input for adapter testing."""
        return '{"status":"updated","artifact":{"type":"planning_input","resource":"availability"}}'

    model = _ToolCallingFakeChatModel(
        tool_name="set_planning_availability",
        tool_args={},
        answer="Die Verfügbarkeit ist gespeichert.",
    )
    monkeypatch.setattr(coach_provider_module, "ChatOpenRouter", lambda **_: model)
    monkeypatch.setattr(coach_provider_module, "coach_tools", lambda **_: [fake_planning_tool])
    agent = OpenRouterCoachProvider(
        api_key="test-key",
        model_id="fake/model",
        timeout_seconds=5,
    )
    events = [
        event
        async for event in agent.stream(
            [CoachHistoryMessage("user", "Speichere meine Verfügbarkeit.")],
            CoachRuntimeContext(1, date(2026, 8, 29), session_factory),
        )
    ]

    assert CoachEvent("artifact_available", artifact_type="planning_input") in events
    assert events[-1] == CoachEvent("completed")


@pytest.mark.asyncio
async def test_langchain_backend_maps_valid_feedback_artifact(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @tool("record_post_session_feedback")
    def fake_feedback_tool() -> str:
        """Store deterministic post-session feedback for adapter testing."""
        return '{"status":"recorded","artifact":{"type":"feedback","resource":"post_session"}}'

    model = _ToolCallingFakeChatModel(
        tool_name="record_post_session_feedback",
        tool_args={},
        answer="Das Feedback ist gespeichert.",
    )
    monkeypatch.setattr(coach_provider_module, "ChatOpenRouter", lambda **_: model)
    monkeypatch.setattr(coach_provider_module, "coach_tools", lambda **_: [fake_feedback_tool])
    agent = OpenRouterCoachProvider(
        api_key="test-key",
        model_id="fake/model",
        timeout_seconds=5,
    )
    events = [
        event
        async for event in agent.stream(
            [CoachHistoryMessage("user", "Speichere mein Feedback.")],
            CoachRuntimeContext(1, date(2026, 8, 29), session_factory),
        )
    ]

    assert CoachEvent("artifact_available", artifact_type="feedback") in events
    assert events[-1] == CoachEvent("completed")


@pytest.mark.parametrize(
    "first_answer",
    [
        "Ich schaue kurz deine letzte Aktivität raus.",
        "Das Feedback ist gespeichert und fließt jetzt in deine Verlaufsbetrachtung ein.",
    ],
)
@pytest.mark.asyncio
async def test_langchain_backend_completes_promised_tool_action_in_same_turn(
    first_answer: str,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @tool("record_post_session_feedback")
    def fake_feedback_tool() -> str:
        """Store deterministic post-session feedback for adapter testing."""
        return '{"status":"recorded","artifact":{"type":"feedback","resource":"post_session"}}'

    model = _DeferredThenToolCallingFakeChatModel(first_answer=first_answer)
    monkeypatch.setattr(coach_provider_module, "ChatOpenRouter", lambda **_: model)
    monkeypatch.setattr(coach_provider_module, "coach_tools", lambda **_: [fake_feedback_tool])
    agent = OpenRouterCoachProvider(
        api_key="test-key",
        model_id="fake/model",
        timeout_seconds=5,
    )
    events = [
        event
        async for event in agent.stream(
            [CoachHistoryMessage("user", "Ja, vollständig; es war etwas schwer.")],
            CoachRuntimeContext(1, date(2026, 8, 31), session_factory),
        )
    ]

    assert model.invocation_count == 3
    assert CoachEvent("artifact_available", artifact_type="feedback") in events
    assert events[-2:] == [
        CoachEvent("answer_text", text="Dein Feedback wurde gespeichert."),
        CoachEvent("completed"),
    ]


@pytest.mark.asyncio
async def test_provider_adapter_logs_and_maps_provider_errors(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coach_provider_module,
        "ChatOpenRouter",
        lambda **_: _FailingFakeChatModel(),
    )
    agent = OpenRouterCoachProvider(api_key="test-key", model_id="fake/model", timeout_seconds=5)
    runtime = CoachRuntimeContext(1, date(2026, 8, 11), session_factory)
    logger = Mock()
    monkeypatch.setattr(coach_provider_module, "logger", logger)

    private_question = "Mein privater Gesundheitswert ist 123."
    events = [
        event
        async for event in agent.stream([CoachHistoryMessage("user", private_question)], runtime)
    ]

    assert events == [CoachEvent("failed", failure_category="provider_error")]
    logger.warning.assert_called_once()
    log_call = logger.warning.call_args
    assert "AI coach agent failed" in log_call.args[0]
    assert "failure_category=provider_error" in log_call.args[0]
    assert "error_type=%s error_source=%s" in log_call.args[0]
    assert "RuntimeError" in log_call.args
    assert private_question not in repr(log_call)


@pytest.mark.asyncio
async def test_provider_adapter_maps_missing_answer_to_failed(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coach_provider_module,
        "ChatOpenRouter",
        lambda **_: _ReasoningFakeChatModel(answer=""),
    )
    provider = OpenRouterCoachProvider(api_key="test-key", model_id="fake/model", timeout_seconds=5)

    events = [
        event
        async for event in provider.stream(
            [CoachHistoryMessage("user", "Wie ist meine Erholung?")],
            CoachRuntimeContext(1, date(2026, 8, 11), session_factory),
        )
    ]

    assert events == [CoachEvent("failed", failure_category="missing_final_answer")]
