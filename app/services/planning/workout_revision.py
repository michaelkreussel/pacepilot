import hashlib
import json
from dataclasses import dataclass
from datetime import date

from app.models import WorkoutRevision
from app.services.planning.validator import WorkoutInput
from app.services.planning.workout_definition import (
    WorkoutDefinitionModel,
    definition_to_json,
    parse_definition,
)

STRUCTURAL_RULE_SET_VERSION = "workout-structure-v1"


@dataclass(frozen=True)
class RevisionIdentity:
    revision_id: int
    revision_number: int
    content_hash: str
    lock_version: int


@dataclass(frozen=True)
class AcceptRevisionCommand:
    identity: RevisionIdentity
    context_fingerprint: str


@dataclass(frozen=True)
class RejectRevisionCommand:
    identity: RevisionIdentity


@dataclass(frozen=True)
class RevisionMetadata:
    purpose: str | None = None
    guidance_json: dict[str, object] | None = None
    load_estimate_json: dict[str, object] | None = None
    validation_report_json: dict[str, object] | None = None
    generation_context_json: dict[str, object] | None = None
    source_type: str = "manual"
    generator_version: str | None = None
    template_id: str | None = None
    template_version: str | None = None
    rule_set_version: str | None = None
    knowledge_base_version: str | None = None
    edit_source: str = "manual"


@dataclass(frozen=True)
class ScheduleWorkoutCommand:
    revision_id: int
    scheduled_for: date
    expected_lock_version: int


@dataclass(frozen=True)
class UnscheduleWorkoutCommand:
    revision_id: int
    expected_lock_version: int


@dataclass(frozen=True)
class AcceptedWorkoutExecution:
    workout_id: int
    revision_id: int
    revision_number: int
    name: str
    sport: str
    description: str | None
    definition_version: int
    definition: dict[str, object]
    scheduled_for: date | None
    garmin_workout_id: str | None

    @property
    def definition_model(self) -> WorkoutDefinitionModel:
        return parse_definition(self.definition, self.definition_version)


def workout_content_hash(data: WorkoutInput) -> str:
    return _content_hash(
        name=data.name,
        sport=data.sport,
        suggested_for=data.scheduled_for,
        description=data.description or None,
        definition_version=data.definition_version,
        definition=definition_to_json(data.definition),
    )


def revision_content_hash(revision: WorkoutRevision) -> str:
    return _content_hash(
        name=revision.name,
        sport=revision.sport,
        suggested_for=revision.suggested_for,
        description=revision.description,
        definition_version=revision.definition_version,
        definition=revision.definition,
        purpose=revision.purpose,
        guidance_json=revision.guidance_json,
        load_estimate_json=revision.load_estimate_json,
    )


def structural_validation_report() -> dict[str, object]:
    return {
        "valid": True,
        "issues": [],
        "rule_set_version": STRUCTURAL_RULE_SET_VERSION,
    }


def default_context_fingerprint(revision_hash: str) -> str:
    canonical = json.dumps(
        {
            "revision_hash": revision_hash,
            "rule_set_version": STRUCTURAL_RULE_SET_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _content_hash(
    *,
    name: str,
    sport: str,
    suggested_for: date | None,
    description: str | None,
    definition_version: int,
    definition: dict[str, object],
    purpose: str | None = None,
    guidance_json: dict[str, object] | None = None,
    load_estimate_json: dict[str, object] | None = None,
) -> str:
    canonical = json.dumps(
        {
            "name": name,
            "sport": sport,
            "suggested_for": suggested_for.isoformat() if suggested_for else None,
            "description": description,
            "definition_version": definition_version,
            "definition": definition,
            "purpose": purpose,
            "guidance_json": guidance_json,
            "load_estimate_json": load_estimate_json,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
