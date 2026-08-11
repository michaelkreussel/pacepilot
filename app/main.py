import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import AuthenticationRequired
from app.config import get_settings
from app.database import engine
from app.jobs.scheduler import start_scheduler, stop_scheduler
from app.logging import configure_logging
from app.migrations import upgrade_database
from app.onboarding import OnboardingAccessRequired
from app.routes import (
    activities,
    auth,
    coach,
    dashboard,
    onboarding,
    plans,
    profile,
    settings,
    workouts,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    app_settings = get_settings()
    if app_settings.session_secret is None and (
        app_settings.environment != "development"
        or app_settings.google_client_id
        or app_settings.github_client_id
    ):
        raise RuntimeError("SESSION_SECRET must be configured before enabling login")
    app_settings.data_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(
        app_settings.data_dir / "logs" / "pacepilot.log",
        level=getattr(logging, app_settings.log_level),
    )
    app_settings.garmin_token_dir.mkdir(parents=True, exist_ok=True)
    upgrade_database()
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()
        engine.dispose()


settings_config = get_settings()
app = FastAPI(
    title=settings_config.app_name,
    lifespan=lifespan,
    openapi_url=None if settings_config.environment == "production" else "/openapi.json",
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings_config.session_secret or "development-only-change-me-development-only",
    session_cookie="pacepilot_session",
    same_site="lax",
    https_only=settings_config.session_https_only,
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(auth.router)
app.include_router(onboarding.router)
app.include_router(dashboard.router)
app.include_router(profile.router)
app.include_router(activities.router)
app.include_router(plans.router)
app.include_router(workouts.router)
app.include_router(settings.router)
app.include_router(coach.router)


@app.exception_handler(AuthenticationRequired)
def authentication_required(request: Request, _: AuthenticationRequired) -> RedirectResponse:
    target = request.url.path
    if request.method in {"GET", "HEAD"} and request.url.query:
        target = f"{target}?{request.url.query}"
    if request.method not in {"GET", "HEAD"}:
        target = "/"
    response = RedirectResponse(f"/login?{urlencode({'next': target})}", status_code=303)
    if request.headers.get("HX-Request") == "true":
        response.headers["HX-Redirect"] = response.headers["location"]
    return response


@app.exception_handler(OnboardingAccessRequired)
def onboarding_required(request: Request, exc: OnboardingAccessRequired) -> RedirectResponse:
    location = f"/onboarding?{urlencode({'blocked': exc.area})}"
    response = RedirectResponse(location, status_code=303)
    if request.headers.get("HX-Request") == "true":
        response.headers["HX-Redirect"] = location
    return response


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
