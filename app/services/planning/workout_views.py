from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Workout,
    WorkoutGarminBinding,
    WorkoutGarminOperation,
    WorkoutGarminRemoteIdentity,
    WorkoutRevision,
)
from app.services.planning.workout_definition import (
    HeartRateRangeTarget,
    RpeRangeTarget,
    StepBlockV2,
    WorkoutDefinitionModel,
    parse_definition,
    workout_metrics,
)
from app.services.planning.workout_revision import default_context_fingerprint

PLAN_ROLE_LABELS = {
    "easy_run": "Lockerer Lauf",
    "long_run": "Langer Lauf",
    "strides": "Steigerungen",
    "threshold_cruise": "Schwellenintervalle",
    "vo2_intervals": "VO₂max-Intervalle",
}
GOAL_TYPE_LABELS = {
    "general_fitness": "Allgemeine Fitness",
    "5k": "5 km",
    "10k": "10 km",
    "half_marathon": "Halbmarathon",
    "marathon": "Marathon",
}


@dataclass(frozen=True)
class WorkoutLifecycleProjection:
    key: str
    label: str
    description: str


def workout_lifecycle_projection(workout: Workout) -> WorkoutLifecycleProjection:
    if workout.approval_status == "rejected":
        return WorkoutLifecycleProjection(
            key="rejected",
            label="Abgelehnt",
            description="Dieser Vorschlag wurde abgelehnt und wird nicht ausgeführt.",
        )
    if workout.status == "pushed":
        return WorkoutLifecycleProjection(
            key="pushed",
            label="An Uhr gesendet",
            description="Die angenommene Revision wurde an das Garmin-Gerät gesendet.",
        )
    if workout.status == "published":
        return WorkoutLifecycleProjection(
            key="published",
            label="Bei Garmin",
            description="Die angenommene Revision wurde zu Garmin übertragen.",
        )
    if workout.local_schedule_status == "scheduled":
        return WorkoutLifecycleProjection(
            key="scheduled",
            label="Eingeplant",
            description="Die angenommene Revision ist im lokalen Kalender eingeplant.",
        )
    if workout.accepted_revision_id is not None:
        return WorkoutLifecycleProjection(
            key="accepted",
            label="Angenommen",
            description="Die geprüfte Revision wurde angenommen, aber nicht eingeplant.",
        )
    return WorkoutLifecycleProjection(
        key="draft",
        label="Unbestätigt",
        description=(
            "Der deterministische Vorschlag ist weder angenommen noch eingeplant. "
            "Prüfe ihn vor jeder weiteren Aktion."
        ),
    )


@dataclass(frozen=True)
class WorkoutRevisionView:
    id: int
    revision_number: int
    content_hash: str
    name: str
    sport: str
    suggested_for: date | None
    description: str | None
    definition_version: int
    definition: dict[str, object]
    purpose: str | None
    guidance: dict[str, object] | None
    load_estimate: dict[str, object] | None
    source_type: str
    context_fingerprint_value: str | None = None
    validation_report: dict[str, object] | None = None
    generation_context: dict[str, object] | None = None
    generator_version: str | None = None
    template_id: str | None = None
    template_version: str | None = None
    rule_set_version: str | None = None
    knowledge_base_version: str | None = None

    @property
    def definition_model(self) -> WorkoutDefinitionModel:
        return parse_definition(self.definition, self.definition_version)

    @property
    def step_count(self) -> int:
        return workout_metrics(self.definition_model).step_count

    @property
    def duration_minutes(self) -> int:
        return round(workout_metrics(self.definition_model).duration_seconds / 60)

    @property
    def target_label(self) -> str:
        definition = self.definition_model
        if len(definition.blocks) == 1 and isinstance(definition.blocks[0], StepBlockV2):
            target = definition.blocks[0].target
            if isinstance(target, HeartRateRangeTarget):
                return f"HF {target.lower_bpm}–{target.upper_bpm} bpm"
            if isinstance(target, RpeRangeTarget):
                return f"RPE {target.lower_rpe}–{target.upper_rpe}"
        return "Lokale Intensitätsleitplanken"

    @property
    def source_label(self) -> str:
        return {
            "manual": "Manuell",
            "template": "Vorlage",
            "import": "Import",
            "ai": "KI",
            "coach_single": "PacePilot-Vorschlag",
            "coach_daily_adaptation": "Tägliche Anpassung",
            "coach_weekly_plan": "Wochenplan-Vorschlag",
        }.get(self.source_type, self.source_type)

    @property
    def is_generated(self) -> bool:
        return self.source_type in {
            "coach_single",
            "coach_daily_adaptation",
            "coach_weekly_plan",
        }

    @property
    def proposal_summary(self) -> dict[str, object] | None:
        if self.source_type != "coach_single" or not self.generation_context:
            return None
        athlete = self.generation_context.get("athlete")
        if not isinstance(athlete, dict):
            return None
        baseline = athlete.get("baseline")
        intensity = athlete.get("intensity")
        windows = baseline.get("windows") if isinstance(baseline, dict) else None
        window = windows.get("28") if isinstance(windows, dict) else None
        data_quality = window.get("data_quality") if isinstance(window, dict) else None
        return {
            "runs_28_days": window.get("runs") if isinstance(window, dict) else None,
            "baseline_confidence": window.get("confidence") if isinstance(window, dict) else None,
            "history_coverage_percent": (
                data_quality.get("history_coverage_percent")
                if isinstance(data_quality, dict)
                else None
            ),
            "intensity_confidence": (
                intensity.get("confidence") if isinstance(intensity, dict) else None
            ),
            "distance_known": bool(
                self.load_estimate and self.load_estimate.get("distance_meters") is not None
            ),
            "device_target": self.guidance.get("device_target") if self.guidance else None,
        }

    @property
    def context_fingerprint(self) -> str:
        return self.context_fingerprint_value or default_context_fingerprint(self.content_hash)


@dataclass(frozen=True)
class WorkoutDetailView:
    id: int
    current: WorkoutRevisionView
    accepted: WorkoutRevisionView | None
    approval_status: str
    local_schedule_status: str
    scheduled_for: date | None
    lock_version: int
    source_type: str
    garmin_content_status: str
    garmin_calendar_status: str
    garmin_device_status: str
    garmin_workout_id: str | None
    replaces_workout_id: int | None = None
    parent: WorkoutRevisionView | None = None
    replacement_original: WorkoutRevisionView | None = None
    open_replacement_id: int | None = None
    active_replacement_id: int | None = None
    garmin_last_operation: str | None = None
    garmin_last_operation_status: str | None = None
    garmin_last_error: str | None = None
    safety_report: dict[str, object] | None = None
    sync_safety_report: dict[str, object] | None = None
    training_fit_outcome: str | None = None
    training_fit_effective_date: date | None = None
    training_fit_acknowledgement_required: bool = False
    training_fit_schedule_acknowledgement_required: bool = False
    garmin_sync_allowed: bool = True
    proposal_actions_allowed: bool = True
    edit_allowed: bool = True

    @property
    def has_unaccepted_changes(self) -> bool:
        return self.accepted is not None and self.accepted.id != self.current.id

    @property
    def is_adaptation_replacement(self) -> bool:
        return self.replaces_workout_id is not None and self.current.source_type == (
            "coach_daily_adaptation"
        )

    @property
    def comparison_revision(self) -> WorkoutRevisionView | None:
        return self.parent or self.replacement_original

    @property
    def garmin_needs_review(self) -> bool:
        if "unknown" in {self.garmin_content_status, self.garmin_device_status}:
            return True
        return (
            self.garmin_last_operation_status == "unknown"
            and self.garmin_last_operation not in {"schedule", "unschedule"}
        )

    @property
    def garmin_compilation_warnings(self) -> tuple[object, ...]:
        from app.services.garmin.workout_export import compile_workout_with_report

        by_code = {
            warning.code: warning for warning in compile_workout_with_report(self.current).warnings
        }
        return tuple(by_code.values())

    @property
    def change_labels(self) -> tuple[str, ...]:
        if not self.accepted or not self.has_unaccepted_changes:
            return ()
        return self._change_labels(self.accepted)

    @property
    def candidate_change_labels(self) -> tuple[str, ...]:
        comparison = self.comparison_revision
        if self.accepted is not None or comparison is None:
            return ()
        return self._change_labels(comparison)

    def _change_labels(self, comparison: WorkoutRevisionView) -> tuple[str, ...]:
        labels: list[str] = []
        for label, current_value, accepted_value in (
            ("Name", self.current.name, comparison.name),
            ("Sportart", self.current.sport, comparison.sport),
            ("Datum", self.current.suggested_for, comparison.suggested_for),
            ("Beschreibung", self.current.description, comparison.description),
            (
                "Formatversion",
                self.current.definition_version,
                comparison.definition_version,
            ),
            ("Ablauf", self.current.definition, comparison.definition),
            ("Zweck", self.current.purpose, comparison.purpose),
            ("Coaching-Hinweise", self.current.guidance, comparison.guidance),
            ("Belastung", self.current.load_estimate, comparison.load_estimate),
        ):
            if current_value != accepted_value:
                labels.append(label)
        return tuple(labels)


@dataclass(frozen=True)
class CalendarWorkout:
    id: int
    name: str
    sport: str
    description: str | None
    scheduled_for: date
    status: str
    step_count: int
    revision_number: int
    source_type: str
    has_unaccepted_changes: bool
    definition: WorkoutDefinitionModel

    @property
    def source_label(self) -> str:
        return {
            "manual": "Manuell",
            "template": "Vorlage",
            "import": "Import",
            "ai": "KI",
            "coach_single": "PacePilot-Vorschlag",
            "coach_daily_adaptation": "Tägliche Anpassung",
            "coach_weekly_plan": "Wochenplan-Vorschlag",
        }.get(self.source_type, self.source_type)


def revision_view(
    revision: WorkoutRevision, *, context_fingerprint: str | None = None
) -> WorkoutRevisionView:
    return WorkoutRevisionView(
        id=revision.id,
        revision_number=revision.revision_number,
        content_hash=revision.content_hash,
        name=revision.name,
        sport=revision.sport,
        suggested_for=revision.suggested_for,
        description=revision.description,
        definition_version=revision.definition_version,
        definition=revision.definition,
        purpose=revision.purpose,
        guidance=revision.guidance_json,
        load_estimate=revision.load_estimate_json,
        source_type=revision.source_type,
        context_fingerprint_value=context_fingerprint,
        validation_report=revision.validation_report_json,
        generation_context=revision.generation_context_json,
        generator_version=revision.generator_version,
        template_id=revision.template_id,
        template_version=revision.template_version,
        rule_set_version=revision.rule_set_version,
        knowledge_base_version=revision.knowledge_base_version,
    )


def workout_detail_view(
    session: Session,
    workout: Workout,
    *,
    context_fingerprint: str | None = None,
    safety_report: dict[str, object] | None = None,
    sync_safety_report: dict[str, object] | None = None,
    training_fit_outcome: str | None = None,
    training_fit_effective_date: date | None = None,
    training_fit_acknowledgement_required: bool = False,
    training_fit_schedule_acknowledgement_required: bool = False,
) -> WorkoutDetailView:
    if workout.current_revision_id is None:
        raise ValueError("Workout has no current revision")
    current = session.get(WorkoutRevision, workout.current_revision_id)
    if current is None or current.workout_id != workout.id:
        raise ValueError("Workout current revision mismatch")
    accepted = (
        session.get(WorkoutRevision, workout.accepted_revision_id)
        if workout.accepted_revision_id is not None
        else None
    )
    if accepted is not None and accepted.workout_id != workout.id:
        raise ValueError("Workout accepted revision mismatch")
    parent = (
        session.get(WorkoutRevision, current.parent_revision_id)
        if current.parent_revision_id is not None
        else None
    )
    if parent is not None and parent.workout_id != workout.id:
        raise ValueError("Workout parent revision mismatch")
    replacement_original = None
    if workout.replaces_workout_id is not None:
        original = session.scalar(
            select(Workout).where(
                Workout.id == workout.replaces_workout_id,
                Workout.user_id == workout.user_id,
                Workout.deleted_at.is_(None),
            )
        )
        if original is not None and original.accepted_revision_id is not None:
            original_revision = session.get(WorkoutRevision, original.accepted_revision_id)
            if original_revision is not None and original_revision.workout_id == original.id:
                replacement_original = original_revision
    open_replacement_id = session.scalar(
        select(Workout.id).where(
            Workout.user_id == workout.user_id,
            Workout.replaces_workout_id == workout.id,
            Workout.source_type == "coach_daily_adaptation",
            Workout.approval_status == "proposed",
            Workout.accepted_revision_id.is_(None),
            Workout.deleted_at.is_(None),
        )
    )
    active_replacement_id = session.scalar(
        select(Workout.id).where(
            Workout.user_id == workout.user_id,
            Workout.replaces_workout_id == workout.id,
            Workout.approval_status.in_({"proposed", "accepted"}),
            Workout.deleted_at.is_(None),
        )
    )
    binding = session.scalar(
        select(WorkoutGarminBinding).where(WorkoutGarminBinding.workout_id == workout.id)
    )
    remote_id = None
    if binding is not None and binding.active_remote_identity_id is not None:
        identity = session.get(WorkoutGarminRemoteIdentity, binding.active_remote_identity_id)
        remote_id = identity.garmin_workout_id if identity is not None else None
    latest_operation = (
        session.scalar(
            select(WorkoutGarminOperation)
            .where(WorkoutGarminOperation.binding_id == binding.id)
            .order_by(WorkoutGarminOperation.id.desc())
            .limit(1)
        )
        if binding is not None
        else None
    )
    from app.config import (
        DEFERRED_QUALITY_TEMPLATE_IDS,
        coach_feature_enabled,
        deferred_quality_templates_enabled,
        get_settings,
    )

    settings = get_settings()
    current_is_generated = current.source_type in {
        "coach_single",
        "coach_daily_adaptation",
        "coach_weekly_plan",
    }
    deferred_quality_actions_allowed = (
        current.template_id not in DEFERRED_QUALITY_TEMPLATE_IDS
        or deferred_quality_templates_enabled()
    )
    source_feature_enabled = (
        coach_feature_enabled(settings.coach_daily_adaptation_enabled, workout.user_id)
        if current.source_type == "coach_daily_adaptation"
        else coach_feature_enabled(settings.coach_plan_generation_enabled, workout.user_id)
        if current.source_type == "coach_weekly_plan"
        else coach_feature_enabled(settings.coach_workout_proposals_enabled, workout.user_id)
    )
    return WorkoutDetailView(
        id=workout.id,
        current=revision_view(current, context_fingerprint=context_fingerprint),
        accepted=revision_view(accepted) if accepted is not None else None,
        approval_status=workout.approval_status,
        local_schedule_status=workout.local_schedule_status,
        scheduled_for=workout.scheduled_for,
        lock_version=workout.lock_version,
        source_type=workout.source_type,
        replaces_workout_id=workout.replaces_workout_id,
        garmin_content_status=binding.content_status if binding else "not_requested",
        garmin_calendar_status=binding.calendar_status if binding else "not_requested",
        garmin_device_status=binding.device_status if binding else "not_requested",
        garmin_workout_id=remote_id or workout.garmin_workout_id,
        parent=revision_view(parent) if parent is not None else None,
        replacement_original=(
            revision_view(replacement_original) if replacement_original is not None else None
        ),
        open_replacement_id=open_replacement_id,
        active_replacement_id=active_replacement_id,
        garmin_last_operation=(latest_operation.operation_type if latest_operation else None),
        garmin_last_operation_status=(latest_operation.status if latest_operation else None),
        garmin_last_error=binding.last_error_message if binding else None,
        safety_report=safety_report,
        sync_safety_report=sync_safety_report,
        training_fit_outcome=training_fit_outcome,
        training_fit_effective_date=training_fit_effective_date,
        training_fit_acknowledgement_required=training_fit_acknowledgement_required,
        training_fit_schedule_acknowledgement_required=(
            training_fit_schedule_acknowledgement_required
        ),
        garmin_sync_allowed=(
            not current_is_generated
            or (
                source_feature_enabled
                and coach_feature_enabled(settings.coach_garmin_sync_enabled, workout.user_id)
            )
        ),
        proposal_actions_allowed=(
            (
                coach_feature_enabled(settings.coach_daily_adaptation_enabled, workout.user_id)
                if current.source_type == "coach_daily_adaptation"
                else coach_feature_enabled(settings.coach_plan_generation_enabled, workout.user_id)
                if current.source_type == "coach_weekly_plan"
                else not current_is_generated
                or coach_feature_enabled(settings.coach_workout_proposals_enabled, workout.user_id)
            )
            and deferred_quality_actions_allowed
        ),
        edit_allowed=(
            current.source_type not in {"coach_daily_adaptation", "coach_weekly_plan"}
            and (current.source_type != "coach_single" or current.template_id == "easy_run")
            and active_replacement_id is None
        ),
    )
