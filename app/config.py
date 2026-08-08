from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "PacePilot"
    environment: str = "development"
    database_url: str = "sqlite:///./data/app.db"
    data_dir: Path = Path("./data")
    garmin_token_dir: Path = Path("./data/garmin-tokens")
    garmin_email: str | None = None
    garmin_password: str | None = None
    sync_days: int = Field(default=14, ge=1, le=90)
    health_sync_overlap_days: int = Field(default=7, ge=1, le=31)
    sync_interval_minutes: int = Field(default=60, ge=5)
    garmin_call_delay_seconds: float = Field(default=0.75, ge=0, le=10)
    scheduler_enabled: bool = True
    session_secret: str | None = Field(default=None, min_length=32)
    session_https_only: bool = False
    google_client_id: str | None = None
    google_client_secret: str | None = None
    github_client_id: str | None = None
    github_client_secret: str | None = None
    auth_legacy_user_email: str | None = None
    llm_api_key: str | None = None
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
