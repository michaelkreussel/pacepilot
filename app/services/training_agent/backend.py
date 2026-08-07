import json
from dataclasses import asdict, dataclass
from typing import Protocol

SnapshotValue = str | int | float | None
SnapshotRow = dict[str, SnapshotValue]


@dataclass(frozen=True)
class TrainingSnapshot:
    as_of: str
    weekly_load: SnapshotRow
    recent_activities: tuple[SnapshotRow, ...]
    recent_health: tuple[SnapshotRow, ...]
    upcoming_workouts: tuple[SnapshotRow, ...]

    def as_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


class TrainingAgentError(RuntimeError):
    """Raised when the configured agent backend cannot produce a response."""


class TrainingAgent(Protocol):
    async def respond(self, message: str, snapshot: TrainingSnapshot) -> str: ...
