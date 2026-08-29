from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from app.services.coach.conversation import CoachHistoryMessage, CoachRuntimeContext


@dataclass(frozen=True)
class CoachEvent:
    type: Literal["answer_text", "artifact_available", "completed", "failed"]
    text: str | None = None
    artifact_type: str | None = None
    failure_category: (
        Literal["provider_error", "missing_final_answer", "tool_call_format"] | None
    ) = None


class CoachProviderError(RuntimeError):
    pass


class CoachAgent(Protocol):
    def stream(
        self,
        messages: Sequence[CoachHistoryMessage],
        runtime: CoachRuntimeContext,
    ) -> AsyncIterator[CoachEvent]: ...
