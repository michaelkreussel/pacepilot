from collections.abc import Callable
from typing import Annotated

from fastapi import Depends

from app.auth import CurrentUser
from app.config import coach_feature_enabled, coach_provider_configured, get_settings
from app.services.coach.agent import CoachAgent
from app.services.coach.provider import OpenRouterCoachProvider

CoachProviderConfiguredDep = Annotated[bool, Depends(coach_provider_configured)]
CoachAgentFactory = Callable[[], CoachAgent]


def get_coach_agent_factory(
    user: CurrentUser, configured: CoachProviderConfiguredDep
) -> CoachAgentFactory | None:
    settings = get_settings()
    api_key = settings.llm_api_key
    model_id = settings.llm_model
    if not configured or not api_key or not model_id:
        return None
    timeout_seconds = settings.llm_timeout_seconds
    workout_proposals_enabled = coach_feature_enabled(
        settings.coach_workout_proposals_enabled, user.id
    )
    daily_adaptation_enabled = coach_feature_enabled(
        settings.coach_daily_adaptation_enabled, user.id
    )

    def create_agent() -> CoachAgent:
        return OpenRouterCoachProvider(
            api_key=api_key,
            model_id=model_id,
            timeout_seconds=timeout_seconds,
            workout_proposals_enabled=workout_proposals_enabled,
            daily_adaptation_enabled=daily_adaptation_enabled,
        )

    return create_agent


CoachAgentFactoryDep = Annotated[CoachAgentFactory | None, Depends(get_coach_agent_factory)]
