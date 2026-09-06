from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CoachConversation,
    CoachMessage,
    TrainingCycle,
    TrainingCycleRevision,
    TrainingPlan,
    TrainingPlanRevision,
    TrainingPlanWorkout,
    User,
    Workout,
    WorkoutRevision,
)
from app.repositories.coach import find_assistant_message
from app.services.planning.planning_queries import list_training_cycle_week_details
from app.services.planning.workout_definition import workout_metrics
from app.services.planning.workout_service import WorkoutService
from app.services.planning.workout_views import (
    GOAL_TYPE_LABELS,
    PLAN_ROLE_LABELS,
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
class DailyAdaptationChoicePresentation:
    adaptation_class: str
    label: str
    rationale: str
    endpoint: str
    context_fingerprint: str
    idempotency_key: str
    duration_minutes: int
    distance_kilometers: float
    week_duration_delta_minutes: int
    recommended: bool
    acknowledgement_required: bool


@dataclass(frozen=True)
class PlanningArtifactPresentation:
    resource: str
    title: str
    details: tuple[tuple[str, str], ...]
    confirmation_endpoint: str | None = None
    confirmation_label: str | None = None
    adaptation_choices: tuple[DailyAdaptationChoicePresentation, ...] = ()
    warning: str | None = None


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
TRAINING_FIT_LABELS = {
    "normal": "Normal",
    "caution": "Hinweis",
    "elevated": "Erhöht",
}
PLAN_CONFIDENCE_LABELS = {
    "high": "Hoch",
    "medium": "Mittel",
    "low": "Niedrig",
    "insufficient": "Unzureichend",
}
PLAN_PHASE_LABELS = {
    "reentry": "Wiedereinstieg",
    "base": "Basis",
    "build": "Aufbau",
    "specific": "Spezifisch",
    "taper": "Tapering",
    "recovery": "Regeneration",
}
PLAN_ALTERNATIVE_LABELS = {
    "reduced_volume": "Reduzierter Umfang",
    "reduced_frequency": "Reduzierte Häufigkeit",
}
PLAN_WARNING_LABELS = {
    "planner.long_run_above_typical_weekly_longest": (
        "Länger als dein typisch längster Wochenlauf"
    ),
    "planner.strides_adjacent_to_long_run": "Steigerungen direkt neben dem Langen Lauf",
    "planner.phase_quality_placed": (
        "Phasen-Qualität: Aufbau- oder Spezifisch-Phase setzt einen kontrollierten Reiz"
    ),
    "planner.weekly_frequency_low": "Geringe wöchentliche Laufhäufigkeit in der Historie",
    "planner.baseline_confidence_insufficient": "Unzureichende Datengrundlage für die Basis",
    "planner.baseline_confidence_low": "Unsichere Datengrundlage für die Basis",
    "planner.reentry_conservative": "Konservativer Wiedereinstieg",
    "planner.consistent_weeks_sparse": "Wenige konsistente Laufwochen",
    "cycle.goal_horizon_short": "Kurzer Zielhorizont für das gewählte Ziel",
    "cycle.goal_horizon_long": "Langer Zielhorizont für das gewählte Ziel",
    "cycle.quality_density_elevated": "Erhöhte Dichte an Qualitätsreizen",
    "cycle.long_run_progression_elevated": "Erhöhte Long-Run-Steigerung",
    "cycle.existing_quality_spacing": "Geringer Abstand zu bestehenden Qualitätsreizen",
    "cycle.history_limited": "Eingeschränkte Verlaufshistorie in einzelnen Wochen",
}


@dataclass(frozen=True)
class PlanSessionPresentation:
    scheduled_for: date
    weekday_label: str
    role_label: str
    name: str
    minutes: int | None


@dataclass(frozen=True)
class PlanEvidencePresentation:
    assessed_on: date | None
    coverage_percent: float | None
    baseline_confidence: str | None
    intensity_confidence: str | None


@dataclass(frozen=True)
class PlanWarningPresentation:
    code: str
    label: str


@dataclass(frozen=True)
class CycleWeekPresentation:
    position: int
    week_start: date
    phase_label: str
    minutes: int


@dataclass(frozen=True)
class WeekPlanArtifactPresentation:
    artifact_type: str
    plan_id: int
    revision_id: int
    revision_number: int
    source_assistant_message_id: int
    week_start: date
    week_end: date
    accepted_revision_id: int | None
    is_current: bool
    status_label: str
    sessions: tuple[PlanSessionPresentation, ...]
    confidence_label: str
    evidence: PlanEvidencePresentation | None
    warnings: tuple[PlanWarningPresentation, ...]
    recommendation: str | None
    alternative: str | None
    accept_endpoint: str | None
    details_url: str


@dataclass(frozen=True)
class TrainingCycleArtifactPresentation:
    artifact_type: str
    cycle_id: int
    revision_id: int
    revision_number: int
    source_assistant_message_id: int
    event_label: str
    start_date: date
    target_date: date
    accepted_revision_id: int | None
    is_current: bool
    status_label: str
    weeks: tuple[CycleWeekPresentation, ...]
    confidence_label: str
    assumptions: tuple[tuple[str, str], ...]
    evidence: PlanEvidencePresentation | None
    warnings: tuple[PlanWarningPresentation, ...]
    alternative: str | None
    accept_endpoint: str | None
    details_url: str


PlanArtifactCard = WeekPlanArtifactPresentation | TrainingCycleArtifactPresentation


def _daily_adaptation_artifact_presentation(
    artifact: dict[str, object],
) -> PlanningArtifactPresentation | None:
    result = artifact.get("result")
    if not isinstance(result, dict):
        return None
    workout_id = result.get("workout_id")
    as_of = result.get("as_of")
    context_fingerprint = result.get("context_fingerprint")
    training_fit = result.get("training_fit")
    raw_choices = result.get("choices")
    if (
        not isinstance(workout_id, int)
        or not isinstance(as_of, str)
        or not isinstance(context_fingerprint, str)
        or not isinstance(training_fit, dict)
        or not isinstance(raw_choices, list)
    ):
        return None
    outcome = training_fit.get("outcome")
    if not isinstance(outcome, str) or outcome not in TRAINING_FIT_LABELS:
        return None
    choices: list[DailyAdaptationChoicePresentation] = []
    for raw in raw_choices:
        if not isinstance(raw, dict):
            return None
        adaptation_class = raw.get("adaptation_class")
        label = raw.get("label")
        rationale = raw.get("rationale")
        idempotency_key = raw.get("idempotency_key")
        duration_minutes = raw.get("duration_minutes")
        distance_kilometers = raw.get("distance_kilometers")
        week_delta = raw.get("week_duration_delta_minutes")
        recommended = raw.get("recommended")
        if (
            not isinstance(adaptation_class, str)
            or not isinstance(label, str)
            or not isinstance(rationale, str)
            or not isinstance(idempotency_key, str)
            or not isinstance(duration_minutes, int)
            or isinstance(distance_kilometers, bool)
            or not isinstance(distance_kilometers, int | float)
            or not isinstance(week_delta, int)
            or not isinstance(recommended, bool)
        ):
            return None
        choices.append(
            DailyAdaptationChoicePresentation(
                adaptation_class=adaptation_class,
                label=label,
                rationale=rationale,
                endpoint=f"/workouts/{workout_id}/adaptation/apply",
                context_fingerprint=context_fingerprint,
                idempotency_key=idempotency_key,
                duration_minutes=duration_minutes,
                distance_kilometers=float(distance_kilometers),
                week_duration_delta_minutes=week_delta,
                recommended=recommended,
                acknowledgement_required=(
                    outcome == "elevated" and adaptation_class in {"KEEP", "REPLACE_WITH_EASY"}
                ),
            )
        )
    available_minutes = result.get("available_minutes")
    details = [
        ("Workout", f"#{workout_id}"),
        ("Datum", as_of),
        ("Trainingshinweis", TRAINING_FIT_LABELS[outcome]),
    ]
    if isinstance(available_minutes, int):
        details.append(("Verfügbar", f"{available_minutes} Minuten"))
    warning = None
    if outcome == "caution":
        warning = "Hinweise beeinflussen die Empfehlung, entfernen aber keine mögliche Auswahl."
    elif outcome == "elevated":
        warning = (
            "Erhöhtes persönliches Gesundheitsrisiko: Pause oder Änderung empfohlen. "
            "Beibehalten oder Ersetzen erfordert deine ausdrückliche Bestätigung."
        )
    return PlanningArtifactPresentation(
        resource="daily_adaptation",
        title="Anpassung für heute",
        details=tuple(details),
        adaptation_choices=tuple(choices),
        warning=warning,
    )


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
    if artifact.get("type") == "daily_adaptation":
        return _daily_adaptation_artifact_presentation(artifact)
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


def _plan_warning(code: str) -> PlanWarningPresentation:
    return PlanWarningPresentation(code=code, label=PLAN_WARNING_LABELS.get(code, code))


def _plan_evidence(
    advisory: dict[str, object],
    baseline: dict[str, object],
    intensity: dict[str, object],
) -> PlanEvidencePresentation | None:
    assessed: date | None = None
    for section in (advisory.get("evidence"), advisory.get("coverage")):
        if not isinstance(section, list):
            continue
        for item in section:
            if not isinstance(item, dict):
                continue
            observed = _date_value(item.get("observed_on")) or _date_value(item.get("current_day"))
            if observed is not None and (assessed is None or observed > assessed):
                assessed = observed
    coverage_percent: float | None = None
    coverage = advisory.get("coverage")
    if isinstance(coverage, list):
        ratios: list[float] = []
        for item in coverage:
            if not isinstance(item, dict):
                continue
            samples = item.get("baseline_sample_count")
            minimum = item.get("minimum_baseline_samples")
            if (
                isinstance(samples, int | float)
                and not isinstance(samples, bool)
                and isinstance(minimum, int | float)
                and not isinstance(minimum, bool)
                and minimum > 0
            ):
                ratios.append(min(float(samples) / float(minimum), 1.0))
        if ratios:
            coverage_percent = round(min(ratios) * 100.0, 1)
    baseline_confidence = baseline.get("confidence")
    intensity_confidence = intensity.get("confidence")
    if (
        assessed is None
        and coverage_percent is None
        and not isinstance(baseline_confidence, str)
        and not isinstance(intensity_confidence, str)
    ):
        return None
    return PlanEvidencePresentation(
        assessed_on=assessed,
        coverage_percent=coverage_percent,
        baseline_confidence=(baseline_confidence if isinstance(baseline_confidence, str) else None),
        intensity_confidence=(
            intensity_confidence if isinstance(intensity_confidence, str) else None
        ),
    )


def _advisory_section(context: dict[str, object], name: str) -> dict[str, object]:
    value = context.get(name)
    return value if isinstance(value, dict) else {}


def _week_plan_sessions(
    session: Session, user_id: int, revision_id: int
) -> tuple[PlanSessionPresentation, ...]:
    rows = session.execute(
        select(TrainingPlanWorkout, Workout)
        .join(Workout, Workout.id == TrainingPlanWorkout.workout_id)
        .where(
            TrainingPlanWorkout.plan_revision_id == revision_id,
            TrainingPlanWorkout.owner_user_id == user_id,
            Workout.user_id == user_id,
            Workout.deleted_at.is_(None),
        )
        .order_by(TrainingPlanWorkout.position)
    ).all()
    sessions: list[PlanSessionPresentation] = []
    for membership, workout in rows:
        metrics = workout_metrics(workout.definition_model)
        minutes = round(metrics.duration_seconds / 60) if metrics.duration_seconds > 0 else None
        sessions.append(
            PlanSessionPresentation(
                scheduled_for=membership.scheduled_for,
                weekday_label=WEEKDAY_LABELS[membership.scheduled_for.weekday()],
                role_label=PLAN_ROLE_LABELS.get(membership.role, membership.role),
                name=workout.name,
                minutes=minutes,
            )
        )
    return tuple(sessions)


def _week_plan_card(
    session: Session,
    revision: TrainingPlanRevision,
    plan: TrainingPlan,
) -> WeekPlanArtifactPresentation:
    context = (
        revision.generation_context_json
        if isinstance(revision.generation_context_json, dict)
        else {}
    )
    advisory = _advisory_section(context, "advisory")
    raw_warnings = advisory.get("warnings")
    warnings = tuple(
        _plan_warning(code)
        for code in (raw_warnings if isinstance(raw_warnings, list) else [])
        if isinstance(code, str)
    )
    recommendation = advisory.get("recommendation")
    alternative = advisory.get("alternative")
    alternative_label = None
    if isinstance(alternative, dict):
        alternative_code = alternative.get("code")
        if isinstance(alternative_code, str):
            alternative_label = PLAN_ALTERNATIVE_LABELS.get(alternative_code, alternative_code)
    is_accepted = plan.accepted_revision_id == revision.id
    is_current = plan.current_revision_id == revision.id
    status_label = "Angenommen" if is_accepted else "Entwurf" if is_current else "Überholt"
    today = date.today()
    current_monday = today - timedelta(days=today.weekday())
    week_offset = (revision.week_start - current_monday).days // 7
    return WeekPlanArtifactPresentation(
        artifact_type="weekly_plan",
        plan_id=plan.id,
        revision_id=revision.id,
        revision_number=revision.revision_number,
        source_assistant_message_id=revision.source_assistant_message_id or 0,
        week_start=revision.week_start,
        week_end=revision.week_end,
        accepted_revision_id=plan.accepted_revision_id,
        is_current=is_current,
        status_label=status_label,
        sessions=_week_plan_sessions(session, plan.user_id, revision.id),
        confidence_label=PLAN_CONFIDENCE_LABELS.get(
            str(advisory.get("confidence")), str(advisory.get("confidence", "–"))
        ),
        evidence=_plan_evidence(
            advisory,
            _advisory_section(context, "baseline"),
            _advisory_section(context, "intensity"),
        ),
        warnings=warnings,
        recommendation=recommendation if isinstance(recommendation, str) else None,
        alternative=alternative_label,
        accept_endpoint=(
            f"/plans/weeks/{plan.id}/revisions/{revision.id}/accept"
            if is_current and not is_accepted
            else None
        ),
        details_url=f"/plans?view=week&week={week_offset}",
    )


def _cycle_card(
    session: Session,
    revision: TrainingCycleRevision,
    cycle: TrainingCycle,
) -> TrainingCycleArtifactPresentation:
    assumptions = revision.assumptions_json if isinstance(revision.assumptions_json, dict) else {}
    validation = (
        revision.validation_report_json if isinstance(revision.validation_report_json, dict) else {}
    )
    raw_warnings = validation.get("warnings")
    warnings = tuple(
        _plan_warning(code)
        for code in (raw_warnings if isinstance(raw_warnings, list) else [])
        if isinstance(code, str)
    )
    alternative = validation.get("alternative")
    alternative_label = None
    if isinstance(alternative, dict):
        alternative_code = alternative.get("code")
        if isinstance(alternative_code, str):
            alternative_label = PLAN_ALTERNATIVE_LABELS.get(alternative_code, alternative_code)
    details = list_training_cycle_week_details(session, cycle.user_id, revision.id)
    weeks: list[CycleWeekPresentation] = []
    for detail in details:
        minutes = sum(
            round((fact.workout.duration_seconds or 0) / 60)
            for fact in detail.workouts
            if (fact.workout.duration_seconds or 0) > 0
        )
        weeks.append(
            CycleWeekPresentation(
                position=detail.membership.position,
                week_start=detail.membership.week_start,
                phase_label=PLAN_PHASE_LABELS.get(detail.membership.phase, detail.membership.phase),
                minutes=minutes,
            )
        )
    purpose = assumptions.get("purpose")
    assumption_rows: list[tuple[str, str]] = [
        ("Zieltyp", GOAL_TYPE_LABELS.get(revision.event_type, revision.event_type))
    ]
    if isinstance(purpose, str) and purpose:
        assumption_rows.append(("Zweck", purpose))
    effective_reentry = assumptions.get("effective_reentry")
    if isinstance(effective_reentry, bool):
        assumption_rows.append(("Wiedereinstieg", "Ja" if effective_reentry else "Nein"))
    evidence_parts: list[PlanEvidencePresentation] = []
    for detail in details:
        member_revision = session.get(
            TrainingPlanRevision, detail.membership.training_plan_revision_id
        )
        if member_revision is None:
            continue
        member_context = (
            member_revision.generation_context_json
            if isinstance(member_revision.generation_context_json, dict)
            else {}
        )
        member_evidence = _plan_evidence(
            _advisory_section(member_context, "advisory"),
            _advisory_section(member_context, "baseline"),
            _advisory_section(member_context, "intensity"),
        )
        if member_evidence is not None:
            evidence_parts.append(member_evidence)
    evidence = None
    if evidence_parts:
        assessed_dates = [
            part.assessed_on for part in evidence_parts if part.assessed_on is not None
        ]
        coverages = [
            part.coverage_percent for part in evidence_parts if part.coverage_percent is not None
        ]
        evidence = PlanEvidencePresentation(
            assessed_on=max(assessed_dates) if assessed_dates else None,
            coverage_percent=min(coverages) if coverages else None,
            baseline_confidence=None,
            intensity_confidence=None,
        )
    is_accepted = cycle.accepted_revision_id == revision.id
    is_current = cycle.current_revision_id == revision.id
    status_label = "Angenommen" if is_accepted else "Entwurf" if is_current else "Überholt"
    return TrainingCycleArtifactPresentation(
        artifact_type="training_cycle",
        cycle_id=cycle.id,
        revision_id=revision.id,
        revision_number=revision.revision_number,
        source_assistant_message_id=revision.source_assistant_message_id or 0,
        event_label=GOAL_TYPE_LABELS.get(revision.event_type, revision.event_type),
        start_date=revision.start_date,
        target_date=revision.target_date,
        accepted_revision_id=cycle.accepted_revision_id,
        is_current=is_current,
        status_label=status_label,
        weeks=tuple(weeks),
        confidence_label=PLAN_CONFIDENCE_LABELS.get(revision.confidence, revision.confidence),
        assumptions=tuple(assumption_rows),
        evidence=evidence,
        warnings=warnings,
        alternative=alternative_label,
        accept_endpoint=(
            f"/plans/cycles/{cycle.id}/revisions/{revision.id}/accept"
            if is_current and not is_accepted
            else None
        ),
        details_url=f"/plans/cycles/{cycle.id}",
    )


def plan_artifact_presentations(
    session: Session,
    user_id: int,
    messages: Sequence[CoachMessage],
) -> dict[int, tuple[PlanArtifactCard, ...]]:
    message_ids = [message.id for message in messages if message.role == "assistant"]
    if not message_ids:
        return {}
    owned_ids = set(
        session.scalars(
            select(CoachMessage.id)
            .join(CoachConversation)
            .where(
                CoachConversation.user_id == user_id,
                CoachMessage.id.in_(message_ids),
                CoachMessage.role == "assistant",
            )
        )
    )
    if not owned_ids:
        return {}
    weekly_revisions = session.scalars(
        select(TrainingPlanRevision)
        .where(
            TrainingPlanRevision.owner_user_id == user_id,
            TrainingPlanRevision.source_assistant_message_id.in_(owned_ids),
        )
        .order_by(TrainingPlanRevision.id)
    ).all()
    cycle_revisions = session.scalars(
        select(TrainingCycleRevision)
        .where(
            TrainingCycleRevision.owner_user_id == user_id,
            TrainingCycleRevision.source_assistant_message_id.in_(owned_ids),
        )
        .order_by(TrainingCycleRevision.id)
    ).all()
    plan_ids = {revision.plan_id for revision in weekly_revisions}
    cycle_ids = {revision.cycle_id for revision in cycle_revisions}
    plans = {
        plan.id: plan
        for plan in session.scalars(
            select(TrainingPlan).where(
                TrainingPlan.user_id == user_id, TrainingPlan.id.in_(plan_ids)
            )
        )
    }
    cycles = {
        cycle.id: cycle
        for cycle in session.scalars(
            select(TrainingCycle).where(
                TrainingCycle.user_id == user_id, TrainingCycle.id.in_(cycle_ids)
            )
        )
    }
    presentations: dict[int, list[PlanArtifactCard]] = {}
    for revision in weekly_revisions:
        plan = plans.get(revision.plan_id)
        message_id = revision.source_assistant_message_id
        if plan is None or message_id not in owned_ids:
            continue
        presentations.setdefault(message_id, []).append(_week_plan_card(session, revision, plan))
    for revision in cycle_revisions:
        cycle = cycles.get(revision.cycle_id)
        message_id = revision.source_assistant_message_id
        if cycle is None or message_id not in owned_ids:
            continue
        presentations.setdefault(message_id, []).append(_cycle_card(session, revision, cycle))
    return {message_id: tuple(cards) for message_id, cards in presentations.items() if cards}
