from typing import Annotated

from fastapi import Depends

from app.config import get_settings
from app.services.coach.agent import CoachAgent, LangChainCoachAgent


def get_coach_agent() -> CoachAgent | None:
    settings = get_settings()
    if not settings.llm_api_key or not settings.llm_model:
        return None
    return LangChainCoachAgent(
        api_key=settings.llm_api_key,
        model_id=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )


CoachAgentDep = Annotated[CoachAgent | None, Depends(get_coach_agent)]
