import base64
import json
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from itsdangerous import TimestampSigner
from sqlalchemy.orm import Session, sessionmaker

from app.models import OAuthIdentity, User
from scripts.agent_browser_login import (
    BrowserLoginError,
    authenticate_browser,
    create_session_cookie,
    normalize_base_url,
    select_user,
    validate_session_name,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("https://[::1]:8443", "https://[::1]:8443"),
    ],
)
def test_normalize_base_url_accepts_only_loopback_origins(value: str, expected: str) -> None:
    assert normalize_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com",
        "http://user@localhost:8000",
        "http://localhost:8000/settings",
        "http://localhost:8000/?next=/settings",
    ],
)
def test_normalize_base_url_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(BrowserLoginError):
        normalize_base_url(value)


def test_validate_session_name_rejects_shell_metacharacters() -> None:
    assert validate_session_name("pacepilot-dev_1") == "pacepilot-dev_1"
    with pytest.raises(BrowserLoginError):
        validate_session_name("pacepilot-dev&whoami")


def test_select_user_defaults_to_most_recent_oauth_login(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_factory() as db:
        older = User(display_name="Older")
        newer = User(display_name="Newer")
        db.add_all([older, newer])
        db.flush()
        db.add_all(
            [
                OAuthIdentity(
                    user_id=older.id,
                    provider="google",
                    subject="older",
                    last_login_at=now - timedelta(days=1),
                ),
                OAuthIdentity(
                    user_id=newer.id,
                    provider="google",
                    subject="newer",
                    last_login_at=now,
                ),
            ]
        )
        db.commit()

        assert select_user(db, None).id == newer.id
        assert select_user(db, older.id).id == older.id


def test_create_session_cookie_matches_starlette_session_format() -> None:
    secret = "development-secret-with-at-least-32-characters"
    cookie = create_session_cookie(42, secret)

    signed_payload = TimestampSigner(secret).unsign(cookie)
    payload = json.loads(base64.b64decode(signed_payload))

    assert payload == {"user_id": 42}


def test_authenticate_browser_sets_cookie_and_enables_restore(monkeypatch: Any) -> None:
    calls: list[list[str]] = []

    def record(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check is True
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", record)

    authenticate_browser(
        "agent-browser",
        browser_session="pacepilot-dev",
        base_url="http://127.0.0.1:8000",
        cookie="signed-cookie",
    )

    first_args = calls[0][calls[0].index("--session") :]
    second_args = calls[1][calls[1].index("--session") :]
    assert first_args == [
        "--session",
        "pacepilot-dev",
        "--restore",
        "cookies",
        "set",
        "pacepilot_session",
        "signed-cookie",
        "--url",
        "http://127.0.0.1:8000/",
    ]
    assert second_args == [
        "--session",
        "pacepilot-dev",
        "--restore",
        "open",
        "http://127.0.0.1:8000/",
    ]
