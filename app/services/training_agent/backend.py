import json
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ConversationTurn:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class CoachEvent:
    type: str
    content: str = ""
    run_id: str | None = None
    tool: str | None = None
    label: str | None = None
    arguments: dict[str, object] | None = None
    phase: str | None = None
    elapsed_seconds: int | None = None
    replace: bool = False
    done: bool = False

    def as_json_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":")) + "\n"


class CoachCapabilities(Protocol):
    def agent_tools(self) -> tuple[Callable[..., str], ...]: ...


class TrainingAgentError(RuntimeError):
    """Raised when the configured agent backend cannot produce a response."""


class TrainingAgent(Protocol):
    def stream(
        self,
        message: str,
        capabilities: CoachCapabilities,
        history: tuple[ConversationTurn, ...] = (),
    ) -> AsyncIterator[CoachEvent]: ...
