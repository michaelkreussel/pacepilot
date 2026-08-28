from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Workout, WorkoutRevision
from app.repositories.coach import find_assistant_message
from app.services.planning.workout_views import (
    WorkoutLifecycleProjection,
    WorkoutRevisionView,
    revision_view,
    workout_lifecycle_projection,
)


@dataclass(frozen=True)
class ArtifactActionPresentation:
    key: str
    label: str
    endpoint: str
    revision_id: int
    scheduled_for: date | None


@dataclass(frozen=True)
class WarningAcknowledgementPresentation:
    key: str
    label: str
    revision_id: int
    scheduled_for: date


@dataclass(frozen=True)
class PersonalEvidencePresentation:
    assessed_on: date | None
    runs_28_days: int | None
    baseline_confidence: str | None
    intensity_confidence: str | None


@dataclass(frozen=True)
class ArtifactWarningPresentation:
    outcome: str
    evidence: PersonalEvidencePresentation | None
    coverage_percent: float | None
    recommendation: str | None
    safer_alternative: str | None


@dataclass(frozen=True)
class WorkoutArtifactPresentation:
    artifact_type: str
    workout_id: int
    source_assistant_message_id: int
    revision_id: int
    revision_number: int
    accepted_revision_id: int | None
    name: str
    suggested_for: date | None
    duration_minutes: int
    target_label: str
    lifecycle: WorkoutLifecycleProjection
    warning: ArtifactWarningPresentation | None
    warning_acknowledgement: WarningAcknowledgementPresentation | None
    lifecycle_actions: tuple[ArtifactActionPresentation, ...]

    @property
    def status_label(self) -> str:
        return self.lifecycle.label

    @property
    def status_description(self) -> str:
        return self.lifecycle.description


def _date_value(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _number_value(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _warning_presentation(
    revision: WorkoutRevisionView,
) -> ArtifactWarningPresentation | None:
    context = revision.generation_context
    if not context or not isinstance(safety := context.get("safety"), dict):
        return None
    outcome = safety.get("outcome")
    if not isinstance(outcome, str):
        return None
    summary = revision.proposal_summary or {}
    runs = summary.get("runs_28_days")
    evidence = PersonalEvidencePresentation(
        assessed_on=_date_value(context.get("as_of")),
        runs_28_days=runs if isinstance(runs, int) and not isinstance(runs, bool) else None,
        baseline_confidence=_string_value(summary.get("baseline_confidence")),
        intensity_confidence=_string_value(summary.get("intensity_confidence")),
    )
    guidance = revision.guidance or {}
    return ArtifactWarningPresentation(
        outcome=outcome,
        evidence=evidence,
        coverage_percent=_number_value(summary.get("history_coverage_percent")),
        recommendation=_string_value(guidance.get("rationale")),
        safer_alternative=_string_value(guidance.get("safer_alternative")),
    )


def _action(
    workout_id: int,
    key: str,
    label: str,
    revision: WorkoutRevisionView,
    scheduled_for: date | None,
) -> ArtifactActionPresentation:
    endpoint_action = "confirm" if key == "accept" else key
    return ArtifactActionPresentation(
        key=key,
        label=label,
        endpoint=f"/workouts/{workout_id}/{endpoint_action}",
        revision_id=revision.id,
        scheduled_for=scheduled_for,
    )


def _available_actions(
    workout: Workout,
    current: WorkoutRevisionView,
    accepted: WorkoutRevisionView | None,
    lifecycle: WorkoutLifecycleProjection,
) -> tuple[ArtifactActionPresentation, ...]:
    if lifecycle.key in {"rejected", "failed"}:
        return ()
    actions: list[ArtifactActionPresentation] = []
    if accepted is None or accepted.id != current.id:
        actions.append(
            _action(
                workout.id,
                "accept",
                "Vorschlag annehmen",
                current,
                current.suggested_for,
            )
        )
        if accepted is None:
            actions.append(
                _action(
                    workout.id,
                    "reject",
                    "Vorschlag ablehnen",
                    current,
                    current.suggested_for,
                )
            )
            return tuple(actions)

    assert accepted is not None
    execution_date = workout.scheduled_for or accepted.suggested_for
    if lifecycle.key in {"accepted", "scheduled"}:
        actions.append(
            _action(
                workout.id,
                "publish",
                "An Garmin übertragen",
                accepted,
                execution_date,
            )
        )
    elif lifecycle.key == "published":
        actions.append(
            _action(
                workout.id,
                "push",
                "An meine Uhr senden",
                accepted,
                execution_date,
            )
        )
    if accepted.suggested_for is not None and workout.scheduled_for != accepted.suggested_for:
        actions.append(
            _action(
                workout.id,
                "schedule",
                "Workout einplanen",
                accepted,
                accepted.suggested_for,
            )
        )
    if workout.scheduled_for is not None:
        actions.append(
            _action(
                workout.id,
                "unschedule",
                "Termin entfernen",
                accepted,
                workout.scheduled_for,
            )
        )
    return tuple(actions)


def workout_artifact_presentation(
    session: Session,
    user_id: int,
    conversation_id: int,
    assistant_message_id: int,
) -> WorkoutArtifactPresentation | None:
    message = find_assistant_message(session, user_id, conversation_id, assistant_message_id)
    if message is None:
        return None
    workout = session.scalar(
        select(Workout)
        .where(
            Workout.user_id == user_id,
            Workout.deleted_at.is_(None),
            Workout.source_type == "coach_single",
            Workout.source_assistant_message_id == message.id,
            Workout.current_revision_id.is_not(None),
        )
        .order_by(Workout.id)
    )
    if workout is None or workout.current_revision_id is None:
        return None
    current_model = session.scalar(
        select(WorkoutRevision).where(
            WorkoutRevision.id == workout.current_revision_id,
            WorkoutRevision.workout_id == workout.id,
        )
    )
    if current_model is None:
        return None
    accepted_model = (
        session.scalar(
            select(WorkoutRevision).where(
                WorkoutRevision.id == workout.accepted_revision_id,
                WorkoutRevision.workout_id == workout.id,
            )
        )
        if workout.accepted_revision_id is not None
        else None
    )
    current = revision_view(current_model)
    accepted = revision_view(accepted_model) if accepted_model is not None else None
    lifecycle = workout_lifecycle_projection(workout)
    warning = _warning_presentation(current)
    acknowledgement = (
        WarningAcknowledgementPresentation(
            key="acknowledge_warning",
            label="Warnung für diese Revision und dieses Datum bestätigen",
            revision_id=current.id,
            scheduled_for=current.suggested_for,
        )
        if warning is not None and warning.outcome == "warn" and current.suggested_for is not None
        else None
    )
    return WorkoutArtifactPresentation(
        artifact_type="workout",
        workout_id=workout.id,
        source_assistant_message_id=message.id,
        revision_id=current.id,
        revision_number=current.revision_number,
        accepted_revision_id=accepted.id if accepted is not None else None,
        name=current.name,
        suggested_for=current.suggested_for,
        duration_minutes=current.duration_minutes,
        target_label=current.target_label,
        lifecycle=lifecycle,
        warning=warning,
        warning_acknowledgement=acknowledgement,
        lifecycle_actions=_available_actions(workout, current, accepted, lifecycle),
    )
