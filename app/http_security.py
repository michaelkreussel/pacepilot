import hmac
import secrets
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings

CSRF_FORM_FIELD = "_csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_SESSION_KEY = "_csrf_token"
REQUEST_ID_HEADER = "X-Request-ID"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
MAX_CSRF_FORM_BYTES = 1_048_576


def get_csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


async def require_csrf(request: Request) -> None:
    if request.method.upper() in SAFE_METHODS:
        return

    content_length = request.headers.get("Content-Length")
    if content_length is None:
        raise HTTPException(status_code=411, detail="Content-Length ist erforderlich.")
    try:
        body_size = int(content_length)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Content-Length ist ungueltig.") from exc
    if body_size < 0 or body_size > MAX_CSRF_FORM_BYTES:
        raise HTTPException(status_code=413, detail="Die Anfrage ist zu gross.")

    expected = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(expected, str) or not expected:
        raise HTTPException(status_code=403, detail="Die Anfrage konnte nicht verifiziert werden.")

    supplied = request.headers.get(CSRF_HEADER)
    if supplied is None:
        form = await request.form()
        value = form.get(CSRF_FORM_FIELD)
        supplied = value if isinstance(value, str) else None

    if not isinstance(supplied, str) or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="Die Anfrage konnte nicht verifiziert werden.")


def _request_id(value: str | None) -> str:
    if value is not None and len(value) <= 36:
        try:
            parsed = UUID(value)
        except ValueError:
            pass
        else:
            if str(parsed) == value.lower():
                return str(parsed)
    return str(uuid4())


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                headers["Content-Security-Policy"] = (
                    "base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'"
                )
                if get_settings().environment == "production":
                    headers["Strict-Transport-Security"] = "max-age=31536000"
                if not path.startswith("/static/"):
                    headers["Cache-Control"] = "private, no-store, max-age=0"
                    headers["Pragma"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_with_security_headers)
