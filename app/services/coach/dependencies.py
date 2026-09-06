from collections.abc import Callable
from typing import Annotated

from fastapi import Depends

from app.config import coach_provider_configured, get_settings
from app.services.coach.agent import CoachAgent
from app.services.coach.provider import OpenRouterCoachProvider

CoachProviderConfiguredDep = Annotated[bool, Depends(coach_provider_configured)]
CoachAgentFactory = Callable[[], CoachAgent]


def get_coach_agent_factory(configured: CoachProviderConfiguredDep) -> CoachAgentFactory | None:
    settings = get_settings()
    api_key = settings.llm_api_key
    model_id = settings.llm_model
    if not configured or not api_key or not model_id:
        return None
    timeout_seconds = settings.llm_timeout_seconds

    def create_agent() -> CoachAgent:
        return OpenRouterCoachProvider(
            api_key=api_key,
            model_id=model_id,
            timeout_seconds=timeout_seconds,
        )

    return create_agent


CoachAgentFactoryDep = Annotated[CoachAgentFactory | None, Depends(get_coach_agent_factory)]
