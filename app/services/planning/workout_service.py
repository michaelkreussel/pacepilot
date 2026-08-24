from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    GarminAccount,
    User,
    Workout,
    WorkoutEvent,
    WorkoutGarminBinding,
    WorkoutGarminOperation,
    WorkoutGarminRemoteIdentity,
    WorkoutRevision,
    WorkoutValidationRun,
)
from app.models.user import utcnow
from app.repositories.users import get_or_create_garmin_account
from app.repositories.workouts import find_workout
from app.services.garmin.client import GarminUnavailableError, connect_garmin_account
from app.services.garmin.locks import GarminAccountBusyError, garmin_account_slot
from app.services.garmin.workout_export import (
    delete_remote_workout,
    push_workout,
    schedule_workout_on_date,
    scheduled_workout_ids,
    unschedule_workout_on_date,
    update_workout_content,
    upload_workout,
)
from app.services.garmin.workout_operations import GarminWorkoutOperationRunner
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

type GarminConnector = Callable[[Session, GarminAccount], Any]


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
    ) -> Workout:
        existing = self.idempotent_proposal(
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if existing is not None:
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
        )
        self.session.add(workout)
        self.session.flush()
        revision = self._create_revision(
            workout,
            data,
            revision_number=1,
            parent_revision_id=None,
            metadata=metadata,
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
        )
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
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            winner = self.idempotent_proposal(
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if winner is not None:
                return winner
            raise
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
        request_hash = workout_content_hash(data)
        if workout.source_type == "coach_single" and idempotency_key:
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
        current = self._current_revision(workout)
        metadata = None
        if workout.source_type == "coach_single":
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
        if workout.source_type == "coach_single":
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
            return workout
        if workout.approval_status == "rejected":
            raise WorkoutTransitionError(
                "Ein abgelehnter Vorschlag muss vor der Annahme bearbeitet werden.",
                code="workout.proposal_rejected",
            )
        if workout.source_type == "coach_single":
            from app.services.planning.workout_proposals import (
                ensure_easy_run_device_target_current,
            )

            ensure_easy_run_device_target_current(self.session, self.user.id, revision)
        binding = self._binding(workout)
        self._ensure_garmin_state_known(binding)
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
        self.session.flush()
        self._event(
            workout,
            revision,
            "accept",
            metadata={
                "validation_run_id": validation.id,
                "context_fingerprint": validation.context_fingerprint,
            },
        )
        self.session.commit()
        self.session.refresh(workout)
        return workout

    def reject(self, workout_id: int, command: RejectRevisionCommand) -> Workout:
        workout = self.get(workout_id)
        if workout.source_type != "coach_single" or workout.accepted_revision_id is not None:
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
        if workout.source_type == "coach_single":
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
        self._ensure_generated_garmin_enabled(workout)
        revision = self._accepted_revision(workout)
        binding = self._binding(workout)
        self._ensure_garmin_state_known(binding, allow={"content", "calendar"})
        self._validate_for_sync(workout, revision)
        account = self._garmin_account()
        runner = GarminWorkoutOperationRunner(self.session, account)
        execution = self._execution(workout, revision)
        identity = self._active_identity(binding, account)
        if identity is None:

            def record_upload(remote_id: str, operation: WorkoutGarminOperation) -> None:
                identity = WorkoutGarminRemoteIdentity(
                    binding_id=binding.id,
                    garmin_account_id=account.id,
                    garmin_workout_id=remote_id,
                    principal_fingerprint=account.principal_fingerprint,
                    status="active",
                )
                self.session.add(identity)
                self.session.flush()
                binding.active_remote_identity_id = identity.id
                binding.content_status = "synced"
                workout.garmin_workout_id = remote_id
                workout.status = "published"
                operation.remote_reference = remote_id

            runner.execute(
                workout=workout,
                binding=binding,
                revision=revision,
                operation_type="upload",
                remote_identity=None,
                call=lambda: self._garmin_call(
                    account, "workout.upload", lambda client: upload_workout(client, execution)
                ),
                on_success=record_upload,
            )
            identity = self._active_identity(binding, account)
            execution = self._execution(workout, revision)
        elif binding.content_status != "synced":
            runner.execute(
                workout=workout,
                binding=binding,
                revision=revision,
                operation_type="update",
                remote_identity=identity,
                call=lambda: self._garmin_call(
                    account,
                    "workout.update",
                    lambda client: update_workout_content(client, execution),
                ),
                on_success=lambda _result, _operation: setattr(binding, "content_status", "synced"),
            )

        if identity is None:
            raise WorkoutTransitionError(
                "Garmin hat keine aktive Workout-ID geliefert.",
                code="garmin.remote_id_required",
            )
        remote_id = identity.garmin_workout_id
        previous_date = binding.remote_scheduled_for
        target_date = (
            workout.scheduled_for if workout.local_schedule_status == "scheduled" else None
        )
        if previous_date is not None and previous_date != target_date:

            def record_unschedule(_result: None, _operation: WorkoutGarminOperation) -> None:
                binding.remote_scheduled_for = None
                binding.calendar_status = "not_requested"

            runner.execute(
                workout=workout,
                binding=binding,
                revision=revision,
                operation_type="unschedule",
                remote_identity=identity,
                scheduled_for=previous_date,
                call=lambda: self._garmin_call(
                    account,
                    "workout.unschedule",
                    lambda client: unschedule_workout_on_date(client, remote_id, previous_date),
                ),
                reconcile=lambda: self._garmin_call(
                    account,
                    "workout.unschedule.reconcile",
                    lambda client: not scheduled_workout_ids(client, remote_id, previous_date),
                ),
                on_success=record_unschedule,
                on_reconciled=lambda operation: record_unschedule(None, operation),
            )
        if target_date is not None and (
            binding.remote_scheduled_for != target_date or binding.calendar_status != "synced"
        ):

            def record_schedule(_result: None, _operation: WorkoutGarminOperation) -> None:
                binding.remote_scheduled_for = target_date
                binding.calendar_status = "synced"

            runner.execute(
                workout=workout,
                binding=binding,
                revision=revision,
                operation_type="schedule",
                remote_identity=identity,
                scheduled_for=target_date,
                call=lambda: self._garmin_call(
                    account,
                    "workout.schedule",
                    lambda client: schedule_workout_on_date(client, remote_id, target_date),
                ),
                reconcile=lambda: self._garmin_call(
                    account,
                    "workout.schedule.reconcile",
                    lambda client: bool(scheduled_workout_ids(client, remote_id, target_date)),
                ),
                on_success=record_schedule,
                on_reconciled=lambda operation: record_schedule(None, operation),
            )
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
        runner = GarminWorkoutOperationRunner(self.session, account)

        def record_push(_result: None, _operation: WorkoutGarminOperation) -> None:
            workout.status = "pushed"
            binding.device_status = "request_accepted"

        operation = runner.execute(
            workout=workout,
            binding=binding,
            revision=revision,
            operation_type="push",
            remote_identity=identity,
            call=lambda: self._garmin_call(
                account, "workout.push", lambda client: push_workout(client, execution)
            ),
            on_success=record_push,
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
            runner = GarminWorkoutOperationRunner(self.session, account)
            remote_id = identity.garmin_workout_id
            remote_date = binding.remote_scheduled_for
            if remote_date is not None:
                runner.execute(
                    workout=workout,
                    binding=binding,
                    revision=revision,
                    operation_type="unschedule",
                    remote_identity=identity,
                    scheduled_for=remote_date,
                    call=lambda: self._garmin_call(
                        account,
                        "workout.unschedule",
                        lambda client: unschedule_workout_on_date(client, remote_id, remote_date),
                    ),
                    reconcile=lambda: self._garmin_call(
                        account,
                        "workout.unschedule.reconcile",
                        lambda client: not scheduled_workout_ids(client, remote_id, remote_date),
                    ),
                    on_success=lambda _result, _operation: setattr(
                        binding, "remote_scheduled_for", None
                    ),
                    on_reconciled=lambda _operation: setattr(binding, "remote_scheduled_for", None),
                )

            def record_delete(_result: None, _operation: WorkoutGarminOperation) -> None:
                identity.status = "removed"
                identity.removed_at = utcnow()
                binding.active_remote_identity_id = None
                binding.content_status = "removed"
                binding.calendar_status = "removed"
                binding.device_status = "not_requested"

            runner.execute(
                workout=workout,
                binding=binding,
                revision=revision,
                operation_type="delete",
                remote_identity=identity,
                call=lambda: self._garmin_call(
                    account,
                    "workout.delete",
                    lambda client: delete_remote_workout(client, remote_id),
                ),
                on_success=record_delete,
            )
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
        if workout.source_type != "coach_single":
            return
        from app.config import get_settings

        if not get_settings().coach_garmin_sync_enabled:
            raise WorkoutTransitionError(
                "Die Garmin-Übertragung für Coach-Vorschläge ist noch nicht freigeschaltet.",
                code="coach.garmin_sync_disabled",
            )

    def _ensure_generated_proposals_enabled(self, workout: Workout) -> None:
        if workout.source_type != "coach_single":
            return
        from app.config import get_settings

        if not get_settings().coach_workout_proposals_enabled:
            raise WorkoutTransitionError(
                "Aktionen für Coach-Vorschläge sind derzeit deaktiviert.",
                code="coach.workout_proposals_disabled",
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
        if workout.source_type == "coach_single":
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
    ) -> None:
        if idempotency_key is not None and self.session.scalar(
            select(WorkoutEvent.id).where(
                WorkoutEvent.owner_user_id == workout.user_id,
                WorkoutEvent.action == action,
                WorkoutEvent.idempotency_key == idempotency_key,
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

    @contextmanager
    def _garmin_client(self) -> Iterator[tuple[GarminAccount, Any]]:
        account = self._garmin_account()
        try:
            with garmin_account_slot(account.id):
                yield account, self.connect_garmin(self.session, account)
        except GarminAccountBusyError as exc:
            raise GarminUnavailableError(
                "Für dieses Garmin-Konto läuft gerade eine andere Operation."
            ) from exc

    def _garmin_account(self) -> GarminAccount:
        account = get_or_create_garmin_account(self.session, self.user)
        if account.connected_at is None:
            raise GarminUnavailableError("Garmin ist noch nicht verbunden.")
        return account

    def _garmin_call[T](
        self, account: GarminAccount, operation: str, call: Callable[[Any], T]
    ) -> T:
        from app.config import get_settings
        from app.services.garmin.health_backfill import GarminPacer

        settings = get_settings()
        pacer = GarminPacer(
            settings.garmin_call_delay_seconds,
            {"garmin_account_id": account.id, "workout_operation": operation},
            rate_limit_cooldown=settings.garmin_rate_limit_cooldown_seconds,
        )
        with self._garmin_client() as (_account, client):
            return pacer.call(operation, lambda: call(client))
