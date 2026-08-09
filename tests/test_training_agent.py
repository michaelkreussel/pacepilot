import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.main import app
from app.models import Activity, DailyHealth, User, Workout
from app.routes import coach as coach_route
from app.services.analytics.coach_data import CoachDataService
from app.services.training_agent import agno_backend as agno_module
from app.services.training_agent.agno_backend import AgnoTrainingAgent, _plain_text
from app.services.training_agent.backend import (
    CoachCapabilities,
    CoachEvent,
    ConversationTurn,
    TrainingAgentError,
)
from app.services.training_agent.dependencies import get_training_agent
from app.services.training_agent.log_config import ensure_coach_file_logging
from app.services.training_agent.orchestrator import CoachOrchestrator
from app.services.training_agent.tools import CoachTools


class FakeTrainingAgent:
    def __init__(self) -> None:
        self.message: str | None = None
        self.training_result: dict[str, object] | None = None
        self.history: tuple[ConversationTurn, ...] = ()

    async def stream(
        self,
        message: str,
        capabilities: CoachCapabilities,
        history: tuple[ConversationTurn, ...] = (),
    ) -> AsyncIterator[CoachEvent]:
        self.message = message
        self.history = history
        tools = {getattr(tool, "__name__", ""): tool for tool in capabilities.agent_tools()}
        self.training_result = json.loads(tools["get_training_history"](days=28))
        yield CoachEvent("tool_started", "Ich prüfe Training.", tool="get_training_history")
        yield CoachEvent("final_response", "Die letzten Einheiten liegen jetzt vor.")
        yield CoachEvent("final_response", done=True)


def test_coach_uses_dynamic_read_only_capabilities(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    client.get("/")
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        session.add(
            Activity(
                user_id=user.id,
                garmin_activity_id="agent-context-1",
                name="Lockerer Morgenlauf",
                activity_type="running",
                started_at=datetime.now(UTC).replace(tzinfo=None),
                duration_s=2400,
                distance_m=6500,
            )
        )
        session.commit()

    fake = FakeTrainingAgent()
    app.dependency_overrides[get_training_agent] = lambda: fake

    response = client.post("/coach", data={"message": "Ordne meine letzten Einheiten ein."})

    assert response.status_code == 200
    assert "Die letzten Einheiten liegen jetzt vor" in response.text
    assert fake.message == "Ordne meine letzten Einheiten ein."
    assert fake.training_result is not None
    source_facts = fake.training_result["source_facts"]
    assert isinstance(source_facts, dict)
    recent = source_facts["recent_workouts"]
    assert isinstance(recent, list)
    assert recent[0]["name"] == "Lockerer Morgenlauf"
    with session_factory() as session:
        assert session.scalar(select(Workout)) is None


def test_coach_page_is_a_streaming_chat_window(client: TestClient) -> None:
    app.dependency_overrides[get_training_agent] = FakeTrainingAgent

    response = client.get("/coach")

    assert response.status_code == 200
    assert 'id="coach-chat"' in response.text
    assert "Live-Aktivität" in response.text
    assert "coach-current-step" in response.text
    assert "Bisher erledigt" in response.text
    assert "bg-info-subtle" not in response.text
    assert "Verlauf nur in diesem Browser-Tab" in response.text
    assert "/js/coach.js" in response.text


def test_coach_streams_structured_progress_and_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTrainingAgent()
    log_info = Mock()
    app.dependency_overrides[get_training_agent] = lambda: fake
    monkeypatch.setattr(coach_route.logger, "info", log_info)

    response = client.post(
        "/coach/stream",
        json={
            "message": "Und wie passt das zu heute?",
            "history": [
                {"role": "user", "content": "War meine Belastung ungewöhnlich hoch?"},
                {"role": "assistant", "content": "Sie lag über deinem letzten Monat."},
            ],
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.headers["cache-control"] == "no-store"
    run_id = response.headers["x-coach-run-id"]
    assert len(run_id) == 12
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["type"] for event in events] == [
        "tool_started",
        "final_response",
        "final_response",
    ]
    assert events[0]["tool"] == "get_training_history"
    assert all(event["run_id"] == run_id for event in events)
    assert events[-1]["done"] is True
    assert fake.history == (
        ConversationTurn("user", "War meine Belastung ungewöhnlich hoch?"),
        ConversationTurn("assistant", "Sie lag über deinem letzten Monat."),
    )
    log_text = "\n".join(call.args[0] % tuple(call.args[1:]) for call in log_info.call_args_list)
    assert f"Coach run started run_id={run_id}" in log_text
    assert f"Coach tool started run_id={run_id} tool=get_training_history" in log_text
    assert f"Coach run completed run_id={run_id}" in log_text
    assert "Und wie passt das zu heute?" not in log_text


def test_coach_requires_configured_backend(client: TestClient) -> None:
    app.dependency_overrides[get_training_agent] = lambda: None

    response = client.post("/coach", data={"message": "Analysiere meine Woche."})
    stream_response = client.post("/coach/stream", json={"message": "Analysiere meine Woche."})

    assert response.status_code == 503
    assert "Konfiguriere zuerst OpenRouter" in response.text
    assert stream_response.status_code == 503
    assert "Konfiguriere zuerst OpenRouter" in stream_response.json()["detail"]


def test_health_tool_separates_source_facts_and_calculations(
    session_factory: sessionmaker[Session],
) -> None:
    as_of = date(2026, 8, 8)
    with session_factory() as session:
        user = User(display_name="Mara")
        session.add(user)
        session.flush()
        session.add_all(
            [
                DailyHealth(user_id=user.id, day=date(2026, 7, 1), resting_hr=50),
                DailyHealth(user_id=user.id, day=as_of, resting_hr=60),
            ]
        )
        session.commit()

        tools = CoachTools(CoachDataService(session, user.id, user.display_name, as_of=as_of))
        result = json.loads(tools.get_health_and_recovery(metrics=["resting_hr"], days=28))

    facts = result["source_facts"]["metrics"]["resting_hr"]
    calculated = result["pacepilot_calculations"]["resting_hr"]
    assert facts["current"] == 60.0
    assert facts["current_day"] == "2026-08-08"
    assert calculated["personal_baseline"] == 50.0
    assert calculated["difference_from_baseline"] == 10.0
    assert result["data_quality"]["coverage"][0]["status"] == "not_synced"


class EmptyCapabilities:
    def agent_tools(self) -> tuple[Callable[..., str], ...]:
        return ()


def test_agno_backend_reserves_budget_for_visible_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_openrouter(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(agno_module, "OpenRouter", fake_openrouter)

    AgnoTrainingAgent(api_key="key", model_id="model", base_url="https://example.test")

    assert captured["max_tokens"] == 8192
    assert captured["reasoning_effort"] == "medium"


def test_agent_markdown_is_normalized_to_plain_text() -> None:
    assert _plain_text("## **Fakten**\n- `Wert`\n[Details](https://example.test)") == (
        "Fakten\n- Wert\nDetails"
    )


@pytest.mark.asyncio
async def test_agno_backend_maps_tool_and_content_events() -> None:
    async def events() -> AsyncIterator[SimpleNamespace]:
        tool = SimpleNamespace(
            tool_name="get_health_and_recovery",
            tool_args={"days": 28},
            result=json.dumps({"progress_summary": "Ruhepuls und HRV wurden geprüft."}),
        )
        yield SimpleNamespace(event="ToolCallStarted", tool=tool)
        yield SimpleNamespace(event="ToolCallCompleted", tool=tool)
        yield SimpleNamespace(event="RunContent", content="Vorsichtige ")
        yield SimpleNamespace(event="RunContent", content="Einordnung.")
        yield SimpleNamespace(event="RunCompleted", content="Vorsichtige Einordnung.")

    class FakeAgnoAgent:
        prompt: str | None = None
        kwargs: dict[str, object] | None = None

        async def arun(self, prompt: str, **kwargs: object) -> AsyncIterator[SimpleNamespace]:
            self.prompt = prompt
            self.kwargs = kwargs
            return events()

    backend = AgnoTrainingAgent.__new__(AgnoTrainingAgent)
    agno_agent = FakeAgnoAgent()
    backend._create_agent = lambda tools: agno_agent

    output = [
        event
        async for event in backend.stream(
            "Warum müde?",
            EmptyCapabilities(),
            (ConversationTurn("user", "Wie war gestern?"),),
        )
    ]

    assert [event.type for event in output] == [
        "status",
        "tool_started",
        "tool_result_summary",
        "analysis_update",
        "final_response",
        "final_response",
        "final_response",
    ]
    assert output[1].label == "Gesundheit & Erholung"
    assert output[2].content == "Ruhepuls und HRV wurden geprüft."
    final_events = [event for event in output if event.type == "final_response"]
    assert final_events[-2].content == "Vorsichtige Einordnung."
    assert final_events[-2].replace is True
    assert output[-1].done is True
    assert agno_agent.prompt is not None
    assert "Athlet: Wie war gestern?" in agno_agent.prompt
    assert "Aktuelle Frage:\nWarum müde?" in agno_agent.prompt
    assert agno_agent.kwargs == {"stream": True, "stream_events": True}


@pytest.mark.asyncio
async def test_agno_backend_accepts_agno_direct_async_stream() -> None:
    async def events() -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(event="RunContent", content="Direkter Stream.")
        yield SimpleNamespace(event="RunCompleted", content="Direkter Stream.")

    class DirectStreamingAgnoAgent:
        def arun(self, prompt: str, **kwargs: object) -> AsyncIterator[SimpleNamespace]:
            del prompt, kwargs
            return events()

    backend = AgnoTrainingAgent.__new__(AgnoTrainingAgent)
    backend._create_agent = lambda tools: DirectStreamingAgnoAgent()

    output = [event async for event in backend.stream("Test", EmptyCapabilities())]

    final_events = [event for event in output if event.type == "final_response"]
    assert final_events[-2].content == "Direkter Stream."
    assert final_events[-2].replace is True
    assert output[-1].done is True


@pytest.mark.asyncio
async def test_agno_backend_hides_provider_errors() -> None:
    class FailingAgnoAgent:
        async def arun(self, prompt: str, **kwargs: object) -> None:
            del prompt, kwargs
            raise RuntimeError("secret provider detail")

    backend = AgnoTrainingAgent.__new__(AgnoTrainingAgent)
    backend._create_agent = lambda tools: FailingAgnoAgent()

    with pytest.raises(TrainingAgentError, match="OpenRouter konnte"):
        _ = [event async for event in backend.stream("Was steht an?", EmptyCapabilities())]


@pytest.mark.asyncio
async def test_orchestrator_emits_waiting_heartbeats() -> None:
    class SlowAgent:
        async def stream(
            self,
            message: str,
            capabilities: CoachCapabilities,
            history: tuple[ConversationTurn, ...] = (),
        ) -> AsyncIterator[CoachEvent]:
            del message, capabilities, history
            yield CoachEvent(
                "tool_started",
                "Gesundheitsdaten werden geladen.",
                label="Gesundheit & Erholung",
            )
            await asyncio.sleep(0.03)
            yield CoachEvent("final_response", "Antwort", done=True)

    events = [
        event
        async for event in CoachOrchestrator(SlowAgent(), heartbeat_seconds=0.005).stream(
            "Warum?", EmptyCapabilities()
        )
    ]

    waiting = [event for event in events if event.type == "waiting"]
    assert waiting
    assert waiting[0].phase == "retrieving_data"
    assert waiting[0].label == "Gesundheit & Erholung"
    assert "läuft noch" in waiting[0].content


def test_coach_file_logging_is_rotating_and_searchable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    test_logger = logging.getLogger("pacepilot.tests.coach-file")
    test_logger.setLevel(logging.DEBUG)
    test_logger.propagate = False
    for handler in test_logger.handlers[:]:
        handler.close()
        test_logger.removeHandler(handler)

    path = ensure_coach_file_logging(test_logger)
    test_logger.error("Coach run failed run_id=d33cd96310c2")
    for handler in test_logger.handlers:
        handler.flush()

    assert path == tmp_path / "logs" / "coach.log"
    assert "run_id=d33cd96310c2" in path.read_text(encoding="utf-8")
    handler = test_logger.handlers[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == 3
    handler.close()
    test_logger.removeHandler(handler)
