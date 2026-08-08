import logging
from pathlib import Path
from typing import Any

from garminconnect import Garmin
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import GarminAccount

logger = logging.getLogger("uvicorn.error")


class GarminUnavailableError(RuntimeError):
    pass


def _token_directory(account_id: int | None) -> Path:
    settings = get_settings()
    base_dir = Path(settings.garmin_token_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    if account_id is None:
        return base_dir
    if account_id < 1:
        raise ValueError("Garmin account ID must be positive")

    token_dir = base_dir / f"account-{account_id}"
    token_dir.mkdir(parents=True, exist_ok=True)
    return token_dir


def connect_garmin(
    email: str | None = None,
    password: str | None = None,
    *,
    account_id: int | None = None,
) -> Garmin:
    try:
        settings = get_settings()
        token_dir = _token_directory(account_id)
        client = Garmin(
            email=email or settings.garmin_email,
            password=password or settings.garmin_password,
        )
        client.login(str(token_dir))
    except Exception as exc:
        logger.exception("Garmin login failed")
        raise GarminUnavailableError("Garmin Connect ist derzeit nicht erreichbar.") from exc
    return client


def connect_garmin_account(_session: Session, account: GarminAccount) -> Garmin:
    return connect_garmin(
        email=account.email,
        account_id=account.id,
    )


def message_from_exception(exc: Exception) -> str:
    if isinstance(exc, GarminUnavailableError):
        return str(exc)[:1000]
    return "Die Garmin-Operation ist unerwartet fehlgeschlagen."


GarminResponse = dict[str, Any]
