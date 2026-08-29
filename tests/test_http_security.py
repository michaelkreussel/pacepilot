import json
import re

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.rate_limits import SlidingWindowLimiter, limiter
from app.services.coach.dependencies import get_coach_agent_factory


def _workout_data(csrf_token: str) -> dict[str, str]:
    return {
        "_csrf_token": csrf_token,
        "name": "CSRF Test",
        "sport": "running",
        "scheduled_for": "",
        "description": "",
        "definition": json.dumps(
            {
                "blocks": [
                    {
                        "id": "easy",
                        "kind": "step",
                        "step_type": "interval",
                        "end": {"type": "time", "seconds": 1800},
                        "target": {"type": "none"},
                    }
                ]
            }
        ),
    }


def test_csrf_rejects_missing_and_invalid_tokens(client: TestClient) -> None:
    missing = client.post(
        "/coach/conversations",
        headers={"X-CSRF-Token": ""},
        follow_redirects=False,
    )
    invalid = client.post(
        "/coach/conversations",
        headers={"X-CSRF-Token": "invalid"},
        follow_redirects=False,
    )

    assert missing.status_code == 403
    assert invalid.status_code == 403


def test_csrf_accepts_header_token(client: TestClient) -> None:
    response = client.post("/coach/conversations", follow_redirects=False)

    assert response.status_code == 303


def test_csrf_form_token_does_not_consume_workout_body(client: TestClient) -> None:
    token = client.headers.pop("X-CSRF-Token")
    try:
        response = client.post(
            "/workouts",
            data=_workout_data(token),
            follow_redirects=False,
        )
    finally:
        client.headers["X-CSRF-Token"] = token

    assert response.status_code == 303


def test_csrf_token_from_another_session_is_rejected(
    client: TestClient, unauthenticated_client: TestClient
) -> None:
    page = unauthenticated_client.get("/login")
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)
    assert match is not None
    other_token = match.group(1)

    foreign_for_other = unauthenticated_client.post(
        "/logout",
        headers={"X-CSRF-Token": client.headers["X-CSRF-Token"]},
        follow_redirects=False,
    )
    foreign_for_client = client.post(
        "/coach/conversations",
        headers={"X-CSRF-Token": other_token},
        follow_redirects=False,
    )
    own_session = unauthenticated_client.post(
        "/logout",
        headers={"X-CSRF-Token": other_token},
        follow_redirects=False,
    )

    assert foreign_for_other.status_code == 403
    assert foreign_for_client.status_code == 403
    assert own_session.status_code == 303


def test_csrf_rejects_oversized_form_before_parsing(client: TestClient) -> None:
    token = client.headers.pop("X-CSRF-Token")
    try:
        response = client.post(
            "/coach/conversations",
            data={"_csrf_token": token, "padding": "x" * 1_048_576},
            follow_redirects=False,
        )
    finally:
        client.headers["X-CSRF-Token"] = token

    assert response.status_code == 413


def test_csrf_rejects_oversized_form_with_header_token(client: TestClient) -> None:
    response = client.post(
        "/coach/conversations",
        data={"padding": "x" * 1_048_576},
        follow_redirects=False,
    )

    assert response.status_code == 413


def test_dynamic_responses_have_security_and_no_store_headers(client: TestClient) -> None:
    response = client.get("/")

    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_static_responses_keep_cache_policy_separate(client: TestClient) -> None:
    response = client.get("/static/js/theme.js")

    assert response.status_code == 200
    assert "cache-control" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"


def test_sliding_window_rate_limiter_isolated_and_resets() -> None:
    test_limiter = SlidingWindowLimiter()

    assert test_limiter.check("coach", "user:1", 2, now=100).remaining == 1
    assert test_limiter.check("coach", "user:1", 2, now=101).remaining == 0
    blocked = test_limiter.check("coach", "user:1", 2, now=102)
    other_user = test_limiter.check("coach", "user:2", 2, now=102)
    reset = test_limiter.check("coach", "user:1", 2, now=161)

    assert blocked.retry_after == 59
    assert other_user.remaining == 1
    assert reset.retry_after == 0


def test_coach_rate_limit_runs_after_csrf_and_returns_retry_after(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "coach_rate_limit_per_minute", 1)
    limiter.clear()

    invalid = client.post(
        "/coach/conversations",
        headers={"X-CSRF-Token": "invalid"},
        follow_redirects=False,
    )
    accepted = client.post("/coach/conversations", follow_redirects=False)
    limited = client.post("/coach/conversations", follow_redirects=False)

    assert invalid.status_code == 403
    assert accepted.status_code == 303
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0


def test_conversational_planning_mutation_uses_coach_rate_limit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = client.post("/coach/conversations", follow_redirects=False)
    conversation_id = int(created.headers["location"].rsplit("/", 1)[1])
    monkeypatch.setattr(get_settings(), "coach_rate_limit_per_minute", 1)
    app.dependency_overrides[get_coach_agent_factory] = lambda: None
    limiter.clear()

    accepted = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Setze meine Verfügbarkeit."},
    )
    limited = client.post(
        f"/coach/{conversation_id}/messages",
        data={"message": "Setze meine Verfügbarkeit erneut."},
    )

    assert accepted.status_code == 503
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0


def test_planning_confirmation_uses_coach_rate_limit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "coach_rate_limit_per_minute", 1)
    limiter.clear()

    missing = client.post(
        "/coach/999/messages/999/planning-goal-confirmation",
        follow_redirects=False,
    )
    limited = client.post(
        "/coach/999/messages/999/planning-goal-confirmation",
        follow_redirects=False,
    )

    assert missing.status_code == 404
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0
