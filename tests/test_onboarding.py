from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import GarminAccount, SyncRun, User
from app.onboarding import complete_onboarding


def _reset_onboarding(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        user.onboarding_notice_acknowledged_at = None
        user.onboarding_completed_at = None
        user.onboarding_completed_version = 0
        session.commit()
        return user.id


def test_new_user_resumes_onboarding_and_unlocks_areas_progressively(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    user_id = _reset_onboarding(session_factory)

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding"
    assert "Verstanden, Einrichtung starten" in client.get("/onboarding").text

    settings = client.get("/settings", follow_redirects=False)
    assert settings.status_code == 303
    assert settings.headers["location"] == "/onboarding?blocked=welcome"

    started = client.post("/onboarding/start", follow_redirects=False)
    assert started.status_code == 303
    assert started.headers["location"] == "/settings"
    assert "Garmin verbinden" in client.get("/settings").text

    plan = client.get("/plans", follow_redirects=False)
    assert plan.status_code == 303
    assert plan.headers["location"] == "/onboarding?blocked=planning"

    with session_factory() as session:
        account = session.scalar(select(GarminAccount).where(GarminAccount.user_id == user_id))
        assert account is not None
        account.connected_at = datetime.now(UTC).replace(tzinfo=None)
        account.sync_status = "connected"
        session.commit()

    assert client.get("/plans").status_code == 200
    activities = client.get("/activities", follow_redirects=False)
    assert activities.status_code == 303
    assert activities.headers["location"] == "/onboarding?blocked=data"

    with session_factory() as session:
        complete_onboarding(session, user_id)
        session.commit()

    assert client.get("/activities").status_code == 200
    assert client.get("/").status_code == 200


def test_review_and_help_do_not_reset_completed_onboarding(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    assert client.get("/help").status_code == 200
    review = client.get("/onboarding?review=true")
    assert review.status_code == 200
    assert "ändert deinen Einrichtungsstatus nicht" in review.text

    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        assert user.onboarding_completed_at is not None
        assert user.onboarding_completed_version == 1


def test_disconnect_after_completion_does_not_reopen_onboarding(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        session.add(GarminAccount(user_id=user.id, sync_status="not_connected"))
        session.commit()

    assert client.get("/activities").status_code == 200
    response = client.get("/onboarding", follow_redirects=False)
    assert response.headers["location"] == "/"


def test_completed_settings_only_show_regular_sync_status(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        session.add(
            GarminAccount(
                user_id=user.id,
                connected_at=now,
                last_sync_at=now,
                sync_status="ok",
            )
        )
        session.add(SyncRun(user_id=user.id, status="ok", finished_at=now))
        session.commit()

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Letzter Lauf abgeschlossen" in response.text
    assert "Einrichtung abgeschlossen" not in response.text
    assert "Ersteinrichtung" not in response.text
