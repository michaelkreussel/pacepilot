from pathlib import Path
from typing import Any

from garminconnect import Garmin

from app.config import get_settings


class GarminUnavailableError(RuntimeError):
    pass


def connect_garmin(email: str | None = None, password: str | None = None) -> Garmin:
    settings = get_settings()
    token_dir = Path(settings.garmin_token_dir)
    token_dir.mkdir(parents=True, exist_ok=True)
    client = Garmin(
        email=email or settings.garmin_email,
        password=password or settings.garmin_password,
    )
    try:
        client.login(str(token_dir))
    except Exception as exc:
        raise GarminUnavailableError(str(exc)) from exc
    return client


def message_from_exception(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:1000]


GarminResponse = dict[str, Any]
