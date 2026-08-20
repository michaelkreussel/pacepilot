"""Create a persistent authenticated agent-browser session for local development.

Run from the repository root while PacePilot is running:

    uv run python scripts/agent_browser_login.py
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from itsdangerous import TimestampSigner
from sqlalchemy import select
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import OAuthIdentity, User  # noqa: E402

SESSION_COOKIE = "pacepilot_session"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_BROWSER_SESSION = "pacepilot-dev"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
SESSION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class BrowserLoginError(RuntimeError):
    """Raised when a safe development login cannot be created."""


def normalize_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in LOOPBACK_HOSTS:
        raise BrowserLoginError("The base URL must use HTTP(S) on a loopback host.")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserLoginError("The base URL must not contain credentials.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise BrowserLoginError(
            "The base URL must be an origin without a path, query, or fragment."
        )
    try:
        _ = parsed.port
    except ValueError as exc:
        raise BrowserLoginError("The base URL contains an invalid port.") from exc
    return value.rstrip("/")


def validate_session_name(value: str) -> str:
    if not SESSION_NAME_PATTERN.fullmatch(value):
        raise BrowserLoginError(
            "The browser session name may only contain letters, numbers, dots, underscores, "
            "and dashes."
        )
    return value


def select_user(db: Session, user_id: int | None) -> User:
    if user_id is not None:
        user = db.get(User, user_id)
        if user is None:
            raise BrowserLoginError(f"No local PacePilot user with ID {user_id} exists.")
        return user

    user = db.scalar(
        select(User)
        .join(OAuthIdentity)
        .order_by(OAuthIdentity.last_login_at.desc(), User.id.desc())
        .limit(1)
    )
    if user is None:
        raise BrowserLoginError(
            "No OAuth user exists. Sign in to PacePilot once before using this helper."
        )
    return user


def create_session_cookie(user_id: int, secret: str) -> str:
    payload = base64.b64encode(
        json.dumps({"user_id": user_id}, separators=(",", ":")).encode("utf-8")
    )
    return TimestampSigner(secret).sign(payload).decode("utf-8")


def build_agent_browser_command(executable: str, *arguments: str) -> list[str]:
    if os.name == "nt" and Path(executable).suffix.casefold() in {".bat", ".cmd"}:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", executable, *arguments]
    return [executable, *arguments]


def authenticate_browser(
    executable: str,
    *,
    browser_session: str,
    base_url: str,
    cookie: str,
) -> None:
    common = ("--session", browser_session, "--restore")
    subprocess.run(
        build_agent_browser_command(
            executable,
            *common,
            "cookies",
            "set",
            SESSION_COOKIE,
            cookie,
            "--url",
            f"{base_url}/",
        ),
        check=True,
    )
    subprocess.run(
        build_agent_browser_command(executable, *common, "open", f"{base_url}/"),
        check=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Authenticate a persistent agent-browser session against local PacePilot."
    )
    parser.add_argument(
        "--session",
        default=DEFAULT_BROWSER_SESSION,
        help=f"agent-browser session name (default: {DEFAULT_BROWSER_SESSION})",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"running local PacePilot origin (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        help="local PacePilot user ID; defaults to the most recently logged-in OAuth user",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        settings = get_settings()
        if settings.environment != "development":
            raise BrowserLoginError("This helper only runs with ENVIRONMENT=development.")
        if settings.session_secret is None:
            raise BrowserLoginError("SESSION_SECRET must be configured before using this helper.")
        if args.user_id is not None and args.user_id < 1:
            raise BrowserLoginError("The user ID must be a positive integer.")

        base_url = normalize_base_url(args.base_url)
        browser_session = validate_session_name(args.session)
        executable = shutil.which("agent-browser")
        if executable is None:
            raise BrowserLoginError("agent-browser is not installed or not available on PATH.")

        with SessionLocal() as db:
            user = select_user(db, args.user_id)
            cookie = create_session_cookie(user.id, settings.session_secret)

        authenticate_browser(
            executable,
            browser_session=browser_session,
            base_url=base_url,
            cookie=cookie,
        )
    except BrowserLoginError as exc:
        parser.error(str(exc))
    except subprocess.CalledProcessError as exc:
        parser.error(f"agent-browser failed with exit code {exc.returncode}.")

    print(f"Authenticated agent-browser session '{browser_session}' for local user {user.id}.")


if __name__ == "__main__":
    main()
