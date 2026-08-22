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
    WorkoutDefinition,
    parse_definition,
    workout_metrics,
)
from app.services.planning.workout_revision import default_context_fingerprint


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

    @property
    def definition_model(self) -> WorkoutDefinition:
        return parse_definition(self.definition)

    @property
    def step_count(self) -> int:
        return workout_metrics(self.definition_model).step_count

    @property
    def duration_minutes(self) -> int:
        return round(workout_metrics(self.definition_model).duration_seconds / 60)

    @property
    def source_label(self) -> str:
        return {
            "manual": "Manuell",
            "template": "Vorlage",
            "import": "Import",
            "ai": "KI",
        }.get(self.source_type, self.source_type)

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
    garmin_last_operation: str | None = None
    garmin_last_operation_status: str | None = None
    garmin_last_error: str | None = None
    safety_report: dict[str, object] | None = None
    sync_safety_report: dict[str, object] | None = None

    @property
    def has_unaccepted_changes(self) -> bool:
        return self.accepted is not None and self.accepted.id != self.current.id

    @property
    def garmin_needs_review(self) -> bool:
        if "unknown" in {self.garmin_content_status, self.garmin_device_status}:
            return True
        return (
            self.garmin_last_operation_status == "unknown"
            and self.garmin_last_operation not in {"schedule", "unschedule"}
        )

    @property
    def change_labels(self) -> tuple[str, ...]:
        if not self.accepted or not self.has_unaccepted_changes:
            return ()
        labels: list[str] = []
        for label, current_value, accepted_value in (
            ("Name", self.current.name, self.accepted.name),
            ("Sportart", self.current.sport, self.accepted.sport),
            ("Datum", self.current.suggested_for, self.accepted.suggested_for),
            ("Beschreibung", self.current.description, self.accepted.description),
            (
                "Formatversion",
                self.current.definition_version,
                self.accepted.definition_version,
            ),
            ("Ablauf", self.current.definition, self.accepted.definition),
            ("Zweck", self.current.purpose, self.accepted.purpose),
            ("Coaching-Hinweise", self.current.guidance, self.accepted.guidance),
            ("Belastung", self.current.load_estimate, self.accepted.load_estimate),
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
    definition: WorkoutDefinition

    @property
    def source_label(self) -> str:
        return {
            "manual": "Manuell",
            "template": "Vorlage",
            "import": "Import",
            "ai": "KI",
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
    )


def workout_detail_view(
    session: Session,
    workout: Workout,
    *,
    context_fingerprint: str | None = None,
    safety_report: dict[str, object] | None = None,
    sync_safety_report: dict[str, object] | None = None,
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
    return WorkoutDetailView(
        id=workout.id,
        current=revision_view(current, context_fingerprint=context_fingerprint),
        accepted=revision_view(accepted) if accepted is not None else None,
        approval_status=workout.approval_status,
        local_schedule_status=workout.local_schedule_status,
        scheduled_for=workout.scheduled_for,
        lock_version=workout.lock_version,
        source_type=workout.source_type,
        garmin_content_status=binding.content_status if binding else "not_requested",
        garmin_calendar_status=binding.calendar_status if binding else "not_requested",
        garmin_device_status=binding.device_status if binding else "not_requested",
        garmin_workout_id=remote_id or workout.garmin_workout_id,
        garmin_last_operation=(latest_operation.operation_type if latest_operation else None),
        garmin_last_operation_status=(latest_operation.status if latest_operation else None),
        garmin_last_error=binding.last_error_message if binding else None,
        safety_report=safety_report,
        sync_safety_report=sync_safety_report,
    )
