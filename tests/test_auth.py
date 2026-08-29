import re
from typing import Any

from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth import oauth
from app.config import get_settings
from app.models import OAuthIdentity, User


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


def test_conversational_planning_mutation_requires_authentication(
    unauthenticated_client: TestClient,
) -> None:
    login = unauthenticated_client.get("/login")
    match = re.search(r'name="_csrf_token" value="([^"]+)"', login.text)
    assert match is not None

    response = unauthenticated_client.post(
        "/coach/1/messages",
        data={"message": "Setze meine Verfügbarkeit."},
        headers={"X-CSRF-Token": match.group(1)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    confirmation = unauthenticated_client.post(
        "/coach/1/messages/1/planning-goal-confirmation",
        headers={"X-CSRF-Token": match.group(1)},
        follow_redirects=False,
    )
    assert confirmation.status_code == 303
    assert confirmation.headers["location"].startswith("/login")


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
    assert response.headers["location"] == "/onboarding"
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
    page = unauthenticated_client.get("/onboarding")
    match = re.search(r'name="_csrf_token" value="([^"]+)"', page.text)
    assert match is not None

    response = unauthenticated_client.post(
        "/logout",
        headers={"X-CSRF-Token": match.group(1)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert unauthenticated_client.get("/", follow_redirects=False).status_code == 303


class RecordingClient:
    def __init__(self) -> None:
        self.redirect_uri: str | None = None

    async def authorize_redirect(self, request: Any, redirect_uri: str) -> Any:
        self.redirect_uri = redirect_uri
        return RedirectResponse("https://example.com/authorize", status_code=302)


def _csrf_header(client: TestClient) -> dict[str, str]:
    page = client.get("/login")
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)
    assert match is not None
    return {"X-CSRF-Token": match.group(1)}


def test_oauth_login_uses_public_base_url_for_redirect_uri(
    unauthenticated_client: TestClient, monkeypatch: Any
) -> None:
    monkeypatch.setattr(get_settings(), "public_base_url", "https://example.com")
    recording = RecordingClient()
    monkeypatch.setattr(
        oauth,
        "create_client",
        lambda provider: recording if provider == "google" else None,
    )

    response = unauthenticated_client.post(
        "/auth/google/login",
        headers=_csrf_header(unauthenticated_client),
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert recording.redirect_uri == "https://example.com/auth/google/callback"


def test_oauth_login_redirect_uri_falls_back_to_request_url(
    unauthenticated_client: TestClient, monkeypatch: Any
) -> None:
    monkeypatch.setattr(get_settings(), "public_base_url", None)
    recording = RecordingClient()
    monkeypatch.setattr(
        oauth,
        "create_client",
        lambda provider: recording if provider == "google" else None,
    )

    unauthenticated_client.post(
        "/auth/google/login",
        headers=_csrf_header(unauthenticated_client),
        follow_redirects=False,
    )

    assert recording.redirect_uri == "http://testserver/auth/google/callback"
