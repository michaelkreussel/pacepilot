import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from time import monotonic

from app.services.training_agent.backend import (
    CoachCapabilities,
    CoachEvent,
    ConversationTurn,
    TrainingAgent,
)


def _waiting_message(phase: str, label: str | None) -> str:
    if phase == "retrieving_data":
        capability = label or "Die Datenabfrage"
        return f"{capability} läuft noch. Größere Zeiträume benötigen etwas mehr Auswertung."
    if phase == "evaluating_data":
        return "Die Daten sind da. Der Coach prüft gerade, ob noch ein Vergleich fehlt."
    if phase == "composing_answer":
        return "Die Zusammenhänge sind geprüft. Der Coach formuliert die belegte Einordnung."
    if phase == "streaming_answer":
        return "Die Antwort wird weiter übertragen."
    return "Der Coach wählt gerade die kleinste passende Datenmenge für deine Frage aus."


class CoachOrchestrator:
    """Adds user-facing execution state around the provider's event stream."""

    def __init__(self, agent: TrainingAgent, *, heartbeat_seconds: float = 3.0) -> None:
        self._agent = agent
        self._heartbeat_seconds = heartbeat_seconds

    async def stream(
        self,
        message: str,
        capabilities: CoachCapabilities,
        history: tuple[ConversationTurn, ...] = (),
    ) -> AsyncIterator[CoachEvent]:
        started_at = monotonic()
        phase = "selecting_data"
        label: str | None = None
        events = self._agent.stream(message, capabilities, history).__aiter__()
        pending = asyncio.ensure_future(anext(events))
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=self._heartbeat_seconds)
                if not done:
                    yield CoachEvent(
                        "waiting",
                        _waiting_message(phase, label),
                        label=label,
                        phase=phase,
                        elapsed_seconds=int(monotonic() - started_at),
                    )
                    continue
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    break
                if event.type == "tool_started":
                    phase = "retrieving_data"
                    label = event.label
                elif event.type in {"tool_result_summary", "tool_error"}:
                    phase = "evaluating_data"
                    label = event.label
                elif event.type == "analysis_update":
                    phase = "composing_answer"
                    label = None
                elif event.type == "final_response":
                    phase = "streaming_answer"
                    label = None
                yield event
                pending = asyncio.ensure_future(anext(events))
        finally:
            if not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
