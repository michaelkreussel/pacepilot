from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "PacePilot"
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./data/app.db"
    data_dir: Path = Path("./data")
    garmin_token_dir: Path = Path("./data/garmin-tokens")
    garmin_email: str | None = None
    garmin_password: str | None = None
    health_sync_overlap_days: int = Field(default=7, ge=1, le=31)
    sync_interval_minutes: int = Field(default=60, ge=5)
    garmin_sync_workers: int = Field(default=2, ge=1, le=8)
    garmin_call_delay_seconds: float = Field(default=0.75, ge=0, le=10)
    garmin_activity_initial_enrichment: int = Field(default=0, ge=0, le=1000)
    garmin_activity_enrichment_per_sync: int = Field(default=5, ge=0, le=1000)
    garmin_rate_limit_cooldown_seconds: int = Field(default=300, ge=60, le=3600)
    scheduler_enabled: bool = True
    session_secret: str | None = Field(default=None, min_length=32)
    session_https_only: bool = False
    public_base_url: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    github_client_id: str | None = None
    github_client_secret: str | None = None
    llm_api_key: str | None = None
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = ""

    @model_validator(mode="after")
    def validate_deployment(self) -> "Settings":
        for provider, client_id, client_secret in (
            ("Google", self.google_client_id, self.google_client_secret),
            ("GitHub", self.github_client_id, self.github_client_secret),
        ):
            if bool(client_id) != bool(client_secret):
                raise ValueError(f"{provider} client ID and secret must be configured together")
        if self.environment == "production":
            if self.session_secret is None:
                raise ValueError("SESSION_SECRET must be configured in production")
            if not self.session_https_only:
                raise ValueError("SESSION_HTTPS_ONLY must be enabled in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
