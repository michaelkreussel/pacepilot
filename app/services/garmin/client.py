import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from garminconnect import Garmin
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import GarminAccount
from app.services.garmin.locks import GarminAccountBusyError, garmin_account_slot

logger = logging.getLogger(__name__)


class GarminUnavailableError(RuntimeError):
    pass


class GarminMfaExpiredError(GarminUnavailableError):
    pass


MFA_CHALLENGE_TTL_SECONDS = 10 * 60


@dataclass
class _PendingMfaLogin:
    client: Garmin
    email: str
    account_id: int
    user_id: int
    token_directory: Path
    created_at: float
    in_progress: bool = False


_pending_mfa_logins: dict[str, _PendingMfaLogin] = {}
_pending_mfa_lock = threading.Lock()


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


def _login_error(exc: Exception, *, mfa: bool = False) -> GarminUnavailableError:
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return GarminUnavailableError(
            "Zu viele Garmin-Anmeldeversuche. Bitte warte einige Minuten."
        )
    if isinstance(exc, GarminConnectAuthenticationError):
        message = (
            "Der Bestätigungscode ist ungültig oder abgelaufen."
            if mfa
            else "E-Mail-Adresse oder Passwort ist ungültig."
        )
        return GarminUnavailableError(message)
    return GarminUnavailableError("Garmin Connect ist derzeit nicht erreichbar.")


def _remove_expired_mfa_logins() -> None:
    cutoff = time.monotonic() - MFA_CHALLENGE_TTL_SECONDS
    expired = [
        challenge_id
        for challenge_id, login in _pending_mfa_logins.items()
        if login.created_at < cutoff
    ]
    for challenge_id in expired:
        del _pending_mfa_logins[challenge_id]


def start_garmin_login(email: str, password: str, *, account_id: int, user_id: int) -> str | None:
    try:
        with garmin_account_slot(account_id):
            return _start_garmin_login(email, password, account_id=account_id, user_id=user_id)
    except GarminAccountBusyError as exc:
        raise GarminUnavailableError(
            "Für dieses Garmin-Konto läuft gerade eine andere Operation."
        ) from exc


def _start_garmin_login(email: str, password: str, *, account_id: int, user_id: int) -> str | None:
    """Start a credential login and return an opaque challenge ID when MFA is required."""
    token_directory = _token_directory(account_id)
    try:
        client = Garmin(email=email, password=password, return_on_mfa=True)
        mfa_status, _ = client.login()
    except Exception as exc:
        logger.exception("Garmin login failed")
        raise _login_error(exc) from exc

    if mfa_status != "needs_mfa":
        try:
            client.client.dump(str(token_directory))
        except Exception as exc:
            logger.exception("Garmin token persistence failed")
            raise GarminUnavailableError(
                "Der Garmin-Token konnte nicht gespeichert werden."
            ) from exc
        return None

    client.password = None
    challenge_id = secrets.token_urlsafe(32)
    with _pending_mfa_lock:
        _remove_expired_mfa_logins()
        for existing_id, login in list(_pending_mfa_logins.items()):
            if login.account_id == account_id or login.user_id == user_id:
                del _pending_mfa_logins[existing_id]
        _pending_mfa_logins[challenge_id] = _PendingMfaLogin(
            client=client,
            email=email,
            account_id=account_id,
            user_id=user_id,
            token_directory=token_directory,
            created_at=time.monotonic(),
        )
    return challenge_id


def pending_garmin_login(challenge_id: str | None, *, account_id: int, user_id: int) -> bool:
    if challenge_id is None:
        return False
    with _pending_mfa_lock:
        _remove_expired_mfa_logins()
        login = _pending_mfa_logins.get(challenge_id)
        return login is not None and login.account_id == account_id and login.user_id == user_id


def finish_garmin_login(
    challenge_id: str | None,
    mfa_code: str,
    *,
    account_id: int,
    user_id: int,
) -> str:
    """Complete a pending MFA login, persist its token, and return the Garmin email."""
    try:
        with garmin_account_slot(account_id):
            return _finish_garmin_login(
                challenge_id,
                mfa_code,
                account_id=account_id,
                user_id=user_id,
            )
    except GarminAccountBusyError as exc:
        raise GarminUnavailableError(
            "Für dieses Garmin-Konto läuft gerade eine andere Operation."
        ) from exc


def _finish_garmin_login(
    challenge_id: str | None,
    mfa_code: str,
    *,
    account_id: int,
    user_id: int,
) -> str:
    if challenge_id is None:
        raise GarminMfaExpiredError(
            "Die Garmin-Anmeldung ist abgelaufen. Bitte melde dich erneut an."
        )

    with _pending_mfa_lock:
        _remove_expired_mfa_logins()
        login = _pending_mfa_logins.get(challenge_id)
        if (
            login is None
            or login.account_id != account_id
            or login.user_id != user_id
            or login.in_progress
        ):
            login = None
        else:
            login.in_progress = True
    if login is None:
        raise GarminMfaExpiredError(
            "Die Garmin-Anmeldung ist abgelaufen. Bitte melde dich erneut an."
        )

    try:
        login.client.resume_login({}, mfa_code.strip())
        login.client.client.dump(str(login.token_directory))
    except Exception as exc:
        with _pending_mfa_lock:
            pending = _pending_mfa_logins.get(challenge_id)
            if pending is login:
                pending.in_progress = False
        logger.exception("Garmin MFA verification failed")
        raise _login_error(exc, mfa=True) from exc

    with _pending_mfa_lock:
        _pending_mfa_logins.pop(challenge_id, None)
    return login.email


def cancel_garmin_login(challenge_id: str | None, *, account_id: int, user_id: int) -> None:
    if challenge_id is None:
        return
    with _pending_mfa_lock:
        login = _pending_mfa_logins.get(challenge_id)
        if login is not None and login.account_id == account_id and login.user_id == user_id:
            del _pending_mfa_logins[challenge_id]


def cancel_garmin_account_logins(*, account_id: int, user_id: int) -> None:
    with _pending_mfa_lock:
        for challenge_id, login in list(_pending_mfa_logins.items()):
            if login.account_id == account_id and login.user_id == user_id:
                del _pending_mfa_logins[challenge_id]


def connect_garmin(
    email: str | None = None,
    password: str | None = None,
    *,
    account_id: int | None = None,
) -> Garmin:
    logger.info(
        "Initializing account-scoped Garmin session",
        extra={"garmin_account_id": account_id, "uses_stored_token": True},
    )
    started = time.perf_counter()
    try:
        settings = get_settings()
        token_dir = _token_directory(account_id)
        client = Garmin(
            email=email or settings.garmin_email,
            password=password or settings.garmin_password,
        )
        client.login(str(token_dir))
    except Exception as exc:
        logger.exception(
            "Account-scoped Garmin session initialization failed",
            extra={
                "garmin_account_id": account_id,
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "error_type": type(exc).__name__,
            },
        )
        raise GarminUnavailableError("Garmin Connect ist derzeit nicht erreichbar.") from exc
    logger.info(
        "Account-scoped Garmin session initialized",
        extra={
            "garmin_account_id": account_id,
            "duration_ms": round((time.perf_counter() - started) * 1000),
        },
    )
    return client


def connect_garmin_account(_session: Session, account: GarminAccount) -> Garmin:
    return connect_garmin(
        email=account.email,
        account_id=account.id,
    )


def message_from_exception(exc: Exception) -> str:
    if isinstance(exc, GarminUnavailableError):
        return str(exc)[:1000]
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return "Garmin hat die Synchronisierung wegen zu vieler Anfragen begrenzt."
    if isinstance(exc, GarminConnectAuthenticationError):
        return "Die Garmin-Sitzung ist abgelaufen. Bitte verbinde das Konto erneut."
    return "Die Garmin-Operation ist unerwartet fehlgeschlagen."


GarminResponse = dict[str, Any]
