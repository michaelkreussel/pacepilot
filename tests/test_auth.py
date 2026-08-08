from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth import oauth
from app.models import OAuthIdentity, User
from app.repositories.users import get_or_create_oauth_user


class FakeGoogleClient:
    async def authorize_access_token(self, _request: Any) -> dict[str, Any]:
        return {
            "userinfo": {
                "sub": "google-123",
                "name": "Ada Athlete",
                "email": "ada@example.com",
                "email_verified": True,
                "picture": "https://example.com/avatar.png",
            }
        }


def test_private_pages_redirect_to_login_and_healthcheck_stays_public(
    unauthenticated_client: TestClient,
) -> None:
    response = unauthenticated_client.get(
        "/profile?period=week",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fprofile%3Fperiod%3Dweek"
    assert unauthenticated_client.get("/api/health").json() == {"status": "ok"}


def test_google_callback_creates_session_and_reuses_identity(
    unauthenticated_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    fake_client = FakeGoogleClient()
    monkeypatch.setattr(
        oauth,
        "create_client",
        lambda provider: fake_client if provider == "google" else None,
    )

    response = unauthenticated_client.get(
        "/auth/google/callback",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    dashboard = unauthenticated_client.get("/")
    assert dashboard.status_code == 200
    assert "Ada Athlete" in dashboard.text

    unauthenticated_client.get("/auth/google/callback", follow_redirects=False)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 1
        identity = session.scalar(select(OAuthIdentity))
        assert identity is not None
        assert identity.provider == "google"
        assert identity.subject == "google-123"
        assert identity.email == "ada@example.com"
        assert identity.email_verified is True


def test_logout_clears_session(unauthenticated_client: TestClient, monkeypatch: Any) -> None:
    fake_client = FakeGoogleClient()
    monkeypatch.setattr(oauth, "create_client", lambda _provider: fake_client)
    unauthenticated_client.get("/auth/google/callback")

    response = unauthenticated_client.post("/logout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert unauthenticated_client.get("/", follow_redirects=False).status_code == 303


def test_legacy_user_is_only_adopted_by_matching_verified_email(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        legacy = User(display_name="Legacy")
        session.add(legacy)
        session.commit()
        legacy_id = legacy.id

        adopted = get_or_create_oauth_user(
            session,
            provider="google",
            subject="allowed",
            display_name="Allowed",
            email="owner@example.com",
            email_verified=True,
            username=None,
            avatar_url=None,
            legacy_user_email="owner@example.com",
        )

        assert adopted.id == legacy_id

    with session_factory() as session:
        separate = get_or_create_oauth_user(
            session,
            provider="github",
            subject="different",
            display_name="Different",
            email="other@example.com",
            email_verified=True,
            username="different",
            avatar_url=None,
            legacy_user_email="owner@example.com",
        )

        assert separate.id != legacy_id
