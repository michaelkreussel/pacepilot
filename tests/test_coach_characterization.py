from collections.abc import AsyncIterator, Sequence
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.main import app
from app.models import CoachAssistantRun, CoachConversation, CoachMessage
from app.models.user import utcnow
from app.services.coach.agent import CoachEvent, CoachHistoryMessage
from app.services.coach.dependencies import get_coach_agent
from app.services.coach.tools import CoachRuntimeContext


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
        yield CoachEvent("answer_delta", text="Recorded answer")


def _new_chat(client: TestClient) -> int:
    response = client.post("/coach/conversations", follow_redirects=False)
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[1])


def test_follow_up_uses_only_the_latest_twenty_completed_messages(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    agent = RecordingCoachAgent()
    app.dependency_overrides[get_coach_agent] = lambda: agent
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
        session.add_all(
            (
                CoachMessage(
                    conversation=conversation,
                    role="assistant",
                    content="failed partial answer",
                    status="failed",
                    completed_at=utcnow(),
                ),
                CoachMessage(
                    conversation=conversation,
                    role="assistant",
                    content="interrupted partial answer",
                    status="interrupted",
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
    assert [(item.role, item.content) for item in agent.calls[0]] == [
        (
            "user" if index % 2 == 0 else "assistant",
            f"history-{index:02d}",
        )
        for index in range(4, 24)
    ] + [("user", "current-question")]


def test_follow_up_limits_prior_history_to_twelve_thousand_characters(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    agent = RecordingCoachAgent()
    app.dependency_overrides[get_coach_agent] = lambda: agent
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
    app.dependency_overrides[get_coach_agent] = lambda: agent
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


def test_stale_response_is_interrupted_before_the_next_message(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    agent = RecordingCoachAgent()
    app.dependency_overrides[get_coach_agent] = lambda: agent
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


def test_partial_stream_failure_is_not_persisted_or_reused_as_history(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    class FailingAfterAnswerAgent:
        async def stream(
            self,
            messages: Sequence[CoachHistoryMessage],
            runtime: CoachRuntimeContext,
        ) -> AsyncIterator[CoachEvent]:
            del messages, runtime
            yield CoachEvent("answer_delta", text="Unfinished private answer")
            raise RuntimeError("provider failed after partial answer")

    app.dependency_overrides[get_coach_agent] = lambda: FailingAfterAnswerAgent()
    conversation_id = _new_chat(client)

    with pytest.raises(RuntimeError, match="provider failed after partial answer"):
        client.post(
            f"/coach/{conversation_id}/messages",
            data={"message": "first-question"},
        )

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
        failed_run = session.scalar(
            select(CoachAssistantRun).where(
                CoachAssistantRun.assistant_message_id == messages[1].id
            )
        )
        assert failed_run is not None
        assert failed_run.status == "failed"

    reloaded = client.get(f"/coach/{conversation_id}")
    assert reloaded.status_code == 200
    assert "Diese Antwort wurde nicht abgeschlossen" in reloaded.text
    assert "Unfinished private answer" not in reloaded.text

    succeeding = RecordingCoachAgent()
    app.dependency_overrides[get_coach_agent] = lambda: succeeding
    follow_up = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "follow-up-question"},
    )

    assert follow_up.status_code == 200
    assert [(item.role, item.content) for item in succeeding.calls[0]] == [
        ("user", "first-question"),
        ("user", "follow-up-question"),
    ]
