from typing import Literal

import pytest
from pydantic import ValidationError

from app.config import Settings, coach_feature_enabled, coach_provider_configured, get_settings


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


def test_coach_workout_features_default_to_disabled() -> None:
    settings = Settings(_env_file=None)

    assert settings.coach_workout_proposals_enabled is False
    assert settings.coach_garmin_sync_enabled is False
    assert settings.coach_daily_adaptation_enabled is False
    assert settings.coach_plan_generation_enabled is False
    assert settings.coach_planner_history_gates_enabled is True
    assert settings.coach_deferred_quality_templates_enabled is False


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


def test_coach_workout_features_require_proposals() -> None:
    invalid_settings = (
        lambda: Settings(_env_file=None, coach_garmin_sync_enabled=True),
        lambda: Settings(_env_file=None, coach_daily_adaptation_enabled=True),
        lambda: Settings(_env_file=None, coach_plan_generation_enabled=True),
    )

    for create_settings in invalid_settings:
        with pytest.raises(ValidationError, match="COACH_WORKOUT_PROPOSALS_ENABLED"):
            create_settings()


def test_coach_workout_features_can_be_enabled_together() -> None:
    settings = Settings(
        _env_file=None,
        coach_workout_proposals_enabled=True,
        coach_garmin_sync_enabled=True,
        coach_daily_adaptation_enabled=True,
        coach_plan_generation_enabled=True,
    )

    assert settings.coach_workout_proposals_enabled is True
    assert settings.coach_garmin_sync_enabled is True
    assert settings.coach_daily_adaptation_enabled is True
    assert settings.coach_plan_generation_enabled is True


def test_production_rejects_disabled_planner_history_gates() -> None:
    with pytest.raises(ValidationError, match="COACH_PLANNER_HISTORY_GATES_ENABLED"):
        Settings(
            _env_file=None,
            environment="production",
            session_secret="x" * 32,
            session_https_only=True,
            coach_planner_history_gates_enabled=False,
        )


@pytest.mark.parametrize("environment", ["test", "production"])
def test_deferred_quality_templates_are_development_only(
    environment: Literal["test", "production"],
) -> None:
    with pytest.raises(ValidationError, match="COACH_DEFERRED_QUALITY_TEMPLATES_ENABLED"):
        Settings(
            _env_file=None,
            environment=environment,
            session_secret="x" * 32 if environment == "production" else None,
            session_https_only=environment == "production",
            coach_deferred_quality_templates_enabled=True,
        )


def test_history_gate_bypass_is_development_only() -> None:
    with pytest.raises(ValidationError, match="may be disabled only in development"):
        Settings(
            _env_file=None,
            environment="test",
            coach_planner_history_gates_enabled=False,
        )


def test_coach_rollout_allowlist_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "coach_rollout_user_ids", "2, 4")

    assert coach_feature_enabled(True, 2) is True
    assert coach_feature_enabled(True, 3) is False
    assert coach_feature_enabled(False, 2) is False

    monkeypatch.setattr(settings, "coach_rollout_user_ids", "invalid")
    assert coach_feature_enabled(True, 2) is False


def test_empty_optional_metrics_token_is_normalized() -> None:
    settings = Settings(_env_file=None, metrics_bearer_token="")

    assert settings.metrics_bearer_token is None
    with pytest.raises(ValidationError, match="METRICS_BEARER_TOKEN"):
        Settings(_env_file=None, metrics_bearer_token="too-short")
