import hashlib
import threading
import time
from collections import deque
from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.config import Settings, get_settings
from app.http_security import SAFE_METHODS

WINDOW_SECONDS = 60
SESSION_USER_ID = "user_id"


@dataclass(frozen=True)
class RateLimitResult:
    limit: int
    remaining: int
    retry_after: int


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str], deque[float]] = {}

    def check(
        self,
        bucket: str,
        key: str,
        limit: int,
        *,
        now: float | None = None,
    ) -> RateLimitResult:
        current = time.monotonic() if now is None else now
        cutoff = current - WINDOW_SECONDS
        identity = (bucket, key)
        with self._lock:
            requests = self._requests.setdefault(identity, deque())
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= limit:
                retry_after = max(int(WINDOW_SECONDS - (current - requests[0])) + 1, 1)
                return RateLimitResult(limit=limit, remaining=0, retry_after=retry_after)
            requests.append(current)
            return RateLimitResult(
                limit=limit,
                remaining=max(limit - len(requests), 0),
                retry_after=0,
            )

    def clear(self) -> None:
        with self._lock:
            self._requests.clear()


limiter = SlidingWindowLimiter()


def _client_key(request: Request) -> str:
    user_id = request.session.get(SESSION_USER_ID)
    if type(user_id) is int:
        return f"user:{user_id}"
    csrf_token = request.session.get("_csrf_token")
    if isinstance(csrf_token, str) and csrf_token:
        digest = hashlib.sha256(csrf_token.encode()).hexdigest()
        return f"session:{digest}"
    session_cookie = request.cookies.get("pacepilot_session")
    if session_cookie:
        digest = hashlib.sha256(session_cookie.encode()).hexdigest()
        return f"session:{digest}"
    host = request.client.host if request.client is not None else "unknown"
    return f"client:{host}"


def _rate_limit_policy(request: Request, settings: Settings) -> tuple[str, int] | None:
    path = request.url.path
    if path == "/account/export":
        return "account-export", settings.account_export_rate_limit_per_minute
    if path.startswith("/auth/") and (path.endswith("/login") or path.endswith("/callback")):
        return "auth", settings.auth_rate_limit_per_minute
    if path in {"/settings/garmin/connect", "/settings/garmin/mfa"}:
        return "auth", settings.auth_rate_limit_per_minute
    if request.method.upper() in SAFE_METHODS:
        return None
    if path.startswith("/coach/"):
        return "coach", settings.coach_rate_limit_per_minute
    return "mutation", settings.mutation_rate_limit_per_minute


async def require_rate_limit(request: Request) -> None:
    policy = _rate_limit_policy(request, get_settings())
    if policy is None:
        return
    bucket, limit = policy
    result = limiter.check(bucket, _client_key(request), limit)
    if result.retry_after:
        raise HTTPException(
            status_code=429,
            detail="Zu viele Anfragen. Bitte versuche es gleich erneut.",
            headers={"Retry-After": str(result.retry_after)},
        )
