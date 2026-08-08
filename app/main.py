from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.jobs.scheduler import start_scheduler, stop_scheduler
from app.routes import activities, coach, dashboard, plans, profile, settings, workouts


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    app_settings = get_settings()
    app_settings.data_dir.mkdir(parents=True, exist_ok=True)
    app_settings.garmin_token_dir.mkdir(parents=True, exist_ok=True)
    start_scheduler()
    yield
    stop_scheduler()


settings_config = get_settings()
app = FastAPI(title=settings_config.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(dashboard.router)
app.include_router(profile.router)
app.include_router(activities.router)
app.include_router(plans.router)
app.include_router(workouts.router)
app.include_router(settings.router)
app.include_router(coach.router)


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
