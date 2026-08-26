from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
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
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    session_secret: str | None = Field(default=None, min_length=32)
    session_https_only: bool = False
    public_base_url: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    github_client_id: str | None = None
    github_client_secret: str | None = None
    llm_api_key: str | None = None
    llm_model: str = ""
    llm_timeout_seconds: float = Field(default=60, ge=5, le=180)
    coach_workout_proposals_enabled: bool = False
    coach_garmin_sync_enabled: bool = False
    coach_daily_adaptation_enabled: bool = False
    coach_plan_generation_enabled: bool = False
    coach_planner_history_gates_enabled: bool = True
    coach_deferred_quality_templates_enabled: bool = False
    mutation_rate_limit_per_minute: int = Field(default=120, ge=10, le=10_000)
    coach_rate_limit_per_minute: int = Field(default=12, ge=1, le=1_000)
    auth_rate_limit_per_minute: int = Field(default=20, ge=1, le=1_000)
    metrics_bearer_token: str | None = None
    garmin_operation_stale_minutes: int = Field(default=15, ge=5, le=1440)
    coach_rollout_user_ids: str = ""
    account_export_rate_limit_per_minute: int = Field(default=2, ge=1, le=60)

    @field_validator("metrics_bearer_token", mode="before")
    @classmethod
    def normalize_metrics_token(cls, value: object) -> object:
        return None if value == "" else value

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
            if not self.coach_planner_history_gates_enabled:
                raise ValueError(
                    "COACH_PLANNER_HISTORY_GATES_ENABLED must be enabled in production"
                )
        if self.coach_deferred_quality_templates_enabled and self.environment != "development":
            raise ValueError(
                "COACH_DEFERRED_QUALITY_TEMPLATES_ENABLED is allowed only in development"
            )
        if not self.coach_planner_history_gates_enabled and self.environment != "development":
            raise ValueError(
                "COACH_PLANNER_HISTORY_GATES_ENABLED may be disabled only in development"
            )
        if self.metrics_bearer_token is not None and len(self.metrics_bearer_token) < 32:
            raise ValueError("METRICS_BEARER_TOKEN must contain at least 32 characters")
        if not self.coach_workout_proposals_enabled and (
            self.coach_garmin_sync_enabled
            or self.coach_daily_adaptation_enabled
            or self.coach_plan_generation_enabled
        ):
            raise ValueError("Coach workout features require COACH_WORKOUT_PROPOSALS_ENABLED")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


DEFERRED_QUALITY_TEMPLATE_IDS = frozenset({"threshold_cruise", "vo2_intervals"})


def deferred_quality_templates_enabled() -> bool:
    settings = get_settings()
    return (
        settings.environment == "development" and settings.coach_deferred_quality_templates_enabled
    )


def coach_feature_enabled(enabled: bool, user_id: int) -> bool:
    if not enabled:
        return False
    configured = get_settings().coach_rollout_user_ids.strip()
    if not configured:
        return True
    try:
        allowed = {int(value.strip()) for value in configured.split(",") if value.strip()}
    except ValueError:
        return False
    return user_id in allowed
