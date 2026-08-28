import asyncio
from collections.abc import AsyncIterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import date, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.main import app
from app.models import CoachAssistantRun, CoachConversation, CoachMessage, User
from app.models.user import utcnow
from app.repositories.coach import conversation_messages, find_conversation
from app.services.coach.agent import CoachEvent
from app.services.coach.conversation import (
    ActiveResponseConflictError,
    CoachHistoryMessage,
    CoachRuntimeContext,
    prepare_execution,
)
from app.services.coach.dependencies import get_coach_agent_factory
from app.services.coach.provider import (
    COACH_PROMPT_TEMPLATE_VERSION,
    COACH_TOOL_CONTRACT_VERSION,
)


class RecordingCoachAgent:
    def __init__(self) -> None:
        self.calls: list[list[CoachHistoryMessage]] = []

    async def stream(
        self,
        messages: Sequence[CoachHistoryMessage],
        runtime: CoachRuntimeContext,
    ) -> AsyncIterator[CoachEvent]:
        del runtime
        self.calls.append(list(messages))
        yield CoachEvent("answer_text", text="Recorded answer")
        yield CoachEvent("completed")


def _new_chat(client: TestClient) -> int:
    response = client.post("/coach/conversations", follow_redirects=False)
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[1])


def test_follow_up_uses_only_the_latest_twenty_completed_messages(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    agent = RecordingCoachAgent()
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: agent
    conversation_id = _new_chat(client)
    with session_factory() as session:
        conversation = session.get(CoachConversation, conversation_id)
        assert conversation is not None
        for index in range(24):
            session.add(
                CoachMessage(
                    conversation=conversation,
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"history-{index:02d}",
                    status="completed",
                    completed_at=utcnow(),
                )
            )
        session.commit()

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "current-question"},
    )

    assert response.status_code == 200
    assert [(item.role, item.content) for item in agent.calls[0]] == [
        (
            "user" if index % 2 == 0 else "assistant",
            f"history-{index:02d}",
        )
        for index in range(4, 24)
    ] + [("user", "current-question")]


@pytest.mark.parametrize("status", ("failed", "interrupted"))
def test_incomplete_assistant_answer_is_excluded_from_follow_up_history(
    client: TestClient,
    session_factory: sessionmaker[Session],
    status: str,
) -> None:
    agent = RecordingCoachAgent()
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: agent
    conversation_id = _new_chat(client)
    with session_factory() as session:
        conversation = session.get(CoachConversation, conversation_id)
        assert conversation is not None
        session.add_all(
            (
                CoachMessage(
                    conversation=conversation,
                    role="user",
                    content="completed question",
                    status="completed",
                    completed_at=utcnow(),
                ),
                CoachMessage(
                    conversation=conversation,
                    role="assistant",
                    content="private partial answer",
                    status=status,
                    completed_at=utcnow(),
                ),
            )
        )
        session.commit()

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "follow-up question"},
    )

    assert response.status_code == 200
    assert [(item.role, item.content) for item in agent.calls[0]] == [
        ("user", "completed question"),
        ("user", "follow-up question"),
    ]


def test_follow_up_limits_prior_history_to_twelve_thousand_characters(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    agent = RecordingCoachAgent()
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: agent
    conversation_id = _new_chat(client)
    oldest = "A" * 5000
    middle = "B" * 5000
    newest = "C" * 5000
    with session_factory() as session:
        conversation = session.get(CoachConversation, conversation_id)
        assert conversation is not None
        session.add_all(
            (
                CoachMessage(
                    conversation=conversation,
                    role="user",
                    content=oldest,
                    status="completed",
                    completed_at=utcnow(),
                ),
                CoachMessage(
                    conversation=conversation,
                    role="assistant",
                    content=middle,
                    status="completed",
                    completed_at=utcnow(),
                ),
                CoachMessage(
                    conversation=conversation,
                    role="user",
                    content=newest,
                    status="completed",
                    completed_at=utcnow(),
                ),
            )
        )
        session.commit()

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "current-question"},
    )

    assert response.status_code == 200
    history = agent.calls[0]
    assert sum(len(item.content) for item in history[:-1]) == 12_000
    assert [(item.role, item.content) for item in history] == [
        ("user", oldest[-2000:]),
        ("assistant", middle),
        ("user", newest),
        ("user", "current-question"),
    ]


def test_active_response_rejects_another_message(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    agent = RecordingCoachAgent()
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: agent
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

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Start another answer"},
    )

    assert response.status_code == 409
    assert "läuft bereits eine Antwort" in response.json()["detail"]
    assert agent.calls == []
    with session_factory() as session:
        messages = list(
            session.scalars(
                select(CoachMessage).where(CoachMessage.conversation_id == conversation_id)
            )
        )
        assert len(messages) == 1
        assert messages[0].status == "streaming"


def test_simultaneous_submissions_start_one_response_without_an_orphan_user_message(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "coach-concurrency.db"
    engine = create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        user = User(display_name="Concurrent athlete")
        conversation = CoachConversation(user=user, title="Neuer Chat")
        session.add_all((user, conversation))
        session.commit()
        user_id = user.id
        conversation_id = conversation.id

    agent = RecordingCoachAgent()
    both_observed_no_active_response = Barrier(2)

    async def execute_provider(execution) -> None:
        async for _event in agent.stream(execution.history, execution.runtime):
            pass

    def submit(question: str) -> str:
        with factory() as session:
            conversation = find_conversation(session, user_id, conversation_id)
            assert conversation is not None
            prior_messages = conversation_messages(session, user_id, conversation_id) or []
            assert prior_messages == []
            both_observed_no_active_response.wait()
            try:
                execution = prepare_execution(
                    session,
                    conversation,
                    prior_messages,
                    user_id=user_id,
                    question=question,
                    model_id="test/model",
                    request_id=question,
                    prompt_template_version=COACH_PROMPT_TEMPLATE_VERSION,
                    operation_contract_version=COACH_TOOL_CONTRACT_VERSION,
                    as_of=date(2026, 8, 28),
                )
                session.commit()
            except ActiveResponseConflictError:
                session.rollback()
                return "conflict"
        asyncio.run(execute_provider(execution))
        return "started"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ("First question", "Second question")))

    assert sorted(results) == ["conflict", "started"]
    assert len(agent.calls) == 1
    with factory() as session:
        messages = list(
            session.scalars(
                select(CoachMessage)
                .where(CoachMessage.conversation_id == conversation_id)
                .order_by(CoachMessage.id)
            )
        )
        assert [(message.role, message.status) for message in messages] == [
            ("user", "completed"),
            ("assistant", "streaming"),
        ]
        assert messages[0].content in {"First question", "Second question"}
    engine.dispose()


def test_loading_conversation_repairs_and_displays_a_stale_response(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    conversation_id = _new_chat(client)
    with session_factory() as session:
        conversation = session.get(CoachConversation, conversation_id)
        assert conversation is not None
        stale = CoachMessage(
            conversation=conversation,
            role="assistant",
            content="partial answer",
            status="streaming",
            created_at=utcnow() - timedelta(minutes=11),
        )
        session.add(stale)
        session.commit()
        stale_id = stale.id

    response = client.get(f"/coach/{conversation_id}")

    assert response.status_code == 200
    assert (
        "Diese Antwort konnte nicht abgeschlossen werden. Bitte versuche es erneut."
        in response.text
    )
    with session_factory() as session:
        stale = session.get(CoachMessage, stale_id)
        assert stale is not None
        assert stale.status == "interrupted"
        assert stale.failure_category == "interrupted"
        assert stale.content == ""
        assert stale.completed_at is not None


def test_stale_response_is_interrupted_before_the_next_message(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    agent = RecordingCoachAgent()
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: agent
    conversation_id = _new_chat(client)
    with session_factory() as session:
        conversation = session.get(CoachConversation, conversation_id)
        assert conversation is not None
        stale = CoachMessage(
            conversation=conversation,
            role="assistant",
            content="partial answer",
            status="streaming",
            created_at=utcnow() - timedelta(minutes=11),
        )
        session.add(stale)
        session.commit()
        stale_id = stale.id

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Try again"},
    )

    assert response.status_code == 200
    assert [(item.role, item.content) for item in agent.calls[0]] == [("user", "Try again")]
    with session_factory() as session:
        stale = session.get(CoachMessage, stale_id)
        assert stale is not None
        assert stale.status == "interrupted"
        assert stale.completed_at is not None
    reloaded = client.get(f"/coach/{conversation_id}")
    assert (
        "Diese Antwort konnte nicht abgeschlossen werden. Bitte versuche es erneut."
        in reloaded.text
    )


def test_disconnect_marks_the_started_answer_interrupted(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PartialAnswerAgent:
        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages, runtime
            yield CoachEvent("answer_text", text="Private partial answer")
            yield CoachEvent("completed")

    async def disconnected(_request: Request) -> bool:
        return True

    monkeypatch.setattr(Request, "is_disconnected", disconnected)
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: PartialAnswerAgent()
    conversation_id = _new_chat(client)

    with suppress(asyncio.CancelledError):
        client.post(
            f"/coach/{conversation_id}/messages",
            data={"message": "Disconnect this answer"},
        )

    with session_factory() as session:
        assistant = session.scalar(
            select(CoachMessage).where(
                CoachMessage.conversation_id == conversation_id,
                CoachMessage.role == "assistant",
            )
        )
        assert assistant is not None
        assert assistant.status == "interrupted"
        assert assistant.failure_category == "interrupted"
        assert assistant.content == ""
        assert assistant.completed_at is not None


def test_partial_stream_failure_is_not_persisted_or_reused_as_history(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    class FailingAfterAnswerAgent:
        persisted_user_message: tuple[str, str, str] | None = None

        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages
            assert runtime.user_message_id is not None
            with runtime.session_factory() as session:
                user_message = session.get(CoachMessage, runtime.user_message_id)
                assert user_message is not None
                self.persisted_user_message = (
                    user_message.role,
                    user_message.status,
                    user_message.content,
                )
            yield CoachEvent("answer_text", text="Unfinished private answer")
            yield CoachEvent("failed")

    failing = FailingAfterAnswerAgent()
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: failing
    conversation_id = _new_chat(client)

    failed = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "first-question"},
    )
    assert failed.status_code == 200
    assert "event: error" in failed.text

    assert failing.persisted_user_message == ("user", "completed", "first-question")
    with session_factory() as session:
        messages = list(
            session.scalars(
                select(CoachMessage)
                .where(CoachMessage.conversation_id == conversation_id)
                .order_by(CoachMessage.id)
            )
        )
        assert [(message.role, message.status, message.content) for message in messages] == [
            ("user", "completed", "first-question"),
            ("assistant", "failed", ""),
        ]
        assert messages[1].failure_category == "provider_error"

    reloaded = client.get(f"/coach/{conversation_id}")
    assert reloaded.status_code == 200
    failure_copy = "Diese Antwort konnte nicht abgeschlossen werden. Bitte versuche es erneut."
    assert failure_copy in failed.text
    assert failure_copy in reloaded.text
    assert "Unfinished private answer" not in reloaded.text

    succeeding = RecordingCoachAgent()
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: succeeding
    follow_up = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "follow-up-question"},
    )

    assert follow_up.status_code == 200
    assert [(item.role, item.content) for item in succeeding.calls[0]] == [
        ("user", "first-question"),
        ("user", "follow-up-question"),
    ]


def test_missing_final_answer_text_is_a_durable_failed_outcome(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    class EmptyCompletionAgent:
        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages, runtime
            yield CoachEvent("completed")

    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: EmptyCompletionAgent()
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Return no answer"},
    )

    failure_copy = "Diese Antwort konnte nicht abgeschlossen werden. Bitte versuche es erneut."
    assert response.status_code == 200
    assert "event: error" in response.text
    assert failure_copy in response.text
    with session_factory() as session:
        assistant = session.scalar(
            select(CoachMessage).where(
                CoachMessage.conversation_id == conversation_id,
                CoachMessage.role == "assistant",
            )
        )
        assert assistant is not None
        assert assistant.status == "failed"
        assert assistant.failure_category == "missing_final_answer"
        assert assistant.content == ""
        assert assistant.completed_at is not None

    reloaded = client.get(f"/coach/{conversation_id}")
    assert reloaded.status_code == 200
    assert failure_copy in reloaded.text


def test_completed_answer_is_persisted_and_visible_after_reload(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    agent = RecordingCoachAgent()
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: agent
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "completed-question"},
    )

    assert response.status_code == 200
    assert "event: answer.completed" in response.text
    with session_factory() as session:
        messages = list(
            session.scalars(
                select(CoachMessage)
                .where(CoachMessage.conversation_id == conversation_id)
                .order_by(CoachMessage.id)
            )
        )
        assert [(message.role, message.status, message.content) for message in messages] == [
            ("user", "completed", "completed-question"),
            ("assistant", "completed", "Recorded answer"),
        ]

    reloaded = client.get(f"/coach/{conversation_id}")
    assert reloaded.status_code == 200
    assert "Recorded answer" in reloaded.text


def test_assistant_message_is_the_only_runtime_execution_record(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    app.dependency_overrides[get_coach_agent_factory] = lambda: lambda: RecordingCoachAgent()
    conversation_id = _new_chat(client)

    response = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "completed-question"},
    )

    assert response.status_code == 200
    with session_factory() as session:
        assistant = session.scalar(
            select(CoachMessage).where(
                CoachMessage.conversation_id == conversation_id,
                CoachMessage.role == "assistant",
            )
        )
        assert assistant is not None
        assert assistant.status == "completed"
        assert assistant.model_id is not None
        assert assistant.request_id is not None
        assert assistant.prompt_template_version == COACH_PROMPT_TEMPLATE_VERSION
        assert assistant.operation_contract_version == COACH_TOOL_CONTRACT_VERSION
        assert assistant.completed_at is not None
        assert session.scalar(select(CoachAssistantRun)) is None
