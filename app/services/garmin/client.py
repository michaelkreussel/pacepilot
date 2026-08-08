import shutil
from pathlib import Path
from typing import Any

from garminconnect import Garmin
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import GarminAccount


class GarminUnavailableError(RuntimeError):
    pass


def _token_directory(account_id: int | None, adopt_legacy_tokens: bool) -> Path:
    settings = get_settings()
    base_dir = Path(settings.garmin_token_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    if account_id is None:
        return base_dir
    if account_id < 1:
        raise ValueError("Garmin account ID must be positive")

    token_dir = base_dir / f"account-{account_id}"
    token_dir.mkdir(parents=True, exist_ok=True)
    legacy_token = base_dir / "garmin_tokens.json"
    account_token = token_dir / "garmin_tokens.json"
    if adopt_legacy_tokens and legacy_token.is_file() and not account_token.exists():
        shutil.copy2(legacy_token, account_token)
    return token_dir


def connect_garmin(
    email: str | None = None,
    password: str | None = None,
    *,
    account_id: int | None = None,
    adopt_legacy_tokens: bool = False,
) -> Garmin:
    settings = get_settings()
    token_dir = _token_directory(account_id, adopt_legacy_tokens)
    client = Garmin(
        email=email or settings.garmin_email,
        password=password or settings.garmin_password,
    )
    try:
        client.login(str(token_dir))
    except Exception as exc:
        raise GarminUnavailableError(str(exc)) from exc
    return client


def connect_garmin_account(session: Session, account: GarminAccount) -> Garmin:
    first_connected_account_id = session.scalar(
        select(GarminAccount.id)
        .where(GarminAccount.connected_at.is_not(None))
        .order_by(GarminAccount.id)
        .limit(1)
    )
    return connect_garmin(
        email=account.email,
        account_id=account.id,
        adopt_legacy_tokens=account.id == first_connected_account_id,
    )


def message_from_exception(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:1000]


GarminResponse = dict[str, Any]
