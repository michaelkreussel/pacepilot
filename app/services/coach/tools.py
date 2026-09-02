import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CoachMessage,
    PostSessionFeedback,
    PreSessionFeedback,
    User,
    Workout,
    WorkoutRevision,
)
from app.repositories.coach import find_assistant_message
from app.services.analytics.athlete_data import AdaptiveContextFocus, AthleteDataService
from app.services.analytics.health_trends import HealthMetric
from app.services.analytics.progress import ProgressReferenceError
from app.services.coach.conversation import CoachRuntimeContext
from app.services.planning.feedback_service import FeedbackCommands, FeedbackNotFoundError
from app.services.planning.planning_commands import (
    AnchorKind,
    AvailabilityInput,
    GoalCreateInput,
    GoalEventType,
    GoalUpdateInput,
    PerformanceAnchorCreateInput,
    PerformanceAnchorUpdateInput,
    PlanningInputCommandError,
    PlanningInputCommands,
    PlanningProfileUpdateInput,
    ReferencedGoalChangeConfirmation,
)
from app.services.planning.planning_queries import (
    AvailabilityFact,
    GoalFact,
    PerformanceAnchorFact,
    PlanningProfileFact,
)
from app.services.planning.planning_queries import (
    get_planning_inputs as planning_inputs,
)
from app.services.planning.safety_triage import (
    IllnessSignal,
    PainInput,
    PostSessionFeedbackInput,
    PreSessionFeedbackInput,
)
from app.services.planning.workout_proposals import (
    RunningProposalRequest,
    RunningProposalService,
    RunningRevisionRequest,
    RunningTemplateId,
    WorkoutProposalError,
)
from app.services.planning.workout_service import (
    ProposalOrigin,
    WorkoutService,
    WorkoutServiceError,
)
from app.services.planning.workout_templates import TemplateExpansionError


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Unsupported value: {type(value).__name__}")


def _json(value: object) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False, separators=(",", ":"))


WEEKDAY_LABELS = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)
PlanningArtifactFact = GoalFact | PlanningProfileFact | AvailabilityFact | PerformanceAnchorFact
FeedbackRecord = PreSessionFeedback | PostSessionFeedback
RevisionEditScope = Literal["supported_parameters", "unsupported"]


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


def get_adaptive_context(
    runtime: CoachRuntimeContext,
    focus: AdaptiveContextFocus,
    days: int = 28,
    goal_id: int | None = None,
) -> str:
    """Select fresh, bounded durable context for one material coaching focus."""
    try:
        with runtime.session_factory() as session:
            context = AthleteDataService(
                session, runtime.user_id, as_of=runtime.as_of
            ).get_adaptive_context(focus=focus, days=days, goal_id=goal_id)
    except ProgressReferenceError:
        return _json({"status": "not_found"})
    return _json(context)


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


def get_progress(
    runtime: CoachRuntimeContext,
    days: int = 28,
    goal_id: int | None = None,
) -> str:
    """Compare accepted planned work with observed activities and feedback.

    Use this for progress, plan adherence, goal trajectory, interruptions, or evidence gaps. The
    result reports synchronization and linkage uncertainty instead of inventing missing progress.
    """
    try:
        with runtime.session_factory() as session:
            progress = AthleteDataService(
                session, runtime.user_id, as_of=runtime.as_of
            ).get_progress(days=days, goal_id=goal_id)
    except ProgressReferenceError:
        return _json({"status": "not_found"})
    return _json(asdict(progress))


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


def get_revisable_running_workouts(runtime: CoachRuntimeContext, limit: int = 10) -> str:
    """List user-owned deterministic workouts with their exact current revision identity."""
    bounded_limit = min(max(limit, 1), 20)
    with runtime.session_factory() as session:
        rows = session.execute(
            select(Workout, WorkoutRevision)
            .join(
                WorkoutRevision,
                WorkoutRevision.id == Workout.current_revision_id,
            )
            .where(
                Workout.user_id == runtime.user_id,
                Workout.deleted_at.is_(None),
                Workout.source_type == "coach_single",
                Workout.approval_status != "rejected",
            )
            .order_by(Workout.updated_at.desc(), Workout.id.desc())
            .limit(bounded_limit)
        ).all()
    return _json(
        [
            {
                "workout_id": workout.id,
                "revision_id": revision.id,
                "revision_number": revision.revision_number,
                "accepted_revision_id": workout.accepted_revision_id,
                "name": revision.name,
                "suggested_for": revision.suggested_for,
                "template_id": revision.template_id,
                "available_minutes": (
                    (revision.generation_context_json or {})
                    .get("request", {})
                    .get("available_minutes")
                    if isinstance((revision.generation_context_json or {}).get("request"), dict)
                    else None
                ),
            }
            for workout, revision in rows
        ]
    )


def get_planning_inputs(runtime: CoachRuntimeContext) -> str:
    """Read active goals, planning profile, availability, and performance anchors."""
    with runtime.session_factory() as session:
        result = planning_inputs(session, runtime.user_id, as_of=runtime.as_of)
    return _json(asdict(result))


def _feedback_result(feedback: FeedbackRecord) -> dict[str, object]:
    result: dict[str, object] = {
        "feedback_id": feedback.id,
        "workout_id": feedback.workout_id,
        "pain": {
            "present": feedback.pain_present,
            "location": feedback.pain_location,
            "severity": feedback.pain_severity,
            "alters_gait": feedback.pain_alters_gait,
            "worsens_with_activity": feedback.pain_worsens_with_activity,
        },
        "notes": feedback.notes,
        "recorded_at": feedback.recorded_at.isoformat(),
    }
    if isinstance(feedback, PreSessionFeedback):
        result.update(
            illness_signal=feedback.illness_signal,
            available_minutes=feedback.available_minutes,
        )
    else:
        result.update(
            activity_id=feedback.activity_id,
            completion_percent=feedback.completion_percent,
            session_rpe=feedback.session_rpe,
            overall_feel=feedback.overall_feel,
            stopped_reason=feedback.stopped_reason,
        )
    return result


def _run_feedback_mutation(
    runtime: CoachRuntimeContext,
    *,
    operation: str,
    resource: str,
    request: dict[str, object],
    not_found_code: str,
    command: Callable[[FeedbackCommands], FeedbackRecord],
) -> str:
    if (
        runtime.conversation_id is None
        or runtime.user_message_id is None
        or runtime.assistant_message_id is None
    ):
        raise ValueError("Coach feedback runtime is incomplete")
    with runtime.session_factory() as session:
        user_message = session.get(CoachMessage, runtime.user_message_id)
        assistant_message = find_assistant_message(
            session,
            runtime.user_id,
            runtime.conversation_id,
            runtime.assistant_message_id,
        )
        user = session.get(User, runtime.user_id)
        if (
            user is None
            or user_message is None
            or user_message.conversation_id != runtime.conversation_id
            or user_message.role != "user"
            or assistant_message is None
        ):
            raise ValueError("Coach feedback runtime is invalid")
        for artifact in assistant_message.artifacts_json:
            if artifact.get("operation") == operation and artifact.get("request") == request:
                return _json(
                    {
                        "status": "recorded",
                        "artifact": {"type": "feedback", "resource": resource},
                    }
                )
        try:
            feedback = command(FeedbackCommands(session, user))
            session.flush()
            artifact: dict[str, object] = {
                "type": "feedback",
                "resource": resource,
                "operation": operation,
                "request": request,
                "result": _feedback_result(feedback),
            }
            assistant_message.artifacts_json = [
                *assistant_message.artifacts_json,
                artifact,
            ]
            session.commit()
        except FeedbackNotFoundError as exc:
            session.rollback()
            return _json(
                {
                    "status": "not_recorded",
                    "error": {"code": not_found_code, "message": str(exc)},
                }
            )
        except Exception:
            session.rollback()
            raise
    return _json(
        {
            "status": "recorded",
            "artifact": {"type": "feedback", "resource": resource},
        }
    )


def _pain_clarification(pain: PainInput | None) -> str | None:
    if pain is None or not pain.present or pain.alters_gait is True:
        return None
    if not pain.location or pain.severity is None or pain.alters_gait is None:
        return (
            "Wo hast du Schmerzen, wie stark sind sie von 0 bis 10 und verändern sie dein Gangbild?"
        )
    return None


def record_pre_session_feedback(
    runtime: CoachRuntimeContext,
    *,
    workout_id: int,
    pain: PainInput | None = None,
    illness_signal: IllnessSignal | None = None,
    available_minutes: int | None = None,
    notes: str | None = None,
) -> str:
    """Record explicit feedback for one exact user-owned workout."""
    if question := _pain_clarification(pain):
        return _json({"status": "needs_clarification", "question": question})
    if illness_signal == IllnessSignal.UNKNOWN:
        return _json(
            {
                "status": "needs_clarification",
                "question": "Welche konkreten Krankheitszeichen hast du?",
            }
        )
    if pain is None and illness_signal is None and available_minutes is None and not notes:
        return _json(
            {
                "status": "needs_clarification",
                "question": "Welche konkrete Rückmeldung soll ich vor dem Training speichern?",
            }
        )
    try:
        data = PreSessionFeedbackInput(
            pain=pain or PainInput(),
            illness_signal=illness_signal or IllnessSignal.NONE,
            available_minutes=available_minutes,
            notes=notes,
        )
    except ValidationError:
        return _json({"status": "not_recorded", "error": {"code": "feedback.invalid_input"}})
    request: dict[str, object] = {"workout_id": workout_id}
    if pain is not None:
        request["pain"] = pain.model_dump(mode="json")
    if illness_signal is not None:
        request["illness_signal"] = illness_signal.value
    if available_minutes is not None:
        request["available_minutes"] = available_minutes
    if data.notes:
        request["notes"] = data.notes
    return _run_feedback_mutation(
        runtime,
        operation="record_pre_session_feedback",
        resource="pre_session",
        request=request,
        not_found_code="feedback.workout_not_found",
        command=lambda commands: commands.record_pre_session(workout_id, data),
    )


def record_post_session_feedback(
    runtime: CoachRuntimeContext,
    *,
    activity_id: int,
    completion_percent: int | None = None,
    session_rpe: int | None = None,
    overall_feel: int | None = None,
    pain: PainInput | None = None,
    stopped_reason: str | None = None,
    notes: str | None = None,
) -> str:
    """Record explicit feedback for one exact user-owned activity."""
    if question := _pain_clarification(pain):
        return _json({"status": "needs_clarification", "question": question})
    if all(
        value is None
        for value in (
            completion_percent,
            session_rpe,
            overall_feel,
            pain,
            stopped_reason,
            notes,
        )
    ):
        return _json(
            {
                "status": "needs_clarification",
                "question": "Welche konkrete Rückmeldung soll ich zur Aktivität speichern?",
            }
        )
    try:
        data = PostSessionFeedbackInput(
            completion_percent=completion_percent,
            session_rpe=session_rpe,
            overall_feel=overall_feel,
            pain=pain or PainInput(),
            stopped_reason=stopped_reason,
            notes=notes,
        )
    except ValidationError:
        return _json({"status": "not_recorded", "error": {"code": "feedback.invalid_input"}})
    request: dict[str, object] = {"activity_id": activity_id}
    for key, value in (
        ("completion_percent", data.completion_percent),
        ("session_rpe", data.session_rpe),
        ("overall_feel", data.overall_feel),
        ("stopped_reason", data.stopped_reason),
        ("notes", data.notes),
    ):
        if value is not None:
            request[key] = value
    if pain is not None:
        request["pain"] = pain.model_dump(mode="json")
    return _run_feedback_mutation(
        runtime,
        operation="record_post_session_feedback",
        resource="post_session",
        request=request,
        not_found_code="feedback.activity_not_found",
        command=lambda commands: commands.record_post_session(activity_id, data),
    )


def _run_planning_mutation(
    runtime: CoachRuntimeContext,
    *,
    operation: str,
    resource: str,
    request: dict[str, object],
    command: Callable[[PlanningInputCommands], PlanningArtifactFact],
    clarifications: dict[str, str] | None = None,
    pending_confirmation: Callable[
        [PlanningInputCommands], tuple[ReferencedGoalChangeConfirmation, dict[str, object]]
    ]
    | None = None,
) -> str:
    if (
        runtime.conversation_id is None
        or runtime.user_message_id is None
        or runtime.assistant_message_id is None
    ):
        raise ValueError("Coach planning runtime is incomplete")
    with runtime.session_factory() as session:
        user_message = session.get(CoachMessage, runtime.user_message_id)
        assistant_message = find_assistant_message(
            session,
            runtime.user_id,
            runtime.conversation_id,
            runtime.assistant_message_id,
        )
        user = session.get(User, runtime.user_id)
        if (
            user is None
            or user_message is None
            or user_message.conversation_id != runtime.conversation_id
            or user_message.role != "user"
            or assistant_message is None
        ):
            raise ValueError("Coach planning runtime is invalid")
        # A retried tool call with the same bounded request is a duplicate and
        # reports the stored outcome; a different request for the same operation
        # is a legitimate new change and supersedes only stale pending
        # confirmations.
        for existing_artifact in assistant_message.artifacts_json:
            if (
                existing_artifact.get("operation") == operation
                and existing_artifact.get("request") == request
            ):
                status = (
                    "confirmation_required"
                    if existing_artifact.get("status") == "confirmation_required"
                    else "updated"
                )
                return _json(
                    {
                        "status": status,
                        "artifact": {"type": "planning_input", "resource": resource},
                    }
                )
        kept_artifacts = [
            artifact
            for artifact in assistant_message.artifacts_json
            if not (
                artifact.get("operation") == operation
                and artifact.get("status") == "confirmation_required"
            )
        ]
        commands = PlanningInputCommands(
            session,
            user,
            as_of=runtime.as_of,
            commit=False,
        )
        try:
            fact = command(commands)
        except PlanningInputCommandError as exc:
            if exc.code == "planning.goal_confirmation_required" and pending_confirmation:
                confirmation, _ = pending_confirmation(commands)
                assistant_message.artifacts_json = [
                    *kept_artifacts,
                    {
                        "type": "planning_input",
                        "resource": resource,
                        "operation": operation,
                        "status": "confirmation_required",
                        "request": request,
                        "confirmation": confirmation.model_dump(mode="json"),
                    },
                ]
                session.commit()
                return _json(
                    {
                        "status": "confirmation_required",
                        "artifact": {"type": "planning_input", "resource": resource},
                    }
                )
            session.rollback()
            if clarifications and exc.code in clarifications:
                return _json(
                    {
                        "status": "needs_clarification",
                        "question": clarifications[exc.code],
                    }
                )
            return _json(
                {
                    "status": "not_updated",
                    "error": {"code": exc.code, "message": str(exc)},
                }
            )
        result = json.loads(_json(asdict(fact)))
        assistant_message.artifacts_json = [
            *kept_artifacts,
            {
                "type": "planning_input",
                "resource": resource,
                "operation": operation,
                "request": request,
                "result": result,
            },
        ]
        try:
            session.commit()
        except Exception:
            session.rollback()
            raise
    return _json(
        {
            "status": "updated",
            "artifact": {"type": "planning_input", "resource": resource},
        }
    )


def create_planning_goal(
    runtime: CoachRuntimeContext,
    event_type: GoalEventType,
    event_name: str | None = None,
    target_date: date | None = None,
) -> str:
    """Create one running goal; ask for a material missing target date before storage."""
    if event_type != "general_fitness" and target_date is None:
        return _json(
            {
                "status": "needs_clarification",
                "question": "Für welches Datum soll dieses Ziel gelten?",
            }
        )
    try:
        data = GoalCreateInput(
            event_type=event_type,
            event_name=event_name,
            target_date=target_date,
        )
    except ValidationError:
        return _json({"status": "not_updated", "error": {"code": "planning.invalid_input"}})
    return _run_planning_mutation(
        runtime,
        operation="create_planning_goal",
        resource="goal",
        request={
            "event_type": event_type,
            "event_name": event_name,
            "target_date": target_date.isoformat() if target_date is not None else None,
        },
        command=lambda commands: commands.create_goal(data),
        clarifications={
            "planning.goal_target_date_required": "Für welches Datum soll dieses Ziel gelten?"
        },
    )


def update_planning_goal(
    runtime: CoachRuntimeContext,
    goal_id: int,
    changes: GoalUpdateInput,
) -> str:
    """Update one user-owned goal without accepting referenced-cycle changes in prose."""
    request: dict[str, object] = {
        "goal_id": goal_id,
        "changes": changes.model_dump(exclude_unset=True, mode="json"),
    }
    return _run_planning_mutation(
        runtime,
        operation="update_planning_goal",
        resource="goal",
        request=request,
        command=lambda commands: commands.update_goal(goal_id, changes),
        clarifications={
            "planning.goal_target_date_required": "Für welches Datum soll dieses Ziel gelten?"
        },
        pending_confirmation=lambda commands: (
            _required_goal_confirmation(commands, goal_id, operation="update"),
            request,
        ),
    )


def deactivate_planning_goal(runtime: CoachRuntimeContext, goal_id: int) -> str:
    """Deactivate one user-owned goal unless an accepted cycle requires exact confirmation."""
    return _run_planning_mutation(
        runtime,
        operation="deactivate_planning_goal",
        resource="goal",
        request={"goal_id": goal_id},
        command=lambda commands: commands.deactivate_goal(goal_id),
        pending_confirmation=lambda commands: (
            _required_goal_confirmation(commands, goal_id, operation="deactivate"),
            {"goal_id": goal_id},
        ),
    )


def _required_goal_confirmation(
    commands: PlanningInputCommands,
    goal_id: int,
    *,
    operation: str,
) -> ReferencedGoalChangeConfirmation:
    if operation not in {"update", "deactivate"}:
        raise ValueError("Unsupported goal confirmation operation")
    confirmation = commands.referenced_goal_change_confirmation(
        goal_id,
        operation="update" if operation == "update" else "deactivate",
    )
    if confirmation is None:
        raise RuntimeError("Referenced goal confirmation is missing")
    return confirmation


def update_planning_profile(
    runtime: CoachRuntimeContext,
    changes: PlanningProfileUpdateInput,
) -> str:
    """Update the user-owned planning profile."""
    return _run_planning_mutation(
        runtime,
        operation="update_planning_profile",
        resource="profile",
        request={"changes": changes.model_dump(exclude_unset=True, mode="json")},
        command=lambda commands: commands.update_profile(changes),
    )


def set_planning_availability(
    runtime: CoachRuntimeContext,
    weekday: int,
    available: bool,
    available_minutes: int | None = None,
) -> str:
    """Set one recurring training day through the planning command boundary."""
    if available and available_minutes is None:
        return _json(
            {
                "status": "needs_clarification",
                "question": f"Wie viele Minuten kannst du am {WEEKDAY_LABELS[weekday]} trainieren?",
            }
        )
    try:
        data = AvailabilityInput(
            weekday=weekday,
            available=available,
            available_minutes=available_minutes,
        )
    except ValidationError:
        return _json({"status": "not_updated", "error": {"code": "planning.invalid_input"}})
    return _run_planning_mutation(
        runtime,
        operation="set_planning_availability",
        resource="availability",
        request={
            "weekday": weekday,
            "available": available,
            "available_minutes": available_minutes,
        },
        command=lambda commands: commands.set_availability(data),
    )


def deactivate_planning_availability(runtime: CoachRuntimeContext, weekday: int) -> str:
    """Mark one existing recurring training day unavailable."""
    return _run_planning_mutation(
        runtime,
        operation="deactivate_planning_availability",
        resource="availability",
        request={"weekday": weekday},
        command=lambda commands: commands.deactivate_availability(weekday=weekday),
    )


def create_planning_anchor(
    runtime: CoachRuntimeContext,
    kind: AnchorKind,
    distance_m: float,
    duration_s: float,
    achieved_on: date | None = None,
    reliable: bool = True,
    notes: str | None = None,
) -> str:
    """Create one performance anchor with an explicit achievement date."""
    if achieved_on is None:
        return _json(
            {
                "status": "needs_clarification",
                "question": "An welchem Datum hast du diese Leistung erreicht?",
            }
        )
    try:
        data = PerformanceAnchorCreateInput(
            kind=kind,
            distance_m=distance_m,
            duration_s=duration_s,
            achieved_on=achieved_on,
            reliable=reliable,
            notes=notes,
        )
    except ValidationError:
        return _json({"status": "not_updated", "error": {"code": "planning.invalid_input"}})
    return _run_planning_mutation(
        runtime,
        operation="create_planning_anchor",
        resource="anchor",
        request={
            "kind": kind,
            "distance_m": distance_m,
            "duration_s": duration_s,
            "achieved_on": achieved_on.isoformat() if achieved_on is not None else None,
            "reliable": reliable,
            "notes": notes,
        },
        command=lambda commands: commands.create_performance_anchor(data),
    )


def update_planning_anchor(
    runtime: CoachRuntimeContext,
    anchor_id: int,
    changes: PerformanceAnchorUpdateInput,
) -> str:
    """Update one user-owned performance anchor."""
    return _run_planning_mutation(
        runtime,
        operation="update_planning_anchor",
        resource="anchor",
        request={
            "anchor_id": anchor_id,
            "changes": changes.model_dump(exclude_unset=True, mode="json"),
        },
        command=lambda commands: commands.update_performance_anchor(anchor_id, changes),
    )


def deactivate_planning_anchor(runtime: CoachRuntimeContext, anchor_id: int) -> str:
    """Mark one user-owned performance anchor unreliable."""
    return _run_planning_mutation(
        runtime,
        operation="deactivate_planning_anchor",
        resource="anchor",
        request={"anchor_id": anchor_id},
        command=lambda commands: commands.deactivate_performance_anchor(anchor_id),
    )


def _proposal_runtime(session: Session, runtime: CoachRuntimeContext) -> tuple[User, CoachMessage]:
    if (
        runtime.conversation_id is None
        or runtime.user_message_id is None
        or runtime.assistant_message_id is None
    ):
        raise ValueError("Coach proposal runtime is incomplete")
    user_message = session.get(CoachMessage, runtime.user_message_id)
    assistant_message = find_assistant_message(
        session,
        runtime.user_id,
        runtime.conversation_id,
        runtime.assistant_message_id,
    )
    user = session.get(User, runtime.user_id)
    if (
        user is None
        or user_message is None
        or user_message.conversation_id != runtime.conversation_id
        or user_message.role != "user"
        or assistant_message is None
    ):
        raise ValueError("Coach proposal runtime is invalid")
    return user, assistant_message


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
    if context.conversation_id is None or context.user_message_id is None:
        raise ValueError("Coach proposal runtime is incomplete")
    conversation_id = context.conversation_id
    user_message_id = context.user_message_id
    with context.session_factory() as session:
        user, assistant_message = _proposal_runtime(session, context)
        assistant_message_id = assistant_message.id
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


def revise_running_workout_proposal(
    runtime: CoachRuntimeContext,
    *,
    workout_id: int,
    revision_id: int,
    suggested_for: date | None = None,
    available_minutes: int | None = None,
    edit_scope: RevisionEditScope = "supported_parameters",
) -> str:
    """Revise only the date or time budget of an exact deterministic workout revision."""
    if runtime.conversation_id is None or runtime.user_message_id is None:
        raise ValueError("Coach proposal runtime is incomplete")
    with runtime.session_factory() as session:
        user, assistant_message = _proposal_runtime(session, runtime)
        service = WorkoutService(session, user, request_id=assistant_message.request_id)
        try:
            workout = service.get(workout_id)
            current = session.get(WorkoutRevision, revision_id)
            if current is None or current.workout_id != workout.id:
                raise WorkoutServiceError(
                    "Workout nicht gefunden",
                    code="workout.not_found",
                )
            alternative = {
                "command": "create_running_workout_proposal",
                "description": (
                    "Erstelle einen neuen deterministischen Vorschlag in einem registrierten "
                    "Format oder ändere nur Datum beziehungsweise Zeitbudget dieses Formats."
                ),
            }
            if edit_scope == "unsupported":
                return _json(
                    {
                        "status": "not_revised",
                        "error": {
                            "code": "proposal.revision_edit_unsupported",
                            "message": (
                                "Eigene Schritte, Ziele und Formatwechsel werden nicht übernommen."
                            ),
                        },
                        "supported_alternative": alternative,
                    }
                )
            if suggested_for is None and available_minutes is None:
                return _json(
                    {
                        "status": "needs_clarification",
                        "question": (
                            "Soll das Datum oder das verfügbare Zeitbudget geändert werden?"
                        ),
                    }
                )
            request_context = (current.generation_context_json or {}).get("request")
            if not isinstance(request_context, dict):
                return _json(
                    {
                        "status": "not_revised",
                        "error": {
                            "code": "proposal.context_invalid",
                            "message": "Der gebundene Parameterkontext dieser Revision fehlt.",
                        },
                        "supported_alternative": alternative,
                    }
                )
            effective_date = suggested_for or current.suggested_for
            prior_minutes = request_context.get("available_minutes")
            effective_minutes = available_minutes or prior_minutes
            if effective_date is None or not isinstance(effective_minutes, int):
                return _json(
                    {
                        "status": "not_revised",
                        "error": {
                            "code": "proposal.context_invalid",
                            "message": "Datum oder Zeitbudget dieser Revision fehlt.",
                        },
                        "supported_alternative": alternative,
                    }
                )
            RunningProposalService(
                session,
                user,
                as_of=runtime.as_of,
                request_id=assistant_message.request_id,
            ).revise(
                RunningRevisionRequest(
                    workout_id=workout_id,
                    revision_id=revision_id,
                    suggested_for=effective_date,
                    available_minutes=effective_minutes,
                    idempotency_key=(
                        f"coach-message:{assistant_message.id}:"
                        f"{assistant_message.created_at.isoformat()}:"
                        f"revise_running_workout_proposal:v1:{workout_id}:{revision_id}"
                    ),
                ),
                origin=ProposalOrigin(
                    conversation_id=runtime.conversation_id,
                    user_message_id=runtime.user_message_id,
                    assistant_message_id=assistant_message.id,
                    model_provider="openrouter",
                    model_id=assistant_message.model_id,
                    prompt_template_version=assistant_message.prompt_template_version,
                ),
            )
        except (WorkoutProposalError, TemplateExpansionError, WorkoutServiceError) as exc:
            return _json(
                {
                    "status": "not_revised",
                    "error": {"code": exc.code, "message": str(exc)},
                    **(
                        {"supported_alternative": alternative}
                        if exc.code == "proposal.revision_unsupported"
                        else {}
                    ),
                }
            )
    return _json(
        {
            "status": "revised",
            "artifact": {"type": "workout_proposal"},
        }
    )
