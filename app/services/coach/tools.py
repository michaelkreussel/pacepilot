import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool
from pydantic import Field
from sqlalchemy.orm import Session, sessionmaker

from app.models import CoachMessage, User
from app.repositories.coach import find_assistant_run
from app.services.analytics.athlete_data import AthleteDataService
from app.services.analytics.health_trends import MetricTrend
from app.services.planning.workout_proposals import (
    EasyRunProposalRequest,
    RunningProposalService,
    WorkoutProposalError,
)
from app.services.planning.workout_service import ProposalOrigin, WorkoutServiceError
from app.services.planning.workout_templates import TemplateExpansionError

HealthMetric = Literal[
    "resting_hr",
    "hrv",
    "sleep_duration",
    "sleep_need",
    "sleep_score",
    "stress",
    "body_battery_high",
    "body_battery_charged",
    "garmin_training_readiness",
    "recovery_time",
    "vo2max",
    "training_load",
    "acute_load",
    "chronic_load",
]


@dataclass(frozen=True)
class CoachRuntimeContext:
    user_id: int
    as_of: date
    session_factory: sessionmaker[Session]
    request_id: str | None = None
    conversation_id: int | None = None
    user_message_id: int | None = None
    assistant_message_id: int | None = None
    assistant_run_id: int | None = None


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Unsupported value: {type(value).__name__}")


def _json(value: object) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False, separators=(",", ":"))


def _trend_payload(trend: MetricTrend) -> dict[str, object]:
    payload = asdict(trend)
    baseline = payload.get("personal_baseline")
    difference = payload.get("difference_from_baseline")
    payload["difference_from_baseline_percent"] = (
        round(float(difference) / float(baseline) * 100, 1)
        if isinstance(baseline, (int, float))
        and baseline != 0
        and isinstance(difference, (int, float))
        else None
    )
    payload["points"] = payload["points"][-31:]  # type: ignore[index]
    return payload


@tool
def get_current_recovery_state(runtime: ToolRuntime[CoachRuntimeContext]) -> str:
    """Get the athlete's current recovery state and readiness components.

    Use this first for questions about today's recovery, fatigue, readiness, or whether the
    athlete should train hard. The result includes source dates and confidence.
    """
    with runtime.context.session_factory() as session:
        state = AthleteDataService(
            session, runtime.context.user_id, as_of=runtime.context.as_of
        ).get_current_recovery_state()
    return _json(asdict(state))


@tool
def get_subjective_context(runtime: ToolRuntime[CoachRuntimeContext]) -> str:
    """Get recent effective Garmin/manual activity feedback.

    Use this with recovery data when recent perceived effort should affect today's training. The
    athlete's current subjective condition comes directly from the current chat message.
    """
    with runtime.context.session_factory() as session:
        subjective = AthleteDataService(
            session, runtime.context.user_id, as_of=runtime.context.as_of
        ).get_subjective_context()
    return _json(asdict(subjective))


@tool
def get_health_trends(
    runtime: ToolRuntime[CoachRuntimeContext],
    days: Annotated[int, Field(ge=7, le=365)] = 28,
    metrics: Annotated[tuple[HealthMetric, ...], Field(min_length=1, max_length=6)] = (
        "hrv",
        "resting_hr",
        "sleep_duration",
        "stress",
    ),
) -> str:
    """Compare selected health metrics with recent averages and the personal baseline.

    Use this for changes and relationships involving HRV, resting heart rate, sleep, stress,
    Body Battery, readiness, recovery time, VO2max, or training load.
    """
    with runtime.context.session_factory() as session:
        trends = AthleteDataService(
            session, runtime.context.user_id, as_of=runtime.context.as_of
        ).get_health_trends(days)
    return _json(
        {
            "start": trends.start,
            "end": trends.end,
            "metrics": {name: _trend_payload(getattr(trends, name)) for name in metrics},
            "coverage": tuple(asdict(item) for item in trends.coverage),
        }
    )


@tool
def get_training_summary(
    runtime: ToolRuntime[CoachRuntimeContext],
    days: Annotated[int, Field(ge=7, le=365)] = 28,
) -> str:
    """Get a bounded summary of recent training volume, frequency, intensity, and coverage.

    Use this when interpreting recovery in light of recent training or comparing training periods.
    """
    with runtime.context.session_factory() as session:
        summary = AthleteDataService(
            session, runtime.context.user_id, as_of=runtime.context.as_of
        ).get_training_summary(days)
    return _json(asdict(summary))


@tool
def get_recent_activities(
    runtime: ToolRuntime[CoachRuntimeContext],
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> str:
    """List recent activities with compact load, heart-rate, distance, and RPE information.

    Use this to identify which recent sessions may explain fatigue or recovery changes.
    """
    with runtime.context.session_factory() as session:
        workouts = AthleteDataService(
            session, runtime.context.user_id, as_of=runtime.context.as_of
        ).get_recent_workouts(limit)
    return _json(tuple(asdict(workout) for workout in workouts))


@tool
def get_activity_details(
    runtime: ToolRuntime[CoachRuntimeContext],
    activity_id: Annotated[int, Field(gt=0)],
) -> str:
    """Get user-scoped details for one activity returned by the recent-activities tool.

    Use this only when a specific session needs closer analysis.
    """
    with runtime.context.session_factory() as session:
        details = AthleteDataService(
            session, runtime.context.user_id, as_of=runtime.context.as_of
        ).get_activity_details(activity_id)
    return _json(asdict(details) if details is not None else {"status": "not_found"})


@tool
def get_health_day(runtime: ToolRuntime[CoachRuntimeContext], day: date) -> str:
    """Get available sleep, heart, HRV, stress, Body Battery, and movement data for a date.

    Use this for questions about one exact day rather than a trend.
    """
    if day > runtime.context.as_of or day < runtime.context.as_of - timedelta(days=365):
        return _json({"status": "outside_supported_range"})
    with runtime.context.session_factory() as session:
        health = AthleteDataService(
            session, runtime.context.user_id, as_of=runtime.context.as_of
        ).get_health_day(day)
    return _json(asdict(health) if health is not None else {"status": "not_found", "day": day})


@tool
def get_upcoming_workouts(
    runtime: ToolRuntime[CoachRuntimeContext],
    days: Annotated[int, Field(ge=1, le=42)] = 14,
) -> str:
    """Get scheduled workouts in the next bounded number of days.

    Use this only when upcoming training changes the health or recovery interpretation.
    """
    with runtime.context.session_factory() as session:
        workouts = AthleteDataService(
            session, runtime.context.user_id, as_of=runtime.context.as_of
        ).get_upcoming_workouts(days)
    return _json(tuple(asdict(workout) for workout in workouts))


@tool
def create_running_workout_proposal(
    runtime: ToolRuntime[CoachRuntimeContext],
    suggested_for: date,
    available_minutes: Annotated[int, Field(ge=20, le=1440)],
) -> str:
    """Create one unaccepted Easy Run proposal through PacePilot's deterministic planner.

    Use this only when the athlete explicitly wants a running-workout proposal and has supplied a
    desired date plus available time. The result remains unscheduled and unaccepted. This tool
    cannot accept, schedule, upload, push, or otherwise synchronize a workout.
    """
    context = runtime.context
    if (
        context.conversation_id is None
        or context.user_message_id is None
        or context.assistant_message_id is None
        or context.assistant_run_id is None
    ):
        raise ValueError("Coach proposal runtime is incomplete")
    conversation_id = context.conversation_id
    user_message_id = context.user_message_id
    assistant_message_id = context.assistant_message_id
    assistant_run_id = context.assistant_run_id
    with context.session_factory() as session:
        run = find_assistant_run(session, context.user_id, conversation_id, assistant_run_id)
        user_message = session.get(CoachMessage, user_message_id)
        assistant_message = session.get(CoachMessage, assistant_message_id)
        user = session.get(User, context.user_id)
        if (
            run is None
            or user is None
            or run.user_message_id != user_message_id
            or run.assistant_message_id != assistant_message_id
            or user_message is None
            or user_message.conversation_id != conversation_id
            or user_message.role != "user"
            or assistant_message is None
            or assistant_message.conversation_id != conversation_id
            or assistant_message.role != "assistant"
        ):
            raise ValueError("Coach proposal runtime is invalid")
        request = EasyRunProposalRequest(
            suggested_for=suggested_for,
            available_minutes=available_minutes,
            # SQLite rowids are reused after conversation deletes cascade old runs away,
            # so the run id alone is not unique. The creation timestamp disambiguates
            # recycled ids and keeps retries within one run on the same key.
            idempotency_key=(
                f"coach-run:{assistant_run_id}:{run.created_at.isoformat()}:"
                "create_running_workout_proposal:v1"
            ),
        )
        try:
            RunningProposalService(session, user, request_id=context.request_id).create_easy_run(
                request,
                origin=ProposalOrigin(
                    conversation_id=conversation_id,
                    user_message_id=user_message_id,
                    assistant_message_id=assistant_message_id,
                    assistant_run_id=assistant_run_id,
                ),
            )
        except (WorkoutProposalError, TemplateExpansionError, WorkoutServiceError) as exc:
            return _json(
                {
                    "status": "not_created",
                    "error": {"code": exc.code, "message": str(exc)},
                }
            )
        return _json(
            {
                "status": "created",
                "artifact": {"type": "workout_proposal"},
            }
        )


COACH_TOOLS = (
    get_current_recovery_state,
    get_subjective_context,
    get_health_trends,
    get_training_summary,
    get_recent_activities,
    get_activity_details,
    get_health_day,
    get_upcoming_workouts,
)


def coach_tools(*, workout_proposals_enabled: bool) -> tuple[BaseTool, ...]:
    if workout_proposals_enabled:
        return (*COACH_TOOLS, create_running_workout_proposal)
    return COACH_TOOLS


TOOL_LABELS = {
    "get_current_recovery_state": "Aktuelle Erholung prüfen",
    "get_subjective_context": "Subjektives Aktivitätsfeedback laden",
    "get_health_trends": "Gesundheitstrends vergleichen",
    "get_training_summary": "Trainingsbelastung auswerten",
    "get_recent_activities": "Letzte Trainingseinheiten ansehen",
    "get_activity_details": "Trainingseinheit genauer analysieren",
    "get_health_day": "Gesundheitsdaten des Tages laden",
    "get_upcoming_workouts": "Geplante Einheiten prüfen",
    "create_running_workout_proposal": "Easy-Run-Vorschlag erstellen",
}


def describe_tool_call(tool_name: str, arguments: dict[str, object]) -> tuple[str, str | None]:
    label = TOOL_LABELS.get(tool_name, "Trainingsdaten prüfen")
    if "days" in arguments:
        return label, f"Zeitraum: {arguments['days']} Tage"
    if tool_name == "get_health_day" and "day" in arguments:
        return label, f"Datum: {arguments['day']}"
    if tool_name == "get_recent_activities" and "limit" in arguments:
        return label, f"Letzte {arguments['limit']} Einheiten"
    if tool_name == "create_running_workout_proposal":
        day = arguments.get("suggested_for")
        minutes = arguments.get("available_minutes")
        return label, f"{day} · {minutes} Minuten"
    return label, None
