import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    GarminAccount,
    User,
    Workout,
    WorkoutEvent,
    WorkoutGarminBinding,
    WorkoutGarminRemoteIdentity,
    WorkoutRevision,
    WorkoutValidationRun,
)
from app.models.user import utcnow
from app.repositories.users import get_or_create_garmin_account
from app.repositories.workouts import find_workout
from app.services.garmin.client import GarminUnavailableError, connect_garmin_account
from app.services.garmin.workout_operations import (
    GarminConnector,
    GarminWorkoutOperations,
)
from app.services.planning.safety_triage import (
    SAFETY_RULE_SET_VERSION,
    SafetyContext,
    TriageOutcome,
    ValidationMode,
    build_safety_context,
)
from app.services.planning.validator import WorkoutInput, validate_workout
from app.services.planning.workout_definition import definition_to_json
from app.services.planning.workout_revision import (
    STRUCTURAL_RULE_SET_VERSION,
    AcceptedWorkoutExecution,
    AcceptRevisionCommand,
    RejectRevisionCommand,
    RevisionIdentity,
    RevisionMetadata,
    ScheduleWorkoutCommand,
    UnscheduleWorkoutCommand,
    revision_content_hash,
    structural_validation_report,
    workout_content_hash,
)


@dataclass(frozen=True)
class ProposalOrigin:
    conversation_id: int
    user_message_id: int
    assistant_message_id: int
    model_provider: str
    model_id: str | None
    prompt_template_version: str | None


class WorkoutServiceError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class WorkoutNotFoundError(WorkoutServiceError):
    def __init__(self) -> None:
        super().__init__("Workout nicht gefunden", code="workout.not_found")


class WorkoutTransitionError(WorkoutServiceError):
    pass


class WorkoutConflictError(WorkoutServiceError):
    pass


class WorkoutService:
    """User-scoped application service for revisioned workout commands."""

    def __init__(
        self,
        session: Session,
        user: User,
        *,
        connect_garmin: GarminConnector = connect_garmin_account,
        request_id: str | None = None,
    ) -> None:
        self.session = session
        self.user = user
        self.connect_garmin = connect_garmin
        self.request_id = request_id

    def get(self, workout_id: int) -> Workout:
        workout = find_workout(self.session, self.user.id, workout_id)
        if workout is None:
            raise WorkoutNotFoundError
        return workout

    def validate(self, data: WorkoutInput) -> None:
        validate_workout(data)

    def create(self, data: WorkoutInput) -> Workout:
        self.validate(data)
        workout = Workout(
            user_id=self.user.id,
            name=data.name,
            sport=data.sport,
            scheduled_for=None,
            description=data.description or None,
            status="draft",
            definition_version=data.definition_version,
            definition=definition_to_json(data.definition),
            source_type="manual",
            approval_status="draft",
            local_schedule_status="unscheduled",
            lock_version=0,
        )
        self.session.add(workout)
        self.session.flush()
        revision = self._create_revision(workout, data, revision_number=1, parent_revision_id=None)
        self.session.flush()
        self._record_structural_validation(workout, revision)
        workout.current_revision_id = revision.id
        workout.materialized_revision_id = revision.id
        self.session.add(WorkoutGarminBinding(workout_id=workout.id))
        self._event(workout, revision, "create")
        self._validate_context(workout, revision, self._safety_context(workout, revision))
        self.session.commit()
        return workout

    def create_proposal(
        self,
        data: WorkoutInput,
        metadata: RevisionMetadata,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        origin: ProposalOrigin | None = None,
        commit: bool = True,
    ) -> Workout:
        existing = self.idempotent_proposal(
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if existing is not None:
            self.verify_proposal_origin(existing, origin)
            return existing

        self.validate(data)
        workout = Workout(
            user_id=self.user.id,
            name=data.name,
            sport=data.sport,
            scheduled_for=None,
            description=data.description or None,
            status="draft",
            definition_version=data.definition_version,
            definition=definition_to_json(data.definition),
            source_type=metadata.source_type,
            approval_status="proposed",
            local_schedule_status="unscheduled",
            lock_version=0,
            originating_conversation_id=origin.conversation_id if origin else None,
            originating_user_message_id=origin.user_message_id if origin else None,
            originating_assistant_message_id=origin.assistant_message_id if origin else None,
            source_assistant_message_id=origin.assistant_message_id if origin else None,
        )
        self.session.add(workout)
        self.session.flush()
        revision_metadata = metadata
        if origin is not None:
            revision_metadata = replace(
                metadata,
                model_provider=origin.model_provider,
                model_id=origin.model_id,
                prompt_template_version=origin.prompt_template_version,
            )
        revision = self._create_revision(
            workout,
            data,
            revision_number=1,
            parent_revision_id=None,
            metadata=revision_metadata,
        )
        self.session.flush()
        self._record_structural_validation(workout, revision)
        workout.current_revision_id = revision.id
        workout.materialized_revision_id = revision.id
        self.session.add(WorkoutGarminBinding(workout_id=workout.id))
        self._event(workout, revision, "create", metadata={"source": metadata.source_type})
        self._event(
            workout,
            revision,
            "propose",
            metadata={
                "template_id": metadata.template_id or "",
                "request_fingerprint": request_fingerprint,
            },
            idempotency_key=idempotency_key,
            skip_existing=False,
        )
        try:
            validation = self._validate_context(
                workout, revision, self._safety_context(workout, revision)
            )
            if not validation.valid:
                outcome = str(validation.report_json.get("outcome", "clarify"))
                self.session.rollback()
                raise WorkoutTransitionError(
                    (
                        "Ein Sicherheitshinweis blockiert diesen Trainingsvorschlag."
                        if outcome == TriageOutcome.SAFETY_STOP.value
                        else "Vor einem Trainingsvorschlag fehlen eindeutige Sicherheitsangaben."
                    ),
                    code="workout.proposal_safety_blocked",
                )
            if commit:
                self.session.commit()
            else:
                self.session.flush()
        except IntegrityError:
            self.session.rollback()
            winner = self.idempotent_proposal(
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if winner is not None:
                self.verify_proposal_origin(winner, origin)
                return winner
            raise
        return workout

    def verify_proposal_origin(self, workout: Workout, origin: ProposalOrigin | None) -> None:
        if origin is None:
            return
        if (
            workout.originating_conversation_id != origin.conversation_id
            or workout.originating_user_message_id != origin.user_message_id
            or workout.originating_assistant_message_id != origin.assistant_message_id
            or workout.source_assistant_message_id != origin.assistant_message_id
        ):
            raise WorkoutConflictError(
                "Der vorhandene Vorschlag gehört nicht zu dieser Coach-Antwort.",
                code="proposal.origin_mismatch",
            )

    def propose_adaptation_revision(
        self,
        workout_id: int,
        data: WorkoutInput,
        metadata: RevisionMetadata,
        *,
        adaptation_class: str,
        context_fingerprint: str,
        expected_identity: RevisionIdentity,
        idempotency_key: str,
    ) -> Workout:
        self._ensure_daily_adaptation_enabled()
        if metadata.source_type != "coach_daily_adaptation":
            raise WorkoutTransitionError(
                "Die Revisionsquelle ist keine tägliche Anpassung.",
                code="adaptation.source_invalid",
            )
        workout = self.get(workout_id)
        request_hash = self._adaptation_request_hash(
            data,
            adaptation_class=adaptation_class,
            context_fingerprint=context_fingerprint,
        )
        replay = self._adaptation_event("adapt_propose", idempotency_key)
        if replay is not None:
            self._verify_adaptation_replay(replay, workout, request_hash)
            return workout
        self.validate(data)
        accepted = self._accepted_revision(workout)
        if workout.current_revision_id != accepted.id:
            raise WorkoutConflictError(
                "Für dieses Workout ist bereits eine neue Revision offen.",
                code="adaptation.candidate_already_open",
            )
        self._verify_revision_identity(workout, accepted, expected_identity)
        next_revision_number = (
            self.session.scalar(
                select(func.max(WorkoutRevision.revision_number)).where(
                    WorkoutRevision.workout_id == workout.id
                )
            )
            or 0
        ) + 1
        revision = self._create_revision(
            workout,
            data,
            revision_number=next_revision_number,
            parent_revision_id=accepted.id,
            metadata=metadata,
        )
        self.session.flush()
        self._record_structural_validation(workout, revision)
        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                update(Workout)
                .where(
                    Workout.id == workout.id,
                    Workout.user_id == self.user.id,
                    Workout.current_revision_id == accepted.id,
                    Workout.accepted_revision_id == accepted.id,
                    Workout.lock_version == expected_identity.lock_version,
                    Workout.deleted_at.is_(None),
                )
                .values(
                    current_revision_id=revision.id,
                    approval_status="proposed",
                    lock_version=Workout.lock_version + 1,
                )
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount != 1:
            self.session.rollback()
            replay = self._adaptation_event("adapt_propose", idempotency_key)
            if replay is not None:
                self._verify_adaptation_replay(replay, workout, request_hash)
                return self.get(workout_id)
            raise WorkoutConflictError(
                "Das Workout wurde zwischenzeitlich geändert.",
                code="workout.lock_stale",
            )
        self._event(
            workout,
            revision,
            "adapt_propose",
            metadata={
                "adaptation_class": adaptation_class,
                "context_fingerprint": context_fingerprint,
                "request_hash": request_hash,
                "parent_revision_id": accepted.id,
            },
            idempotency_key=idempotency_key,
            skip_existing=False,
        )
        self._validate_context(workout, revision, self._safety_context(workout, revision))
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            replay = self._adaptation_event("adapt_propose", idempotency_key)
            if replay is None:
                raise
            self._verify_adaptation_replay(replay, workout, request_hash)
            return self.get(workout_id)
        self.session.refresh(workout)
        return workout

    def propose_adaptation_replacement(
        self,
        workout_id: int,
        data: WorkoutInput,
        metadata: RevisionMetadata,
        *,
        context_fingerprint: str,
        expected_identity: RevisionIdentity,
        idempotency_key: str,
    ) -> Workout:
        self._ensure_daily_adaptation_enabled()
        if metadata.source_type != "coach_daily_adaptation":
            raise WorkoutTransitionError(
                "Die Revisionsquelle ist keine tägliche Anpassung.",
                code="adaptation.source_invalid",
            )
        original = self.get(workout_id)
        request_hash = self._adaptation_request_hash(
            data,
            adaptation_class="REPLACE_WITH_EASY",
            context_fingerprint=context_fingerprint,
        )
        replay = self._adaptation_event("adapt_replace_propose", idempotency_key)
        if replay is not None:
            self._verify_adaptation_replay(replay, original, request_hash)
            replacement_id = replay.safe_metadata_json.get("replacement_workout_id")
            if not isinstance(replacement_id, int):
                raise WorkoutConflictError(
                    "Dem Ersatz-Audit fehlt das neue Workout.",
                    code="adaptation.replay_invalid",
                ) from None
            return self.get(replacement_id)
        self.validate(data)
        accepted = self._accepted_revision(original)
        if original.current_revision_id != accepted.id:
            raise WorkoutConflictError(
                "Für dieses Workout ist bereits eine neue Revision offen.",
                code="adaptation.candidate_already_open",
            )
        self._verify_revision_identity(original, accepted, expected_identity)
        if original.local_schedule_status != "scheduled" or original.scheduled_for is None:
            raise WorkoutConflictError(
                "Der Trainingstermin wurde bereits geändert.",
                code="adaptation.schedule_stale",
            )
        open_replacement = self.session.scalar(
            select(Workout.id).where(
                Workout.user_id == self.user.id,
                Workout.replaces_workout_id == original.id,
                Workout.source_type == "coach_daily_adaptation",
                Workout.approval_status == "proposed",
                Workout.accepted_revision_id.is_(None),
                Workout.deleted_at.is_(None),
            )
        )
        if open_replacement is not None:
            raise WorkoutConflictError(
                "Für dieses Workout ist bereits ein Ersatzvorschlag offen.",
                code="adaptation.candidate_already_open",
            )
        replacement = Workout(
            user_id=self.user.id,
            name=data.name,
            sport=data.sport,
            scheduled_for=None,
            description=data.description or None,
            status="draft",
            definition_version=data.definition_version,
            definition=definition_to_json(data.definition),
            source_type="coach_daily_adaptation",
            approval_status="proposed",
            local_schedule_status="unscheduled",
            lock_version=0,
            replaces_workout_id=original.id,
        )
        self.session.add(replacement)
        self.session.flush()
        revision = self._create_revision(
            replacement,
            data,
            revision_number=1,
            parent_revision_id=None,
            metadata=metadata,
        )
        self.session.flush()
        self._record_structural_validation(replacement, revision)
        replacement.current_revision_id = revision.id
        replacement.materialized_revision_id = revision.id
        self.session.add(WorkoutGarminBinding(workout_id=replacement.id))
        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                update(Workout)
                .where(
                    Workout.id == original.id,
                    Workout.user_id == self.user.id,
                    Workout.current_revision_id == accepted.id,
                    Workout.accepted_revision_id == accepted.id,
                    Workout.lock_version == expected_identity.lock_version,
                    Workout.local_schedule_status == "scheduled",
                    Workout.scheduled_for == data.scheduled_for,
                    Workout.deleted_at.is_(None),
                )
                .values(lock_version=Workout.lock_version + 1)
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount != 1:
            self.session.rollback()
            replay = self._adaptation_event("adapt_replace_propose", idempotency_key)
            if replay is not None:
                self._verify_adaptation_replay(replay, original, request_hash)
                replacement_id = replay.safe_metadata_json.get("replacement_workout_id")
                if isinstance(replacement_id, int):
                    return self.get(replacement_id)
            raise WorkoutConflictError(
                "Das Workout wurde zwischenzeitlich geändert.", code="workout.lock_stale"
            )
        self._event(
            replacement,
            revision,
            "create",
            metadata={"source": metadata.source_type, "replaces_workout_id": original.id},
        )
        self._event(
            original,
            accepted,
            "adapt_replace_propose",
            metadata={
                "adaptation_class": "REPLACE_WITH_EASY",
                "context_fingerprint": context_fingerprint,
                "request_hash": request_hash,
                "replacement_workout_id": replacement.id,
            },
            idempotency_key=idempotency_key,
            skip_existing=False,
        )
        self._validate_context(replacement, revision, self._safety_context(replacement, revision))
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            replay = self._adaptation_event("adapt_replace_propose", idempotency_key)
            if replay is None:
                raise
            self._verify_adaptation_replay(replay, original, request_hash)
            replacement_id = replay.safe_metadata_json.get("replacement_workout_id")
            if not isinstance(replacement_id, int):
                raise WorkoutConflictError(
                    "Dem Ersatz-Audit fehlt das neue Workout.",
                    code="adaptation.replay_invalid",
                ) from None
            return self.get(replacement_id)
        self.session.refresh(original)
        self.session.refresh(replacement)
        return replacement

    def record_adaptation_keep(
        self,
        workout_id: int,
        *,
        context_fingerprint: str,
        expected_identity: RevisionIdentity,
        idempotency_key: str,
    ) -> Workout:
        self._ensure_daily_adaptation_enabled()
        workout = self.get(workout_id)
        request_hash = self._adaptation_decision_hash(
            "KEEP", context_fingerprint, expected_identity
        )
        replay = self._adaptation_event("adapt_keep", idempotency_key)
        if replay is not None:
            self._verify_adaptation_replay(replay, workout, request_hash)
            return workout
        accepted = self._accepted_revision(workout)
        if workout.current_revision_id != accepted.id:
            raise WorkoutConflictError(
                "Für dieses Workout ist bereits eine neue Revision offen.",
                code="adaptation.candidate_already_open",
            )
        self._verify_revision_identity(workout, accepted, expected_identity)
        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                update(Workout)
                .where(
                    Workout.id == workout.id,
                    Workout.user_id == self.user.id,
                    Workout.current_revision_id == accepted.id,
                    Workout.accepted_revision_id == accepted.id,
                    Workout.lock_version == expected_identity.lock_version,
                    Workout.local_schedule_status == "scheduled",
                    Workout.deleted_at.is_(None),
                )
                .values(lock_version=Workout.lock_version + 1)
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount != 1:
            self.session.rollback()
            replay = self._adaptation_event("adapt_keep", idempotency_key)
            if replay is not None:
                self._verify_adaptation_replay(replay, workout, request_hash)
                return self.get(workout_id)
            raise WorkoutConflictError(
                "Das Workout wurde zwischenzeitlich geändert.", code="workout.lock_stale"
            )
        self._event(
            workout,
            accepted,
            "adapt_keep",
            metadata={
                "adaptation_class": "KEEP",
                "context_fingerprint": context_fingerprint,
                "request_hash": request_hash,
            },
            idempotency_key=idempotency_key,
            skip_existing=False,
        )
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            replay = self._adaptation_event("adapt_keep", idempotency_key)
            if replay is None:
                raise
            self._verify_adaptation_replay(replay, workout, request_hash)
            return self.get(workout_id)
        self.session.refresh(workout)
        return workout

    def apply_adaptation_rest(
        self,
        workout_id: int,
        *,
        context_fingerprint: str,
        expected_identity: RevisionIdentity,
        idempotency_key: str,
    ) -> Workout:
        self._ensure_daily_adaptation_enabled()
        workout = self.get(workout_id)
        request_hash = self._adaptation_decision_hash(
            "REST", context_fingerprint, expected_identity
        )
        replay = self._adaptation_event("adapt_rest", idempotency_key)
        if replay is not None:
            self._verify_adaptation_replay(replay, workout, request_hash)
            return workout
        accepted = self._accepted_revision(workout)
        if workout.current_revision_id != accepted.id:
            raise WorkoutConflictError(
                "Für dieses Workout ist bereits eine neue Revision offen.",
                code="adaptation.candidate_already_open",
            )
        self._verify_revision_identity(workout, accepted, expected_identity)
        if workout.local_schedule_status != "scheduled" or workout.scheduled_for is None:
            raise WorkoutConflictError(
                "Der Trainingstermin wurde bereits geändert.",
                code="adaptation.schedule_stale",
            )
        previous_date = workout.scheduled_for
        binding = self._binding(workout)
        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                update(Workout)
                .where(
                    Workout.id == workout.id,
                    Workout.user_id == self.user.id,
                    Workout.current_revision_id == accepted.id,
                    Workout.accepted_revision_id == accepted.id,
                    Workout.lock_version == expected_identity.lock_version,
                    Workout.local_schedule_status == "scheduled",
                    Workout.scheduled_for == previous_date,
                    Workout.deleted_at.is_(None),
                )
                .values(
                    scheduled_for=None,
                    local_schedule_status="cancelled",
                    lock_version=Workout.lock_version + 1,
                )
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount != 1:
            self.session.rollback()
            replay = self._adaptation_event("adapt_rest", idempotency_key)
            if replay is not None:
                self._verify_adaptation_replay(replay, workout, request_hash)
                return self.get(workout_id)
            raise WorkoutConflictError(
                "Das Workout wurde zwischenzeitlich geändert.",
                code="workout.lock_stale",
            )
        if (
            binding.active_remote_identity_id is not None
            and binding.remote_scheduled_for is not None
            and binding.calendar_status not in {"pending", "unknown"}
        ):
            binding.calendar_status = "pending"
        self._event(
            workout,
            accepted,
            "unschedule",
            metadata={"previous_date": previous_date.isoformat(), "source": "daily_adaptation"},
            idempotency_key=f"{idempotency_key}:unschedule",
            skip_existing=False,
        )
        self._event(
            workout,
            accepted,
            "adapt_rest",
            metadata={
                "adaptation_class": "REST",
                "context_fingerprint": context_fingerprint,
                "request_hash": request_hash,
                "previous_date": previous_date.isoformat(),
                "device_delivery_may_persist": binding.device_status == "request_accepted",
            },
            idempotency_key=idempotency_key,
            skip_existing=False,
        )
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            replay = self._adaptation_event("adapt_rest", idempotency_key)
            if replay is None:
                raise
            self._verify_adaptation_replay(replay, workout, request_hash)
            return self.get(workout_id)
        self.session.refresh(workout)
        return workout

    def discard_adaptation_revision(
        self, workout_id: int, command: RejectRevisionCommand
    ) -> Workout:
        workout = self.get(workout_id)
        current = self._current_revision(workout)
        if (
            workout.replaces_workout_id is not None
            and workout.accepted_revision_id is None
            and current.source_type == "coach_daily_adaptation"
        ):
            self._verify_revision_identity(workout, current, command.identity)
            if workout.approval_status == "rejected":
                return workout
            source = self.get(workout.replaces_workout_id)
            result = cast(
                "CursorResult[Any]",
                self.session.execute(
                    update(Workout)
                    .where(
                        Workout.id == workout.id,
                        Workout.user_id == self.user.id,
                        Workout.current_revision_id == current.id,
                        Workout.accepted_revision_id.is_(None),
                        Workout.approval_status == "proposed",
                        Workout.lock_version == command.identity.lock_version,
                        Workout.deleted_at.is_(None),
                    )
                    .values(
                        approval_status="rejected",
                        lock_version=Workout.lock_version + 1,
                    )
                    .execution_options(synchronize_session=False)
                ),
            )
            if result.rowcount != 1:
                self.session.rollback()
                raise WorkoutConflictError(
                    "Das Workout wurde zwischenzeitlich geändert.", code="workout.lock_stale"
                )
            self._event(
                workout,
                current,
                "adapt_reject",
                metadata={"replaces_workout_id": source.id},
            )
            source_revision = self._accepted_revision(source)
            self._event(
                source,
                source_revision,
                "adapt_replace_reject",
                metadata={"replacement_workout_id": workout.id},
            )
            self.session.commit()
            self.session.refresh(workout)
            return workout
        accepted = self._accepted_revision(workout)
        if (
            current.source_type != "coach_daily_adaptation"
            or current.parent_revision_id != accepted.id
        ):
            raise WorkoutTransitionError(
                "Es ist keine tägliche Anpassung zum Verwerfen geöffnet.",
                code="adaptation.discard_invalid",
            )
        self._verify_revision_identity(workout, current, command.identity)
        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                update(Workout)
                .where(
                    Workout.id == workout.id,
                    Workout.user_id == self.user.id,
                    Workout.current_revision_id == current.id,
                    Workout.accepted_revision_id == accepted.id,
                    Workout.lock_version == command.identity.lock_version,
                    Workout.deleted_at.is_(None),
                )
                .values(
                    current_revision_id=accepted.id,
                    approval_status="accepted",
                    lock_version=Workout.lock_version + 1,
                )
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount != 1:
            self.session.rollback()
            raise WorkoutConflictError(
                "Das Workout wurde zwischenzeitlich geändert.", code="workout.lock_stale"
            )
        self._event(
            workout,
            current,
            "adapt_reject",
            metadata={"restored_revision_id": accepted.id},
        )
        self.session.commit()
        self.session.refresh(workout)
        return workout

    def idempotent_proposal(
        self, *, idempotency_key: str, request_fingerprint: str
    ) -> Workout | None:
        existing_event = self.session.scalar(
            select(WorkoutEvent).where(
                WorkoutEvent.owner_user_id == self.user.id,
                WorkoutEvent.action == "propose",
                WorkoutEvent.idempotency_key == idempotency_key,
            )
        )
        if existing_event is None:
            return None
        if existing_event.safe_metadata_json.get("request_fingerprint") != request_fingerprint:
            raise WorkoutConflictError(
                "Dieser Wiederholungsschlüssel wurde bereits für einen anderen Vorschlag genutzt.",
                code="proposal.idempotency_conflict",
            )
        return self.get(existing_event.workout_id)

    def update(
        self,
        workout_id: int,
        data: WorkoutInput,
        *,
        expected_identity: RevisionIdentity | None = None,
        idempotency_key: str | None = None,
    ) -> Workout:
        workout = self.get(workout_id)
        self._ensure_generated_proposals_enabled(workout)
        current = self._current_revision(workout)
        if current.source_type == "coach_weekly_plan":
            raise WorkoutTransitionError(
                "Wochenplan-Vorschläge werden einzeln angenommen oder abgelehnt, nicht bearbeitet.",
                code="plan.edit_not_supported",
            )
        if current.source_type == "coach_daily_adaptation":
            raise WorkoutTransitionError(
                "Tägliche Anpassungen können vor der Annahme verworfen oder neu erzeugt werden.",
                code="adaptation.edit_not_supported",
            )
        request_hash = workout_content_hash(data)
        if current.source_type == "coach_single" and idempotency_key:
            existing_event = self.session.scalar(
                select(WorkoutEvent).where(
                    WorkoutEvent.owner_user_id == self.user.id,
                    WorkoutEvent.action == "revise",
                    WorkoutEvent.idempotency_key == idempotency_key,
                )
            )
            if existing_event is not None:
                if existing_event.safe_metadata_json.get("request_hash") != request_hash:
                    raise WorkoutConflictError(
                        "Dieser Wiederholungsschlüssel gehört zu einer anderen Bearbeitung.",
                        code="workout.idempotency_conflict",
                    )
                return workout
        self.validate(data)
        metadata = None
        if current.source_type == "coach_single":
            if expected_identity is None or idempotency_key is None:
                raise WorkoutConflictError(
                    "Für generierte Vorschläge fehlen exakte Revisionsangaben.",
                    code="workout.revision_identity_required",
                )
            if (
                current.id != expected_identity.revision_id
                or current.revision_number != expected_identity.revision_number
                or current.content_hash != expected_identity.content_hash
                or workout.lock_version != expected_identity.lock_version
            ):
                raise WorkoutConflictError(
                    "Diese Workout-Revision ist nicht mehr aktuell.",
                    code="workout.revision_stale",
                )
            expected_lock_version = expected_identity.lock_version
            from app.services.planning.workout_proposals import edited_easy_run_metadata

            metadata = edited_easy_run_metadata(self.session, self.user, current, data)
        revision = self._create_revision(
            workout,
            data,
            revision_number=current.revision_number + 1,
            parent_revision_id=current.id,
            metadata=metadata,
        )
        self.session.flush()
        self._record_structural_validation(workout, revision)
        change_labels = self._change_labels(current, revision)
        if current.source_type == "coach_single":
            result = cast(
                "CursorResult[Any]",
                self.session.execute(
                    update(Workout)
                    .where(
                        Workout.id == workout.id,
                        Workout.user_id == self.user.id,
                        Workout.current_revision_id == current.id,
                        Workout.lock_version == expected_lock_version,
                        Workout.deleted_at.is_(None),
                    )
                    .values(
                        current_revision_id=revision.id,
                        materialized_revision_id=(
                            revision.id
                            if workout.accepted_revision_id is None
                            else workout.materialized_revision_id
                        ),
                        approval_status="proposed",
                        lock_version=Workout.lock_version + 1,
                        **(
                            {
                                "name": revision.name,
                                "sport": revision.sport,
                                "description": revision.description,
                                "definition_version": revision.definition_version,
                                "definition": revision.definition,
                            }
                            if workout.accepted_revision_id is None
                            else {}
                        ),
                    )
                    .execution_options(synchronize_session=False)
                ),
            )
            if result.rowcount != 1:
                self.session.rollback()
                replay = self.session.scalar(
                    select(WorkoutEvent).where(
                        WorkoutEvent.owner_user_id == self.user.id,
                        WorkoutEvent.action == "revise",
                        WorkoutEvent.idempotency_key == idempotency_key,
                    )
                )
                if (
                    replay is not None
                    and replay.safe_metadata_json.get("request_hash") == request_hash
                ):
                    return self.get(workout_id)
                raise WorkoutConflictError(
                    "Das Workout wurde zwischenzeitlich geändert.", code="workout.lock_stale"
                )
            self._event(
                workout,
                revision,
                "revise",
                metadata={
                    "parent_revision_id": current.id,
                    "changed_fields": list(change_labels),
                    "request_hash": request_hash,
                },
                idempotency_key=idempotency_key,
            )
            self._validate_context(workout, revision, self._safety_context(workout, revision))
            self.session.commit()
            self.session.refresh(workout)
            return workout
        workout.current_revision_id = revision.id
        workout.approval_status = (
            "proposed"
            if workout.accepted_revision_id or workout.source_type == "coach_single"
            else "draft"
        )
        workout.lock_version += 1
        if workout.accepted_revision_id is None:
            workout.materialized_revision_id = revision.id
            self._materialize(workout, revision)
        self._event(workout, revision, "revise")
        self._validate_context(workout, revision, self._safety_context(workout, revision))
        self.session.commit()
        return workout

    def confirm(self, workout_id: int, command: AcceptRevisionCommand) -> Workout:
        return self.accept(workout_id, command)

    def accept(self, workout_id: int, command: AcceptRevisionCommand) -> Workout:
        workout = self.get(workout_id)
        self._ensure_generated_proposals_enabled(workout)
        revision = self._current_revision(workout)
        replacement_source = (
            self.get(workout.replaces_workout_id)
            if workout.replaces_workout_id is not None
            else None
        )
        identity = command.identity
        if (
            revision.id != identity.revision_id
            or revision.revision_number != identity.revision_number
            or revision.content_hash != identity.content_hash
        ):
            raise WorkoutConflictError(
                "Diese Workout-Revision ist nicht mehr aktuell.",
                code="workout.revision_stale",
            )
        safety_context = self._safety_context(workout, revision)
        if command.context_fingerprint != safety_context.fingerprint:
            raise WorkoutConflictError(
                "Der Prüfkontext dieser Workout-Revision ist nicht mehr aktuell.",
                code="workout.validation_context_stale",
            )
        if workout.accepted_revision_id == revision.id and workout.approval_status == "accepted":
            if replacement_source is not None and (
                replacement_source.local_schedule_status != "cancelled"
                or replacement_source.scheduled_for is not None
            ):
                raise WorkoutConflictError(
                    "Der ersetzte Termin ist nicht vollständig aufgehoben.",
                    code="adaptation.replacement_state_invalid",
                )
            return workout
        if workout.approval_status == "rejected":
            raise WorkoutTransitionError(
                "Ein abgelehnter Vorschlag muss vor der Annahme bearbeitet werden.",
                code="workout.proposal_rejected",
            )
        if revision.source_type in {"coach_single", "coach_daily_adaptation"}:
            from app.services.planning.workout_proposals import (
                ensure_easy_run_device_target_current,
            )

            ensure_easy_run_device_target_current(self.session, self.user.id, revision)
        binding = self._binding(workout)
        self._ensure_garmin_state_known(binding)
        source_binding = (
            self._binding(replacement_source) if replacement_source is not None else None
        )
        if source_binding is not None:
            self._ensure_garmin_state_known(source_binding)
        adaptation_validation: WorkoutValidationRun | None = None
        if revision.source_type == "coach_daily_adaptation":
            from app.services.planning.daily_adaptation import (
                DailyAdaptationError,
                DailyAdaptationService,
            )

            generation_context = revision.generation_context_json or {}
            expected_adaptation_context = generation_context.get("adaptation_context_fingerprint")
            adaptation_as_of = generation_context.get("as_of")
            if not isinstance(expected_adaptation_context, str):
                raise WorkoutTransitionError(
                    "Der Anpassungsrevision fehlt ihr geprüfter Kontext.",
                    code="adaptation.context_missing",
                )
            if not isinstance(adaptation_as_of, str):
                raise WorkoutTransitionError(
                    "Der Anpassungsrevision fehlt ihr geprüfter Trainingstag.",
                    code="adaptation.context_missing",
                )
            try:
                adaptation_day = date.fromisoformat(adaptation_as_of)
            except ValueError as exc:
                raise WorkoutTransitionError(
                    "Der geprüfte Trainingstag ist ungültig.",
                    code="adaptation.context_invalid",
                ) from exc
            if adaptation_day != date.today():
                raise WorkoutConflictError(
                    "Diese tägliche Anpassung ist nicht mehr aktuell.",
                    code="adaptation.context_stale",
                )
            try:
                adaptation_workout_id = (
                    replacement_source.id if replacement_source is not None else workout.id
                )
                current_adaptation = DailyAdaptationService(
                    self.session,
                    self.user,
                    as_of=adaptation_day,
                    request_id=self.request_id,
                ).assess_today(
                    adaptation_workout_id,
                    allow_open_candidate=True,
                    expected_replacement_id=(
                        workout.id if replacement_source is not None else None
                    ),
                )
            except DailyAdaptationError as exc:
                raise WorkoutConflictError(str(exc), code=exc.code) from exc
            if current_adaptation.context_fingerprint != expected_adaptation_context:
                raise WorkoutConflictError(
                    "Der Kontext dieser Anpassung ist nicht mehr aktuell.",
                    code="adaptation.context_stale",
                )
            adaptation_validation = self._validate_context(
                workout,
                revision,
                SafetyContext(
                    fingerprint=current_adaptation.context_fingerprint,
                    feedback_ids=safety_context.feedback_ids,
                    report=safety_context.report,
                ),
                validation_kind="daily_adaptation_acceptance",
                force=True,
            )
        validation = self._validate_context(
            workout,
            revision,
            safety_context,
            validation_kind="acceptance",
            force=True,
        )
        if not validation.valid:
            outcome = str(validation.report_json.get("outcome", "clarify"))
            message = (
                "Ein Sicherheitshinweis blockiert die Annahme dieses Lauftrainings."
                if outcome == TriageOutcome.SAFETY_STOP.value
                else "Vor der Annahme fehlen noch eindeutige Sicherheitsangaben."
            )
            self.session.commit()
            raise WorkoutTransitionError(
                message,
                code="workout.validation_failed",
            )

        accepted_at = utcnow()
        replacement_date = None
        if replacement_source is not None:
            original_context = (revision.generation_context_json or {}).get("original_workout")
            original_revision_context = (revision.generation_context_json or {}).get(
                "original_revision"
            )
            if not isinstance(original_context, dict) or not isinstance(
                original_revision_context, dict
            ):
                raise WorkoutTransitionError(
                    "Dem Ersatzvorschlag fehlt der geprüfte Ursprung.",
                    code="adaptation.replacement_context_missing",
                )
            scheduled_for = original_context.get("scheduled_for")
            expected_source_lock = original_context.get("acceptance_lock_version")
            if not isinstance(scheduled_for, str) or not isinstance(expected_source_lock, int):
                raise WorkoutTransitionError(
                    "Der geprüfte Ersatztermin ist ungültig.",
                    code="adaptation.replacement_context_invalid",
                )
            replacement_date = date.fromisoformat(scheduled_for)
            source_revision = self._accepted_revision(replacement_source)
            if (
                original_context.get("id") != replacement_source.id
                or original_revision_context.get("id") != source_revision.id
                or original_revision_context.get("number") != source_revision.revision_number
                or original_revision_context.get("content_hash") != source_revision.content_hash
            ):
                raise WorkoutConflictError(
                    "Das zu ersetzende Workout wurde zwischenzeitlich geändert.",
                    code="adaptation.context_stale",
                )
            source_result = cast(
                "CursorResult[Any]",
                self.session.execute(
                    update(Workout)
                    .where(
                        Workout.id == replacement_source.id,
                        Workout.user_id == self.user.id,
                        Workout.current_revision_id == source_revision.id,
                        Workout.accepted_revision_id == source_revision.id,
                        Workout.lock_version == expected_source_lock,
                        Workout.local_schedule_status == "scheduled",
                        Workout.scheduled_for == replacement_date,
                        Workout.deleted_at.is_(None),
                    )
                    .values(
                        scheduled_for=None,
                        local_schedule_status="cancelled",
                        status="superseded",
                        lock_version=Workout.lock_version + 1,
                    )
                    .execution_options(synchronize_session=False)
                ),
            )
            if source_result.rowcount != 1:
                self.session.rollback()
                raise WorkoutConflictError(
                    "Das zu ersetzende Workout wurde zwischenzeitlich geändert.",
                    code="workout.lock_stale",
                )
        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                update(Workout)
                .where(
                    Workout.id == workout.id,
                    Workout.user_id == self.user.id,
                    Workout.current_revision_id == revision.id,
                    Workout.lock_version == identity.lock_version,
                    Workout.deleted_at.is_(None),
                )
                .values(
                    accepted_revision_id=revision.id,
                    materialized_revision_id=revision.id,
                    accepted_at=accepted_at,
                    accepted_by_user_id=self.user.id,
                    approval_status="accepted",
                    lock_version=Workout.lock_version + 1,
                    status="confirmed",
                    name=revision.name,
                    sport=revision.sport,
                    description=revision.description,
                    definition_version=revision.definition_version,
                    definition=revision.definition,
                    scheduled_for=(
                        replacement_date
                        if replacement_source is not None
                        else workout.scheduled_for
                    ),
                    local_schedule_status=(
                        "scheduled"
                        if replacement_source is not None
                        else workout.local_schedule_status
                    ),
                )
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount != 1:
            self.session.rollback()
            raise WorkoutConflictError(
                "Das Workout wurde zwischenzeitlich geändert.",
                code="workout.lock_stale",
            )

        if binding.active_remote_identity_id is not None:
            binding.content_status = "pending"
        binding.device_status = "not_requested"
        if (
            source_binding is not None
            and source_binding.active_remote_identity_id is not None
            and source_binding.remote_scheduled_for is not None
        ):
            source_binding.calendar_status = "pending"
        self.session.flush()
        if replacement_source is not None:
            source_revision = self._accepted_revision(replacement_source)
            self._event(
                replacement_source,
                source_revision,
                "unschedule",
                metadata={
                    "previous_date": replacement_date.isoformat() if replacement_date else None,
                    "source": "daily_adaptation_replacement",
                    "replacement_workout_id": workout.id,
                },
            )
            self._event(
                replacement_source,
                source_revision,
                "supersede",
                metadata={"replacement_workout_id": workout.id},
            )
        self._event(
            workout,
            revision,
            "accept",
            metadata={
                "validation_run_id": validation.id,
                "context_fingerprint": validation.context_fingerprint,
                "adaptation_validation_run_id": (
                    adaptation_validation.id if adaptation_validation is not None else None
                ),
            },
        )
        self.session.commit()
        if replacement_source is not None:
            self.session.refresh(replacement_source)
        self.session.refresh(workout)
        return workout

    def reject(self, workout_id: int, command: RejectRevisionCommand) -> Workout:
        workout = self.get(workout_id)
        if (
            workout.source_type not in {"coach_single", "coach_weekly_plan"}
            or workout.accepted_revision_id is not None
        ):
            raise WorkoutTransitionError(
                "Nur ein noch nicht angenommener Coach-Vorschlag kann abgelehnt werden.",
                code="workout.proposal_reject_invalid",
            )
        revision = self._current_revision(workout)
        identity = command.identity
        if (
            revision.id != identity.revision_id
            or revision.revision_number != identity.revision_number
            or revision.content_hash != identity.content_hash
        ):
            raise WorkoutConflictError(
                "Diese Workout-Revision ist nicht mehr aktuell.",
                code="workout.revision_stale",
            )
        if workout.approval_status == "rejected":
            return workout
        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                update(Workout)
                .where(
                    Workout.id == workout.id,
                    Workout.user_id == self.user.id,
                    Workout.current_revision_id == revision.id,
                    Workout.lock_version == identity.lock_version,
                    Workout.deleted_at.is_(None),
                )
                .values(
                    approval_status="rejected",
                    lock_version=Workout.lock_version + 1,
                )
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount != 1:
            self.session.rollback()
            raise WorkoutConflictError(
                "Das Workout wurde zwischenzeitlich geändert.", code="workout.lock_stale"
            )
        self._event(workout, revision, "reject")
        self.session.commit()
        self.session.refresh(workout)
        return workout

    def schedule(self, workout_id: int, command: ScheduleWorkoutCommand) -> Workout:
        workout = self.get(workout_id)
        self._ensure_generated_proposals_enabled(workout)
        accepted_replacement = self.session.scalar(
            select(Workout.id).where(
                Workout.user_id == self.user.id,
                Workout.replaces_workout_id == workout.id,
                Workout.accepted_revision_id.is_not(None),
                Workout.approval_status == "accepted",
                Workout.deleted_at.is_(None),
            )
        )
        if accepted_replacement is not None:
            raise WorkoutTransitionError(
                "Dieses Workout wurde bereits durch eine angenommene Anpassung ersetzt.",
                code="adaptation.original_superseded",
            )
        if workout.accepted_revision_id != command.revision_id:
            raise WorkoutConflictError(
                "Nur die exakt angenommene Revision kann eingeplant werden.",
                code="workout.accepted_revision_mismatch",
            )
        if (
            workout.local_schedule_status == "scheduled"
            and workout.scheduled_for == command.scheduled_for
        ):
            return workout
        revision = self._revision(workout, command.revision_id)
        if workout.source_type in {"coach_single", "coach_weekly_plan"}:
            if command.scheduled_for < utcnow().date():
                raise WorkoutTransitionError(
                    "Ein Workout kann nicht in die Vergangenheit eingeplant werden.",
                    code="workout.schedule_date_in_past",
                )
            if revision.suggested_for != command.scheduled_for:
                raise WorkoutConflictError(
                    "Der Termin entspricht nicht dem geprüften Vorschlagsdatum.",
                    code="workout.schedule_date_mismatch",
                )
        binding = self._binding(workout)
        self._ensure_garmin_state_known(binding)
        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                update(Workout)
                .where(
                    Workout.id == workout.id,
                    Workout.user_id == self.user.id,
                    Workout.accepted_revision_id == command.revision_id,
                    Workout.lock_version == command.expected_lock_version,
                    Workout.deleted_at.is_(None),
                )
                .values(
                    scheduled_for=command.scheduled_for,
                    local_schedule_status="scheduled",
                    lock_version=Workout.lock_version + 1,
                )
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount != 1:
            self.session.rollback()
            raise WorkoutConflictError(
                "Das Workout wurde zwischenzeitlich geändert.",
                code="workout.lock_stale",
            )
        if binding.active_remote_identity_id is not None:
            binding.calendar_status = "pending"
        self._event(
            workout,
            revision,
            "schedule",
            metadata={
                "previous_date": workout.scheduled_for.isoformat()
                if workout.scheduled_for
                else None,
                "scheduled_for": command.scheduled_for.isoformat(),
            },
        )
        self.session.commit()
        self.session.refresh(workout)
        return workout

    def unschedule(self, workout_id: int, command: UnscheduleWorkoutCommand) -> Workout:
        workout = self.get(workout_id)
        if workout.accepted_revision_id != command.revision_id:
            raise WorkoutConflictError(
                "Nur die exakt angenommene Revision kann aus dem Kalender entfernt werden.",
                code="workout.accepted_revision_mismatch",
            )
        if workout.local_schedule_status != "scheduled" and workout.scheduled_for is None:
            return workout
        binding = self._binding(workout)
        self._ensure_garmin_state_known(binding)
        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                update(Workout)
                .where(
                    Workout.id == workout.id,
                    Workout.user_id == self.user.id,
                    Workout.accepted_revision_id == command.revision_id,
                    Workout.lock_version == command.expected_lock_version,
                    Workout.deleted_at.is_(None),
                )
                .values(
                    scheduled_for=None,
                    local_schedule_status="cancelled",
                    lock_version=Workout.lock_version + 1,
                )
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount != 1:
            self.session.rollback()
            raise WorkoutConflictError(
                "Das Workout wurde zwischenzeitlich geändert.",
                code="workout.lock_stale",
            )
        if binding.active_remote_identity_id is not None:
            binding.calendar_status = "pending"
        revision = self._revision(workout, command.revision_id)
        self._event(workout, revision, "unschedule")
        self.session.commit()
        self.session.refresh(workout)
        return workout

    def publish(self, workout_id: int) -> Workout:
        workout = self.get(workout_id)
        revision = self._accepted_revision(workout)
        binding = self._binding(workout)
        self._ensure_garmin_state_known(binding, allow={"content", "calendar"})
        target_date = (
            workout.scheduled_for if workout.local_schedule_status == "scheduled" else None
        )
        retirement_required = (
            binding.remote_scheduled_for is not None and binding.remote_scheduled_for != target_date
        )
        if not retirement_required:
            self._ensure_generated_garmin_enabled(workout)
        account: GarminAccount | None = None
        if retirement_required:
            account = self._garmin_account()
            self._retire_remote_calendar(workout, revision, binding, account)
        if (
            target_date is None
            and binding.active_remote_identity_id is not None
            and binding.content_status == "synced"
        ):
            self.session.commit()
            return workout
        if workout.replaces_workout_id is not None:
            if account is None:
                account = self._garmin_account()
            self._retire_replaced_calendar(workout, account)
        self._ensure_generated_garmin_enabled(workout)
        self._validate_for_sync(workout, revision)
        if account is None:
            account = self._garmin_account()
        operations = GarminWorkoutOperations(
            self.session, account, connect_garmin=self.connect_garmin
        )
        execution = self._execution(workout, revision)
        identity = self._active_identity(binding, account)
        if identity is None:
            identity = operations.upload(
                workout,
                binding,
                revision,
                execution,
                on_uploaded=lambda: setattr(workout, "status", "published"),
            )
            execution = self._execution(workout, revision)
        elif binding.content_status != "synced":
            operations.update_content(workout, binding, revision, identity, execution)

        if identity is None:
            raise WorkoutTransitionError(
                "Garmin hat keine aktive Workout-ID geliefert.",
                code="garmin.remote_id_required",
            )
        previous_date = binding.remote_scheduled_for
        target_date = (
            workout.scheduled_for if workout.local_schedule_status == "scheduled" else None
        )
        if previous_date is not None and previous_date != target_date:
            operations.unschedule(workout, binding, revision, identity, previous_date)
        if target_date is not None and (
            binding.remote_scheduled_for != target_date or binding.calendar_status != "synced"
        ):
            operations.schedule(workout, binding, revision, identity, target_date)
        elif target_date is None:
            binding.calendar_status = "not_requested"
        workout.status = "published"
        self._event(
            workout,
            revision,
            "publish",
            idempotency_key=(f"publish:{workout.id}:{revision.id}:{workout.lock_version}"),
        )
        self.session.commit()
        return workout

    def _retire_replaced_calendar(self, replacement: Workout, account: GarminAccount) -> None:
        if replacement.replaces_workout_id is None:
            return
        original = self.get(replacement.replaces_workout_id)
        if (
            replacement.accepted_revision_id is None
            or replacement.local_schedule_status != "scheduled"
            or original.local_schedule_status != "cancelled"
            or original.scheduled_for is not None
        ):
            raise WorkoutConflictError(
                "Der lokale Workout-Ersatz ist nicht vollständig angenommen.",
                code="adaptation.replacement_state_invalid",
            )
        original_revision = self._accepted_revision(original)
        original_binding = self._binding(original)
        self._ensure_garmin_state_known(original_binding, allow={"calendar"})
        self._retire_remote_calendar(original, original_revision, original_binding, account)

    def _retire_remote_calendar(
        self,
        workout: Workout,
        revision: WorkoutRevision,
        binding: WorkoutGarminBinding,
        account: GarminAccount,
    ) -> None:
        identity = self._active_identity(binding, account)
        if identity is None:
            if binding.calendar_status == "unknown":
                raise WorkoutTransitionError(
                    "Der Garmin-Kalenderzustand des Workouts ist unklar.",
                    code="garmin.state_unknown",
                )
            binding.calendar_status = "not_requested"
            return
        remote_date = binding.remote_scheduled_for
        if remote_date is None:
            binding.calendar_status = "not_requested"
            return
        GarminWorkoutOperations(
            self.session, account, connect_garmin=self.connect_garmin
        ).unschedule(workout, binding, revision, identity, remote_date)

    def push(self, workout_id: int) -> Workout:
        workout = self.get(workout_id)
        self._ensure_generated_garmin_enabled(workout)
        revision = self._accepted_revision(workout)
        binding = self._binding(workout)
        self._validate_for_sync(workout, revision)
        if binding.content_status != "synced":
            raise WorkoutTransitionError(
                "Das angenommene Workout muss zuerst an Garmin übertragen werden.",
                code="garmin.content_not_synced",
            )
        if binding.calendar_status == "pending" or (
            workout.local_schedule_status == "scheduled" and binding.calendar_status != "synced"
        ):
            raise WorkoutTransitionError(
                "Die Garmin-Kalenderänderung muss zuerst abgeschlossen werden.",
                code="garmin.calendar_not_synced",
            )
        execution = self._execution(workout, revision)
        identity = self._active_identity(binding)
        if identity is None:
            raise WorkoutTransitionError(
                "Das Workout besitzt keine aktive Garmin-ID.",
                code="garmin.remote_id_required",
            )
        account = self._garmin_account()
        self._ensure_identity_principal(identity, account)
        operation = GarminWorkoutOperations(
            self.session, account, connect_garmin=self.connect_garmin
        ).push(
            workout,
            binding,
            revision,
            identity,
            execution,
            on_accepted=lambda: setattr(workout, "status", "pushed"),
        )
        if binding.device_status == "request_accepted":
            self._event(
                workout,
                revision,
                "push",
                idempotency_key=operation.idempotency_key,
            )
            self.session.commit()
        return workout

    def delete(self, workout_id: int) -> None:
        workout = self.get(workout_id)
        active_replacement = self.session.scalar(
            select(Workout.id).where(
                Workout.user_id == self.user.id,
                Workout.replaces_workout_id == workout.id,
                Workout.approval_status.in_({"proposed", "accepted"}),
                Workout.deleted_at.is_(None),
            )
        )
        if active_replacement is not None:
            raise WorkoutTransitionError(
                "Das Original kann nicht gelöscht werden, solange ein Ersatz aktiv ist.",
                code="adaptation.replacement_active",
            )
        binding = self._binding(workout)
        revision = (
            self._revision(workout, workout.accepted_revision_id)
            if workout.accepted_revision_id
            else self._current_revision(workout)
        )
        self._ensure_garmin_state_known(binding, allow={"calendar"})
        identity = self._active_identity(binding)
        if identity is not None:
            account = self._garmin_account()
            self._ensure_identity_principal(identity, account)
            operations = GarminWorkoutOperations(
                self.session, account, connect_garmin=self.connect_garmin
            )
            remote_date = binding.remote_scheduled_for
            if remote_date is not None:
                operations.unschedule_before_delete(
                    workout, binding, revision, identity, remote_date
                )
            operations.delete(workout, binding, revision, identity)
        workout.deleted_at = utcnow()
        workout.lock_version += 1
        self._event(workout, revision, "delete")
        self.session.commit()

    def validate_revision_context(
        self, workout_id: int, revision_id: int, context_fingerprint: str
    ) -> WorkoutValidationRun:
        workout = self.get(workout_id)
        revision = self._revision(workout, revision_id)
        safety_context = self._safety_context(workout, revision)
        run = self._validate_context(
            workout,
            revision,
            SafetyContext(
                fingerprint=context_fingerprint,
                feedback_ids=safety_context.feedback_ids,
                report=safety_context.report,
            ),
        )
        self.session.commit()
        return run

    def acceptance_context(self, workout_id: int) -> SafetyContext:
        workout = self.get(workout_id)
        return self._safety_context(workout, self._current_revision(workout))

    def sync_context(self, workout_id: int) -> SafetyContext:
        workout = self.get(workout_id)
        revision = (
            self._revision(workout, workout.accepted_revision_id)
            if workout.accepted_revision_id is not None
            else self._current_revision(workout)
        )
        return self._safety_context(workout, revision, mode="sync")

    def _create_revision(
        self,
        workout: Workout,
        data: WorkoutInput,
        *,
        revision_number: int,
        parent_revision_id: int | None,
        metadata: RevisionMetadata | None = None,
    ) -> WorkoutRevision:
        details = metadata or RevisionMetadata()
        revision = WorkoutRevision(
            workout_id=workout.id,
            revision_number=revision_number,
            parent_revision_id=parent_revision_id,
            name=data.name,
            sport=data.sport,
            suggested_for=data.scheduled_for,
            description=data.description or None,
            definition_version=data.definition_version,
            definition=definition_to_json(data.definition),
            purpose=details.purpose,
            guidance_json=details.guidance_json,
            load_estimate_json=details.load_estimate_json,
            validation_report_json=(
                details.validation_report_json or structural_validation_report()
            ),
            generation_context_json=details.generation_context_json,
            source_type=details.source_type,
            generator_version=details.generator_version,
            template_id=details.template_id,
            template_version=details.template_version,
            rule_set_version=details.rule_set_version,
            knowledge_base_version=details.knowledge_base_version,
            model_provider=details.model_provider,
            model_id=details.model_id,
            prompt_template_version=details.prompt_template_version,
            content_hash="",
            edit_source=details.edit_source,
        )
        revision.content_hash = (
            workout_content_hash(data) if metadata is None else revision_content_hash(revision)
        )
        self.session.add(revision)
        return revision

    def _materialize(self, workout: Workout, revision: WorkoutRevision) -> None:
        workout.name = revision.name
        workout.sport = revision.sport
        workout.description = revision.description
        workout.definition_version = revision.definition_version
        workout.definition = revision.definition

    def _current_revision(self, workout: Workout) -> WorkoutRevision:
        if workout.current_revision_id is None:
            raise WorkoutTransitionError(
                "Das Workout besitzt keine aktuelle Revision.",
                code="workout.current_revision_missing",
            )
        return self._revision(workout, workout.current_revision_id)

    def _accepted_revision(self, workout: Workout) -> WorkoutRevision:
        if workout.accepted_revision_id is None:
            raise WorkoutTransitionError(
                "Bitte den Entwurf vor der Übertragung bestätigen.",
                code="workout.confirmation_required",
            )
        return self._revision(workout, workout.accepted_revision_id)

    def _revision(self, workout: Workout, revision_id: int) -> WorkoutRevision:
        revision = self.session.scalar(
            select(WorkoutRevision).where(
                WorkoutRevision.id == revision_id,
                WorkoutRevision.workout_id == workout.id,
            )
        )
        if revision is None:
            raise WorkoutConflictError(
                "Die angegebene Workout-Revision gehört nicht zu diesem Workout.",
                code="workout.revision_mismatch",
            )
        return revision

    @staticmethod
    def _verify_revision_identity(
        workout: Workout, revision: WorkoutRevision, identity: RevisionIdentity
    ) -> None:
        if (
            revision.id != identity.revision_id
            or revision.revision_number != identity.revision_number
            or revision.content_hash != identity.content_hash
            or workout.lock_version != identity.lock_version
        ):
            raise WorkoutConflictError(
                "Diese Workout-Revision ist nicht mehr aktuell.",
                code="workout.revision_stale",
            )

    def _adaptation_event(self, action: str, idempotency_key: str) -> WorkoutEvent | None:
        return self.session.scalar(
            select(WorkoutEvent).where(
                WorkoutEvent.owner_user_id == self.user.id,
                WorkoutEvent.action == action,
                WorkoutEvent.idempotency_key == idempotency_key,
            )
        )

    @staticmethod
    def _verify_adaptation_replay(event: WorkoutEvent, workout: Workout, request_hash: str) -> None:
        if (
            event.workout_id != workout.id
            or event.safe_metadata_json.get("request_hash") != request_hash
        ):
            raise WorkoutConflictError(
                "Dieser Wiederholungsschlüssel gehört zu einer anderen Anpassung.",
                code="adaptation.idempotency_conflict",
            )

    @staticmethod
    def _adaptation_request_hash(
        data: WorkoutInput, *, adaptation_class: str, context_fingerprint: str
    ) -> str:
        payload = {
            "adaptation_class": adaptation_class,
            "context_fingerprint": context_fingerprint,
            "content_hash": workout_content_hash(data),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _adaptation_decision_hash(
        adaptation_class: str,
        context_fingerprint: str,
        identity: RevisionIdentity,
    ) -> str:
        payload = {
            "adaptation_class": adaptation_class,
            "context_fingerprint": context_fingerprint,
            "revision_id": identity.revision_id,
            "revision_number": identity.revision_number,
            "content_hash": identity.content_hash,
            "lock_version": identity.lock_version,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _binding(self, workout: Workout) -> WorkoutGarminBinding:
        binding = self.session.scalar(
            select(WorkoutGarminBinding)
            .where(WorkoutGarminBinding.workout_id == workout.id)
            .execution_options(populate_existing=True)
        )
        if binding is None:
            binding = WorkoutGarminBinding(workout_id=workout.id)
            self.session.add(binding)
            self.session.flush()
        return binding

    def _remote_id(self, binding: WorkoutGarminBinding) -> str | None:
        identity = self._active_identity(binding)
        return identity.garmin_workout_id if identity is not None else None

    def _active_identity(
        self,
        binding: WorkoutGarminBinding,
        account: GarminAccount | None = None,
    ) -> WorkoutGarminRemoteIdentity | None:
        if binding.active_remote_identity_id is None:
            return None
        identity = self.session.get(WorkoutGarminRemoteIdentity, binding.active_remote_identity_id)
        if identity is not None and account is not None:
            self._ensure_identity_principal(identity, account)
        return identity

    def _ensure_identity_principal(
        self, identity: WorkoutGarminRemoteIdentity, account: GarminAccount
    ) -> None:
        if (
            identity.garmin_account_id != account.id
            or identity.principal_fingerprint != account.principal_fingerprint
        ):
            raise WorkoutTransitionError(
                "Die Garmin-ID gehört zu einer früheren Kontoverbindung und muss geprüft werden.",
                code="garmin.principal_mismatch",
            )

    def _ensure_garmin_state_known(
        self, binding: WorkoutGarminBinding, *, allow: set[str] | None = None
    ) -> None:
        allowed = allow or set()
        states = {
            "content": binding.content_status,
            "calendar": binding.calendar_status,
            "device": binding.device_status,
        }
        if any(
            state in {"pending", "unknown"} for axis, state in states.items() if axis not in allowed
        ):
            raise WorkoutTransitionError(
                "Der Garmin-Zustand muss vor einer weiteren Änderung geprüft werden.",
                code="garmin.state_unknown",
            )

    def _ensure_generated_garmin_enabled(self, workout: Workout) -> None:
        revision = (
            self._revision(workout, workout.accepted_revision_id)
            if workout.accepted_revision_id is not None
            else self._current_revision(workout)
        )
        if revision.source_type not in {
            "coach_single",
            "coach_daily_adaptation",
            "coach_weekly_plan",
        }:
            return
        from app.config import (
            DEFERRED_QUALITY_TEMPLATE_IDS,
            coach_feature_enabled,
            deferred_quality_templates_enabled,
            get_settings,
        )

        settings = get_settings()
        source_enabled = (
            coach_feature_enabled(settings.coach_daily_adaptation_enabled, self.user.id)
            if revision.source_type == "coach_daily_adaptation"
            else coach_feature_enabled(settings.coach_plan_generation_enabled, self.user.id)
            if revision.source_type == "coach_weekly_plan"
            else coach_feature_enabled(settings.coach_workout_proposals_enabled, self.user.id)
        )
        if not source_enabled:
            raise WorkoutTransitionError(
                "Die erzeugende Coach-Funktion ist derzeit deaktiviert.",
                code="coach.source_feature_disabled",
            )
        if not coach_feature_enabled(settings.coach_garmin_sync_enabled, self.user.id):
            raise WorkoutTransitionError(
                "Die Garmin-Übertragung für Coach-Vorschläge ist noch nicht freigeschaltet.",
                code="coach.garmin_sync_disabled",
            )
        if (
            revision.template_id in DEFERRED_QUALITY_TEMPLATE_IDS
            and not deferred_quality_templates_enabled()
        ):
            raise WorkoutTransitionError(
                "Development-Qualitätstemplates können außerhalb des Testmodus nicht "
                "übertragen werden.",
                code="coach.deferred_quality_disabled",
            )
        from app.services.planning.workout_proposals import (
            QUALITY_TEMPLATE_IDS,
            quality_density_conflicts,
        )

        if revision.template_id in QUALITY_TEMPLATE_IDS:
            quality_date = workout.scheduled_for or revision.suggested_for
            if quality_date is None:
                raise WorkoutTransitionError(
                    "Für eine Qualitätseinheit fehlt das vorgesehene Datum.",
                    code="proposal.quality_date_required",
                )
            if quality_density_conflicts(
                self.session,
                self.user.id,
                quality_date,
                exclude_workout_id=workout.id,
            ):
                raise WorkoutTransitionError(
                    "Zu einer angenommenen Qualitätseinheit fehlen mindestens 48 Stunden Abstand.",
                    code="proposal.quality_spacing_violation",
                )

    def _ensure_generated_proposals_enabled(self, workout: Workout) -> None:
        from app.config import (
            DEFERRED_QUALITY_TEMPLATE_IDS,
            coach_feature_enabled,
            deferred_quality_templates_enabled,
            get_settings,
        )

        revision = self._current_revision(workout)
        settings = get_settings()
        if (
            revision.template_id in DEFERRED_QUALITY_TEMPLATE_IDS
            and not deferred_quality_templates_enabled()
        ):
            raise WorkoutTransitionError(
                "Aktionen für Development-Qualitätstemplates sind derzeit deaktiviert.",
                code="coach.deferred_quality_disabled",
            )
        from app.services.planning.workout_proposals import (
            QUALITY_TEMPLATE_IDS,
            quality_density_conflicts,
        )

        if revision.template_id in QUALITY_TEMPLATE_IDS:
            quality_date = workout.scheduled_for or revision.suggested_for
            if quality_date is None:
                raise WorkoutTransitionError(
                    "Für eine Qualitätseinheit fehlt das vorgesehene Datum.",
                    code="proposal.quality_date_required",
                )
            if quality_density_conflicts(
                self.session,
                self.user.id,
                quality_date,
                exclude_workout_id=workout.id,
            ):
                raise WorkoutTransitionError(
                    "Zu einer angenommenen Qualitätseinheit fehlen mindestens 48 Stunden Abstand.",
                    code="proposal.quality_spacing_violation",
                )
        if revision.source_type == "coach_daily_adaptation":
            if not coach_feature_enabled(settings.coach_daily_adaptation_enabled, self.user.id):
                raise WorkoutTransitionError(
                    "Aktionen für tägliche Anpassungen sind derzeit deaktiviert.",
                    code="adaptation.feature_disabled",
                )
            return
        if revision.source_type == "coach_weekly_plan":
            if not coach_feature_enabled(settings.coach_plan_generation_enabled, self.user.id):
                raise WorkoutTransitionError(
                    "Aktionen für Wochenplan-Vorschläge sind derzeit deaktiviert.",
                    code="plan.feature_disabled",
                )
            return
        if revision.source_type == "coach_single" and not coach_feature_enabled(
            settings.coach_workout_proposals_enabled, self.user.id
        ):
            raise WorkoutTransitionError(
                "Aktionen für Coach-Vorschläge sind derzeit deaktiviert.",
                code="coach.workout_proposals_disabled",
            )

    def _ensure_daily_adaptation_enabled(self) -> None:
        from app.config import coach_feature_enabled, get_settings

        if not coach_feature_enabled(get_settings().coach_daily_adaptation_enabled, self.user.id):
            raise WorkoutTransitionError(
                "Die tägliche Trainingsanpassung ist noch nicht freigeschaltet.",
                code="adaptation.feature_disabled",
            )

    def _execution(self, workout: Workout, revision: WorkoutRevision) -> AcceptedWorkoutExecution:
        binding = self._binding(workout)
        return AcceptedWorkoutExecution(
            workout_id=workout.id,
            revision_id=revision.id,
            revision_number=revision.revision_number,
            name=revision.name,
            sport=revision.sport,
            description=revision.description,
            definition_version=revision.definition_version,
            definition=revision.definition,
            scheduled_for=(
                workout.scheduled_for if workout.local_schedule_status == "scheduled" else None
            ),
            garmin_workout_id=self._remote_id(binding),
        )

    def _validate_for_sync(self, workout: Workout, revision: WorkoutRevision) -> None:
        if workout.accepted_revision_id != revision.id or workout.deleted_at is not None:
            raise WorkoutConflictError(
                "Die angenommene Workout-Revision ist nicht mehr aktuell.",
                code="workout.accepted_revision_mismatch",
            )
        if revision.source_type in {"coach_single", "coach_daily_adaptation"}:
            from app.services.planning.workout_proposals import (
                ensure_easy_run_device_target_current,
            )

            ensure_easy_run_device_target_current(self.session, self.user.id, revision)
        validation = self._validate_context(
            workout,
            revision,
            self._safety_context(workout, revision, mode="sync"),
        )
        if not validation.valid:
            outcome = str(validation.report_json.get("outcome", "clarify"))
            message = (
                "Ein neuer Sicherheitshinweis blockiert die Übertragung dieses Lauftrainings."
                if outcome == TriageOutcome.SAFETY_STOP.value
                else "Vor der Übertragung fehlen noch eindeutige Sicherheitsangaben."
            )
            self.session.commit()
            raise WorkoutTransitionError(
                message,
                code="workout.validation_failed",
            )

    def _safety_context(
        self,
        workout: Workout,
        revision: WorkoutRevision,
        *,
        mode: ValidationMode = "acceptance",
    ) -> SafetyContext:
        return build_safety_context(
            self.session,
            self.user.id,
            workout,
            revision,
            mode=mode,
        )

    def _validate_context(
        self,
        workout: Workout,
        revision: WorkoutRevision,
        safety_context: SafetyContext,
        validation_kind: str = "contextual",
        force: bool = False,
    ) -> WorkoutValidationRun:
        now = utcnow()
        existing = (
            None
            if force
            else self.session.scalar(
                select(WorkoutValidationRun)
                .where(
                    WorkoutValidationRun.workout_id == workout.id,
                    WorkoutValidationRun.revision_id == revision.id,
                    WorkoutValidationRun.validation_kind == validation_kind,
                    WorkoutValidationRun.rule_set_version == SAFETY_RULE_SET_VERSION,
                    WorkoutValidationRun.context_fingerprint == safety_context.fingerprint,
                    WorkoutValidationRun.expires_at > now,
                )
                .order_by(WorkoutValidationRun.evaluated_at.desc())
            )
        )
        if existing is not None:
            return existing
        run = WorkoutValidationRun(
            workout_id=workout.id,
            revision_id=revision.id,
            validation_kind=validation_kind,
            rule_set_version=SAFETY_RULE_SET_VERSION,
            context_fingerprint=safety_context.fingerprint,
            feedback_ids_json=list(safety_context.feedback_ids),
            evaluated_at=now,
            expires_at=now + timedelta(hours=1),
            valid=safety_context.report.valid,
            report_json=safety_context.report.to_json(),
        )
        self.session.add(run)
        return run

    def _record_structural_validation(
        self, workout: Workout, revision: WorkoutRevision
    ) -> WorkoutValidationRun:
        run = WorkoutValidationRun(
            workout_id=workout.id,
            revision_id=revision.id,
            validation_kind="structural",
            rule_set_version=STRUCTURAL_RULE_SET_VERSION,
            context_fingerprint=revision.content_hash,
            feedback_ids_json=[],
            evaluated_at=utcnow(),
            expires_at=None,
            valid=True,
            report_json=revision.validation_report_json or structural_validation_report(),
        )
        self.session.add(run)
        return run

    @staticmethod
    def _change_labels(parent: WorkoutRevision, current: WorkoutRevision) -> tuple[str, ...]:
        labels: list[str] = []
        for label, old, new in (
            ("name", parent.name, current.name),
            ("sport", parent.sport, current.sport),
            ("suggested_for", parent.suggested_for, current.suggested_for),
            ("description", parent.description, current.description),
            ("definition", parent.definition, current.definition),
            ("purpose", parent.purpose, current.purpose),
            ("guidance", parent.guidance_json, current.guidance_json),
            ("load_estimate", parent.load_estimate_json, current.load_estimate_json),
        ):
            if old != new:
                labels.append(label)
        return tuple(labels)

    def _event(
        self,
        workout: Workout,
        revision: WorkoutRevision | None,
        action: str,
        *,
        metadata: dict[str, object] | None = None,
        idempotency_key: str | None = None,
        skip_existing: bool = True,
    ) -> None:
        if (
            skip_existing
            and idempotency_key is not None
            and self.session.scalar(
                select(WorkoutEvent.id).where(
                    WorkoutEvent.owner_user_id == workout.user_id,
                    WorkoutEvent.action == action,
                    WorkoutEvent.idempotency_key == idempotency_key,
                )
            )
        ):
            return
        self.session.add(
            WorkoutEvent(
                workout_id=workout.id,
                revision_id=revision.id if revision else None,
                owner_user_id=workout.user_id,
                actor_type="user",
                actor_user_id=self.user.id,
                action=action,
                request_id=self.request_id,
                idempotency_key=idempotency_key,
                safe_metadata_json=metadata or {},
            )
        )

    def _garmin_account(self) -> GarminAccount:
        account = get_or_create_garmin_account(self.session, self.user)
        if account.connected_at is None:
            raise GarminUnavailableError("Garmin ist noch nicht verbunden.")
        return account
