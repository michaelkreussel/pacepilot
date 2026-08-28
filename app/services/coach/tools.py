import json
from dataclasses import asdict
from datetime import date, datetime, timedelta

from app.models import CoachMessage, User
from app.repositories.coach import find_assistant_message
from app.services.analytics.athlete_data import AthleteDataService
from app.services.analytics.health_trends import HealthMetric
from app.services.coach.conversation import CoachRuntimeContext
from app.services.planning.workout_proposals import (
    RunningProposalRequest,
    RunningProposalService,
    RunningTemplateId,
    WorkoutProposalError,
)
from app.services.planning.workout_service import ProposalOrigin, WorkoutServiceError
from app.services.planning.workout_templates import TemplateExpansionError


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Unsupported value: {type(value).__name__}")


def _json(value: object) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False, separators=(",", ":"))


def get_current_recovery_state(runtime: CoachRuntimeContext) -> str:
    """Get the athlete's current recovery state and readiness components.

    Use this first for questions about today's recovery, fatigue, readiness, or whether the
    athlete should train hard. The result includes source dates and confidence.
    """
    with runtime.session_factory() as session:
        state = AthleteDataService(
            session, runtime.user_id, as_of=runtime.as_of
        ).get_current_recovery_state()
    return _json(asdict(state))


def get_subjective_context(runtime: CoachRuntimeContext) -> str:
    """Get recent effective Garmin/manual activity feedback.

    Use this with recovery data when recent perceived effort should affect today's training. The
    athlete's current subjective condition comes directly from the current chat message.
    """
    with runtime.session_factory() as session:
        subjective = AthleteDataService(
            session, runtime.user_id, as_of=runtime.as_of
        ).get_subjective_context()
    return _json(asdict(subjective))


def get_health_trends(
    runtime: CoachRuntimeContext,
    days: int = 28,
    metrics: tuple[HealthMetric, ...] = (
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
    with runtime.session_factory() as session:
        payload = AthleteDataService(
            session, runtime.user_id, as_of=runtime.as_of
        ).get_health_trends_payload(days, metrics)
    return _json(payload)


def get_training_summary(
    runtime: CoachRuntimeContext,
    days: int = 28,
) -> str:
    """Get a bounded summary of recent training volume, frequency, intensity, and coverage.

    Use this when interpreting recovery in light of recent training or comparing training periods.
    """
    with runtime.session_factory() as session:
        summary = AthleteDataService(
            session, runtime.user_id, as_of=runtime.as_of
        ).get_training_summary(days)
    return _json(asdict(summary))


def get_recent_activities(
    runtime: CoachRuntimeContext,
    limit: int = 5,
) -> str:
    """List recent activities with compact load, heart-rate, distance, and RPE information.

    Use this to identify which recent sessions may explain fatigue or recovery changes.
    """
    with runtime.session_factory() as session:
        workouts = AthleteDataService(
            session, runtime.user_id, as_of=runtime.as_of
        ).get_recent_workouts(limit)
    return _json(tuple(asdict(workout) for workout in workouts))


def get_activity_details(
    runtime: CoachRuntimeContext,
    activity_id: int,
) -> str:
    """Get user-scoped details for one activity returned by the recent-activities tool.

    Use this only when a specific session needs closer analysis.
    """
    with runtime.session_factory() as session:
        details = AthleteDataService(
            session, runtime.user_id, as_of=runtime.as_of
        ).get_activity_details(activity_id)
    return _json(asdict(details) if details is not None else {"status": "not_found"})


def get_health_day(runtime: CoachRuntimeContext, day: date) -> str:
    """Get available sleep, heart, HRV, stress, Body Battery, and movement data for a date.

    Use this for questions about one exact day rather than a trend.
    """
    if day > runtime.as_of or day < runtime.as_of - timedelta(days=365):
        return _json({"status": "outside_supported_range"})
    with runtime.session_factory() as session:
        health = AthleteDataService(session, runtime.user_id, as_of=runtime.as_of).get_health_day(
            day
        )
    return _json(asdict(health) if health is not None else {"status": "not_found", "day": day})


def get_upcoming_workouts(
    runtime: CoachRuntimeContext,
    days: int = 14,
) -> str:
    """Get scheduled workouts in the next bounded number of days.

    Use this only when upcoming training changes the health or recovery interpretation.
    """
    with runtime.session_factory() as session:
        workouts = AthleteDataService(
            session, runtime.user_id, as_of=runtime.as_of
        ).get_upcoming_workouts(days)
    return _json(tuple(asdict(workout) for workout in workouts))


def create_running_workout_proposal(
    runtime: CoachRuntimeContext,
    suggested_for: date,
    available_minutes: int,
    template_id: RunningTemplateId = "easy_run",
) -> str:
    """Create one unaccepted running workout through PacePilot's deterministic planner.

    Use this only when the athlete explicitly wants a running-workout proposal and has supplied a
    desired date plus available time. Select the workout type that best matches the stated goal;
    if the athlete did not request a type, use easy_run. The result remains unscheduled and
    unaccepted. This tool cannot accept, schedule, upload, push, or synchronize a workout.
    """
    context = runtime
    if (
        context.conversation_id is None
        or context.user_message_id is None
        or context.assistant_message_id is None
    ):
        raise ValueError("Coach proposal runtime is incomplete")
    conversation_id = context.conversation_id
    user_message_id = context.user_message_id
    assistant_message_id = context.assistant_message_id
    with context.session_factory() as session:
        user_message = session.get(CoachMessage, user_message_id)
        assistant_message = find_assistant_message(
            session, context.user_id, conversation_id, assistant_message_id
        )
        user = session.get(User, context.user_id)
        if (
            user is None
            or user_message is None
            or user_message.conversation_id != conversation_id
            or user_message.role != "user"
            or assistant_message is None
        ):
            raise ValueError("Coach proposal runtime is invalid")
        request = RunningProposalRequest(
            template_id=template_id,
            suggested_for=suggested_for,
            available_minutes=available_minutes,
            # SQLite rowids can be reused after conversation deletion, so the timestamp
            # keeps retries scoped to this exact durable assistant execution.
            idempotency_key=(
                f"coach-message:{assistant_message_id}:{assistant_message.created_at.isoformat()}:"
                "create_running_workout_proposal:v2"
            ),
        )
        try:
            RunningProposalService(
                session,
                user,
                as_of=context.as_of,
                request_id=assistant_message.request_id,
            ).create(
                request,
                origin=ProposalOrigin(
                    conversation_id=conversation_id,
                    user_message_id=user_message_id,
                    assistant_message_id=assistant_message_id,
                    model_provider="openrouter",
                    model_id=assistant_message.model_id,
                    prompt_template_version=assistant_message.prompt_template_version,
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
