import pytest
from pydantic import ValidationError

from app.config import Settings


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
