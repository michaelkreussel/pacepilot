from typing import Annotated

from fastapi import Depends

from app.config import get_settings
from app.services.training_agent.backend import TrainingAgent


def get_training_agent() -> TrainingAgent | None:
    settings = get_settings()
    if not settings.llm_api_key or not settings.llm_model:
        return None

    from app.services.training_agent.agno_backend import AgnoTrainingAgent

    return AgnoTrainingAgent(
        api_key=settings.llm_api_key,
        model_id=settings.llm_model,
        base_url=settings.llm_base_url,
    )


TrainingAgentDep = Annotated[TrainingAgent | None, Depends(get_training_agent)]
