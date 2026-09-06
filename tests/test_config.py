import pytest
from pydantic import ValidationError

from app.config import Settings, coach_provider_configured, get_settings


def test_production_requires_secure_session_configuration() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="production",
            session_secret="x" * 32,
            session_https_only=False,
        )


def test_oauth_credentials_must_be_configured_as_a_pair() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, github_client_id="client-id")


def test_removed_coach_capability_settings_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COACH_WORKOUT_PROPOSALS_ENABLED", "true")
    monkeypatch.setenv("COACH_GARMIN_SYNC_ENABLED", "true")
    monkeypatch.setenv("COACH_DAILY_ADAPTATION_ENABLED", "true")
    monkeypatch.setenv("COACH_PLAN_GENERATION_ENABLED", "true")
    monkeypatch.setenv("COACH_ROLLOUT_USER_IDS", "1,2")
    monkeypatch.setenv("COACH_PLANNER_HISTORY_GATES_ENABLED", "false")
    monkeypatch.setenv("COACH_DEFERRED_QUALITY_TEMPLATES_ENABLED", "true")
    settings = Settings(_env_file=None)

    assert not hasattr(settings, "coach_workout_proposals_enabled")
    assert not hasattr(settings, "coach_garmin_sync_enabled")
    assert not hasattr(settings, "coach_daily_adaptation_enabled")
    assert not hasattr(settings, "coach_plan_generation_enabled")
    assert not hasattr(settings, "coach_rollout_user_ids")
    assert not hasattr(settings, "coach_planner_history_gates_enabled")
    assert not hasattr(settings, "coach_deferred_quality_templates_enabled")


def test_coach_defaults_to_zai_glm_flash() -> None:
    settings = Settings(_env_file=None)

    assert settings.llm_model == "z-ai/glm-5.3-flash"


@pytest.mark.parametrize(
    ("api_key", "model", "expected"),
    (
        (None, "", False),
        ("test-key", "", False),
        (None, "test-model", False),
        ("test-key", "test-model", True),
    ),
)
def test_coach_provider_requires_credentials_and_model(
    monkeypatch: pytest.MonkeyPatch,
    api_key: str | None,
    model: str,
    expected: bool,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", api_key)
    monkeypatch.setattr(settings, "llm_model", model)

    assert coach_provider_configured() is expected


def test_empty_optional_metrics_token_is_normalized() -> None:
    settings = Settings(_env_file=None, metrics_bearer_token="")

    assert settings.metrics_bearer_token is None
    with pytest.raises(ValidationError, match="METRICS_BEARER_TOKEN"):
        Settings(_env_file=None, metrics_bearer_token="too-short")
