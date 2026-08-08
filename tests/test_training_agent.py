from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.main import app
from app.models import Activity, User, Workout
from app.services.training_agent.agno_backend import AgnoTrainingAgent
from app.services.training_agent.backend import TrainingAgentError, TrainingSnapshot
from app.services.training_agent.dependencies import get_training_agent


class FakeTrainingAgent:
    def __init__(self) -> None:
        self.message: str | None = None
        self.snapshot: TrainingSnapshot | None = None

    async def respond(self, message: str, snapshot: TrainingSnapshot) -> str:
        self.message = message
        self.snapshot = snapshot
        return "Plane zunächst eine lockere Einheit und prüfe danach deine Erholung."


def test_coach_uses_read_only_training_snapshot(
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
            )
        )
        session.commit()

    fake = FakeTrainingAgent()
    app.dependency_overrides[get_training_agent] = lambda: fake

    response = client.post("/coach", data={"message": "Plane meine nächste Laufwoche."})

    assert response.status_code == 200
    assert "Plane zunächst eine lockere Einheit" in response.text
    assert fake.message == "Plane meine nächste Laufwoche."
    assert fake.snapshot is not None
    assert fake.snapshot.recent_activities[0]["name"] == "Lockerer Morgenlauf"
    with session_factory() as session:
        assert session.scalar(select(Workout)) is None


def test_coach_requires_configured_backend(client: TestClient) -> None:
    app.dependency_overrides[get_training_agent] = lambda: None

    response = client.post("/coach", data={"message": "Plane meine Woche."})

    assert response.status_code == 503
    assert "Konfiguriere zuerst OpenRouter" in response.text


@pytest.mark.asyncio
async def test_agno_backend_returns_text_response() -> None:
    class FakeAgnoAgent:
        prompt: str | None = None

        async def arun(self, prompt: str) -> SimpleNamespace:
            self.prompt = prompt
            return SimpleNamespace(content="  Ein vorsichtiger Vorschlag.  ")

    backend = AgnoTrainingAgent.__new__(AgnoTrainingAgent)
    agno_agent = FakeAgnoAgent()
    backend._agent = agno_agent
    snapshot = TrainingSnapshot("2026-08-07", {}, (), (), ())

    answer = await backend.respond("Was steht an?", snapshot)

    assert answer == "Ein vorsichtiger Vorschlag."
    assert agno_agent.prompt is not None
    assert '"as_of":"2026-08-07"' in agno_agent.prompt


@pytest.mark.asyncio
async def test_agno_backend_hides_provider_errors() -> None:
    class FailingAgnoAgent:
        async def arun(self, prompt: str) -> None:
            del prompt
            raise RuntimeError("secret provider detail")

    backend = AgnoTrainingAgent.__new__(AgnoTrainingAgent)
    backend._agent = FailingAgnoAgent()

    with pytest.raises(TrainingAgentError, match="OpenRouter konnte"):
        await backend.respond("Was steht an?", TrainingSnapshot("2026-08-07", {}, (), (), ()))
