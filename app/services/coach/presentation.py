from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CoachConversation, CoachMessage, User, Workout, WorkoutRevision
from app.repositories.coach import find_assistant_message
from app.services.planning.workout_service import WorkoutService
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
    revision_number: int
    content_hash: str
    lock_version: int
    context_fingerprint: str
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
    content_hash: str
    lock_version: int
    context_fingerprint: str
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


@dataclass(frozen=True)
class PlanningArtifactPresentation:
    resource: str
    title: str
    details: tuple[tuple[str, str], ...]
    confirmation_endpoint: str | None = None
    confirmation_label: str | None = None


WEEKDAY_LABELS = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)
EXPERIENCE_LABELS = {
    "novice": "Einsteiger",
    "intermediate": "Fortgeschritten",
    "advanced": "Erfahren",
}
GOAL_LABELS = {
    "general_fitness": "Allgemeine Fitness",
    "5k": "5 km",
    "10k": "10 km",
    "half_marathon": "Halbmarathon",
    "marathon": "Marathon",
}
ANCHOR_LABELS = {
    "race": "Wettkampf",
    "time_trial": "Zeitlauf",
    "manual": "Manuell",
}
ILLNESS_LABELS = {
    "none": "Keine Krankheitszeichen",
    "mild_upper_respiratory": "Leichte Erkältungszeichen",
    "fever": "Fieber",
    "systemic": "Deutliches allgemeines Krankheitsgefühl",
    "cardiopulmonary_warning": "Kardiopulmonales Warnzeichen",
}


def _feedback_artifact_presentation(
    artifact: dict[str, object],
) -> PlanningArtifactPresentation | None:
    resource = artifact.get("resource")
    request = artifact.get("request")
    result = artifact.get("result")
    if not isinstance(resource, str) or resource not in {"pre_session", "post_session"}:
        return None
    if not isinstance(request, dict) or not isinstance(result, dict):
        return None
    details: list[tuple[str, str]] = []
    if resource == "pre_session":
        workout_id = result.get("workout_id")
        if not isinstance(workout_id, int):
            return None
        details.append(("Bezug", f"Workout #{workout_id}"))
        if "available_minutes" in request and isinstance(
            minutes := result.get("available_minutes"), int
        ):
            details.append(("Verfügbar", f"{minutes} Minuten"))
        if "illness_signal" in request and isinstance(signal := result.get("illness_signal"), str):
            details.append(("Krankheit", ILLNESS_LABELS.get(signal, signal)))
        title = "Feedback vor dem Training gespeichert"
    else:
        activity_id = result.get("activity_id")
        if not isinstance(activity_id, int):
            return None
        details.append(("Bezug", f"Aktivität #{activity_id}"))
        if isinstance(completion := result.get("completion_percent"), int):
            details.append(("Abgeschlossen", f"{completion} %"))
        if isinstance(effort := result.get("session_rpe"), int | float):
            details.append(("Anstrengung", f"{float(effort):g}/10"))
        if isinstance(feel := result.get("overall_feel"), int):
            details.append(("Gefühl", f"{feel}/5"))
        if isinstance(reason := result.get("stopped_reason"), str):
            details.append(("Abbruchgrund", reason))
        title = "Feedback nach dem Training gespeichert"
    if "pain" in request and isinstance(pain := result.get("pain"), dict):
        if pain.get("present") is False:
            details.append(("Schmerzen", "Keine"))
        elif pain.get("present") is True:
            location = pain.get("location")
            severity = pain.get("severity")
            label = str(location) if isinstance(location, str) else "Gemeldet"
            if isinstance(severity, int):
                label += f" · {severity}/10"
            details.append(("Schmerzen", label))
    if isinstance(notes := result.get("notes"), str):
        details.append(("Notiz", notes))
    return PlanningArtifactPresentation(resource=resource, title=title, details=tuple(details))


def _planning_artifact_presentation(
    artifact: object,
    *,
    conversation_id: int,
    message_id: int,
) -> PlanningArtifactPresentation | None:
    if not isinstance(artifact, dict):
        return None
    if artifact.get("type") == "feedback":
        return _feedback_artifact_presentation(artifact)
    if artifact.get("type") != "planning_input":
        return None
    result = artifact.get("result")
    resource = artifact.get("resource")
    if not isinstance(resource, str):
        return None
    if resource == "goal" and artifact.get("status") == "confirmation_required":
        request = artifact.get("request")
        operation = artifact.get("operation")
        if not isinstance(request, dict) or not isinstance(operation, str):
            return None
        if operation == "update_planning_goal":
            changes = request.get("changes")
            target = changes.get("target_date") if isinstance(changes, dict) else None
            change_label = (
                f"Zieldatum auf {target} ändern"
                if isinstance(target, str)
                else "Ziel wie angezeigt ändern"
            )
            action_label = "Diese Zieländerung ausdrücklich bestätigen"
        elif operation == "deactivate_planning_goal":
            change_label = "Ziel deaktivieren"
            action_label = "Diese Zieldeaktivierung ausdrücklich bestätigen"
        else:
            return None
        return PlanningArtifactPresentation(
            resource=resource,
            title="Bestätigung erforderlich",
            details=(
                ("Änderung", change_label),
                ("Auswirkung", "Dieses Ziel wird im angenommenen Trainingszyklus verwendet."),
            ),
            confirmation_endpoint=(
                f"/coach/{conversation_id}/messages/{message_id}/planning-goal-confirmation"
            ),
            confirmation_label=action_label,
        )
    if not isinstance(result, dict):
        return None
    if resource == "goal":
        event_type = result.get("event_type")
        status = result.get("status")
        if not isinstance(event_type, str) or not isinstance(status, str):
            return None
        target = result.get("target_date")
        details = [
            ("Ziel", str(result.get("event_name") or GOAL_LABELS.get(event_type, event_type))),
            ("Distanz", GOAL_LABELS.get(event_type, event_type)),
            ("Status", "Aktiv" if status == "active" else "Archiviert"),
        ]
        if isinstance(target, str):
            details.append(("Zieldatum", target))
        return PlanningArtifactPresentation(
            resource=resource,
            title="Ziel aktualisiert",
            details=tuple(details),
        )
    if resource == "profile":
        experience = result.get("experience_level")
        weekday = result.get("preferred_long_run_weekday")
        reentry = result.get("self_declared_reentry")
        details: list[tuple[str, str]] = []
        if isinstance(experience, str):
            details.append(("Erfahrung", EXPERIENCE_LABELS.get(experience, experience)))
        if isinstance(weekday, int) and 0 <= weekday < len(WEEKDAY_LABELS):
            details.append(("Langer Lauf", WEEKDAY_LABELS[weekday]))
        if isinstance(reentry, bool):
            details.append(("Wiedereinstieg", "Ja" if reentry else "Nein"))
        note = result.get("constraint_note")
        if isinstance(note, str):
            details.append(("Hinweis", note))
        return PlanningArtifactPresentation(
            resource=resource,
            title="Trainingsprofil aktualisiert",
            details=tuple(details),
        )
    if resource == "anchor":
        kind = result.get("kind")
        distance_m = result.get("distance_m")
        duration_s = result.get("duration_s")
        achieved_on = result.get("achieved_on")
        reliable = result.get("reliable")
        if (
            not isinstance(kind, str)
            or not isinstance(distance_m, int | float)
            or not isinstance(duration_s, int | float)
            or not isinstance(achieved_on, str)
            or not isinstance(reliable, bool)
        ):
            return None
        minutes, seconds = divmod(round(float(duration_s)), 60)
        return PlanningArtifactPresentation(
            resource=resource,
            title="Leistungsanker aktualisiert",
            details=(
                ("Art", ANCHOR_LABELS.get(kind, kind)),
                ("Distanz", f"{float(distance_m) / 1000:.2f} km".replace(".", ",")),
                ("Zeit", f"{minutes}:{seconds:02d} Minuten"),
                ("Datum", achieved_on),
                ("Verlässlich", "Ja" if reliable else "Nein"),
            ),
        )
    if resource != "availability":
        return None
    weekday = result.get("weekday")
    available = result.get("available")
    minutes = result.get("available_minutes")
    if not isinstance(weekday, int) or not 0 <= weekday < len(WEEKDAY_LABELS):
        return None
    if not isinstance(available, bool):
        return None
    availability = (
        f"{minutes} Minuten" if available and isinstance(minutes, int) else "Nicht verfügbar"
    )
    return PlanningArtifactPresentation(
        resource="availability",
        title="Verfügbarkeit aktualisiert",
        details=(("Wochentag", WEEKDAY_LABELS[weekday]), ("Zeitraum", availability)),
    )


def planning_artifact_presentations(
    session: Session,
    user_id: int,
    messages: Sequence[CoachMessage],
) -> dict[int, tuple[PlanningArtifactPresentation, ...]]:
    message_ids = [message.id for message in messages if message.role == "assistant"]
    if not message_ids:
        return {}
    owned_messages = session.scalars(
        select(CoachMessage)
        .join(CoachConversation)
        .where(
            CoachConversation.user_id == user_id,
            CoachMessage.id.in_(message_ids),
            CoachMessage.role == "assistant",
        )
    )
    presentations: dict[int, tuple[PlanningArtifactPresentation, ...]] = {}
    for message in owned_messages:
        artifacts = tuple(
            presentation
            for artifact in message.artifacts_json
            if (
                presentation := _planning_artifact_presentation(
                    artifact,
                    conversation_id=message.conversation_id,
                    message_id=message.id,
                )
            )
            is not None
        )
        if artifacts:
            presentations[message.id] = artifacts
    return presentations


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
    guidance = revision.guidance or {}
    training_fit = guidance.get("training_fit")
    if isinstance(training_fit, dict):
        outcome = training_fit.get("outcome")
        if not isinstance(outcome, str):
            return None
        evaluated_at = training_fit.get("evaluated_at")
        assessed_on = _date_value(evaluated_at[:10]) if isinstance(evaluated_at, str) else None
        alternative = training_fit.get("alternative")
        alternative_code = alternative.get("code") if isinstance(alternative, dict) else None
        summary = revision.proposal_summary or {}
        runs = summary.get("runs_28_days")
        return ArtifactWarningPresentation(
            outcome=outcome,
            evidence=PersonalEvidencePresentation(
                assessed_on=assessed_on,
                runs_28_days=runs if isinstance(runs, int) and not isinstance(runs, bool) else None,
                baseline_confidence=_string_value(summary.get("baseline_confidence")),
                intensity_confidence=_string_value(summary.get("intensity_confidence")),
            ),
            coverage_percent=_number_value(summary.get("history_coverage_percent")),
            recommendation=_string_value(training_fit.get("recommendation")),
            safer_alternative=_string_value(alternative_code),
        )
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
    return ArtifactWarningPresentation(
        outcome=outcome,
        evidence=evidence,
        coverage_percent=_number_value(summary.get("history_coverage_percent")),
        recommendation=_string_value(guidance.get("rationale")),
        safer_alternative=_string_value(guidance.get("safer_alternative")),
    )


def _action(
    workout: Workout,
    key: str,
    label: str,
    revision: WorkoutRevisionView,
    scheduled_for: date | None,
) -> ArtifactActionPresentation:
    endpoint_action = "confirm" if key == "accept" else key
    return ArtifactActionPresentation(
        key=key,
        label=label,
        endpoint=f"/workouts/{workout.id}/{endpoint_action}",
        revision_id=revision.id,
        revision_number=revision.revision_number,
        content_hash=revision.content_hash,
        lock_version=workout.lock_version,
        context_fingerprint=revision.context_fingerprint,
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
                workout,
                "accept",
                ("Angenommenes Workout ersetzen" if accepted is not None else "Vorschlag annehmen"),
                current,
                current.suggested_for,
            )
        )
        if accepted is None:
            actions.append(
                _action(
                    workout,
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
                workout,
                "publish",
                "An Garmin übertragen",
                accepted,
                execution_date,
            )
        )
    elif lifecycle.key == "published":
        actions.append(
            _action(
                workout,
                "push",
                "An meine Uhr senden",
                accepted,
                execution_date,
            )
        )
    if accepted.suggested_for is not None and workout.scheduled_for != accepted.suggested_for:
        actions.append(
            _action(
                workout,
                "schedule",
                "Workout einplanen",
                accepted,
                accepted.suggested_for,
            )
        )
    if workout.scheduled_for is not None:
        actions.append(
            _action(
                workout,
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
    return _workout_artifact_presentation(
        session,
        user_id,
        message.id,
        workout,
        current_model,
        accepted_model,
    )


def workout_artifact_presentations(
    session: Session,
    user_id: int,
    messages: Sequence[CoachMessage],
) -> dict[int, WorkoutArtifactPresentation]:
    assistant_messages = {
        message.id: message for message in messages if message.role == "assistant"
    }
    if not assistant_messages:
        return {}
    workouts = list(
        session.scalars(
            select(Workout)
            .where(
                Workout.user_id == user_id,
                Workout.deleted_at.is_(None),
                Workout.source_type == "coach_single",
                Workout.source_assistant_message_id.in_(assistant_messages),
                Workout.current_revision_id.is_not(None),
            )
            .order_by(Workout.id)
        )
    )
    revision_ids = {
        revision_id
        for workout in workouts
        for revision_id in (workout.current_revision_id, workout.accepted_revision_id)
        if revision_id is not None
    }
    revisions = {
        (revision.id, revision.workout_id): revision
        for revision in session.scalars(
            select(WorkoutRevision).where(WorkoutRevision.id.in_(revision_ids))
        )
    }
    cards: dict[int, WorkoutArtifactPresentation] = {}
    for workout in workouts:
        message_id = workout.source_assistant_message_id
        if message_id not in assistant_messages or workout.current_revision_id is None:
            continue
        current_model = revisions.get((workout.current_revision_id, workout.id))
        if current_model is None:
            continue
        accepted_model = (
            revisions.get((workout.accepted_revision_id, workout.id))
            if workout.accepted_revision_id is not None
            else None
        )
        cards.setdefault(
            message_id,
            _workout_artifact_presentation(
                session,
                user_id,
                message_id,
                workout,
                current_model,
                accepted_model,
            ),
        )
    return cards


def _workout_artifact_presentation(
    session: Session,
    user_id: int,
    source_assistant_message_id: int,
    workout: Workout,
    current_model: WorkoutRevision,
    accepted_model: WorkoutRevision | None,
) -> WorkoutArtifactPresentation:
    user = session.get(User, user_id)
    if user is None:
        raise ValueError("Workout artifact user is missing")
    service = WorkoutService(session, user)
    safety_context = service.acceptance_context(workout.id)
    current = revision_view(current_model, context_fingerprint=safety_context.fingerprint)
    accepted = revision_view(accepted_model) if accepted_model is not None else None
    lifecycle = workout_lifecycle_projection(workout)
    warning = _warning_presentation(current)
    action_revision = current if accepted is None or accepted.id != current.id else accepted
    effective_date = workout.scheduled_for or action_revision.suggested_for
    training_fit = (
        service.local_action_training_fit(workout.id, action_revision.id, effective_date)
        if effective_date is not None
        else None
    )
    if training_fit is not None and training_fit.assessment.outcome.value == "elevated":
        warning = (
            replace(warning, outcome="elevated")
            if warning is not None
            else ArtifactWarningPresentation(
                outcome="elevated",
                evidence=None,
                coverage_percent=None,
                recommendation="Einheit auslassen oder anpassen.",
                safer_alternative="Ruhetag oder angepasste Einheit wählen.",
            )
        )
    acknowledgement = (
        WarningAcknowledgementPresentation(
            key="acknowledge_warning",
            label="Warnung für diese Revision und dieses Datum bestätigen",
            revision_id=action_revision.id,
            scheduled_for=effective_date,
        )
        if training_fit is not None
        and training_fit.acknowledgement_required
        and effective_date is not None
        else None
    )
    return WorkoutArtifactPresentation(
        artifact_type="workout",
        workout_id=workout.id,
        source_assistant_message_id=source_assistant_message_id,
        revision_id=current.id,
        revision_number=current.revision_number,
        content_hash=current.content_hash,
        lock_version=workout.lock_version,
        context_fingerprint=current.context_fingerprint,
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
