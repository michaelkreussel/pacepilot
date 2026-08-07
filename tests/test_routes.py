from datetime import UTC, date, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import DailyHealth, GarminAccount
from app.services.garmin import sync as sync_module


def test_main_pages_render(client: TestClient) -> None:
    for path in ("/", "/activities", "/plans", "/workouts/new", "/coach", "/settings"):
        response = client.get(path)
        assert response.status_code == 200
        assert "PacePilot" in response.text

    assert client.get("/api/health").json() == {"status": "ok"}


def test_sync_status_partial_renders(client: TestClient) -> None:
    response = client.get("/settings/sync-status")
    assert response.status_code == 200
    assert 'id="sync-progress"' in response.text


def test_health_refresh_fetches_current_steps(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    class FakeGarmin:
        def get_user_summary(self, _day: str) -> dict[str, int]:
            return {"totalSteps": 2559}

    client.get("/")
    with session_factory() as session:
        account = session.scalar(select(GarminAccount))
        assert account is not None
        account.connected_at = datetime.now(UTC).replace(tzinfo=None)
        session.commit()

    monkeypatch.setattr(sync_module, "connect_garmin", lambda: FakeGarmin())

    response = client.post("/health")

    assert response.status_code == 200
    assert "2559" in response.text
    with session_factory() as session:
        health = session.scalar(select(DailyHealth).where(DailyHealth.day == date.today()))
        assert health is not None
        assert health.steps == 2559


def test_create_and_confirm_workout(client: TestClient) -> None:
    response = client.post(
        "/workouts",
        data={
            "name": "Lockerer Dauerlauf",
            "sport": "running",
            "scheduled_for": "2026-08-09",
            "description": "Ruhig bleiben",
            "step_type": ["warmup", "interval", "cooldown"],
            "duration_type": ["time", "time", "time"],
            "duration_value": ["10", "30", "5"],
            "repeat_count": ["1", "1", "1"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]

    detail = client.get(location)
    assert detail.status_code == 200
    assert "Lockerer Dauerlauf" in detail.text
    assert "Entwurf bestätigen" in detail.text

    confirmed = client.post(f"{location}/confirm", follow_redirects=True)
    assert confirmed.status_code == 200
    assert "An Garmin übertragen" in confirmed.text


def test_invalid_workout_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/workouts",
        data={
            "name": "",
            "sport": "running",
            "step_type": "interval",
            "duration_type": "time",
            "duration_value": "0",
            "repeat_count": "1",
        },
    )
    assert response.status_code == 422
    assert "Bitte einen Namen angeben" in response.text
