import json
import re

from fastapi.testclient import TestClient


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
