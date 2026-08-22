from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
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
from app.services.planning.validator import WorkoutInput, validate_workout
from app.services.planning.workout_definition import definition_to_json
from app.services.planning.workout_revision import (
    STRUCTURAL_RULE_SET_VERSION,
    AcceptedWorkoutExecution,
    AcceptRevisionCommand,
    ScheduleWorkoutCommand,
    UnscheduleWorkoutCommand,
    default_context_fingerprint,
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
            definition_version=1,
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
        workout.current_revision_id = revision.id
        workout.materialized_revision_id = revision.id
        self.session.add(WorkoutGarminBinding(workout_id=workout.id))
        self._event(workout, revision, "create")
        self._validate_context(
            workout,
            revision,
            default_context_fingerprint(revision.content_hash),
        )
        self.session.commit()
        return workout

    def update(self, workout_id: int, data: WorkoutInput) -> Workout:
        workout = self.get(workout_id)
        self.validate(data)
        current = self._current_revision(workout)
        revision = self._create_revision(
            workout,
            data,
            revision_number=current.revision_number + 1,
            parent_revision_id=current.id,
        )
        self.session.flush()
        workout.current_revision_id = revision.id
        workout.approval_status = "proposed" if workout.accepted_revision_id else "draft"
        workout.lock_version += 1
        if workout.accepted_revision_id is None:
            workout.materialized_revision_id = revision.id
            self._materialize(workout, revision)
        self._event(workout, revision, "revise")
        self._validate_context(
            workout,
            revision,
            default_context_fingerprint(revision.content_hash),
        )
        self.session.commit()
        return workout

    def confirm(self, workout_id: int, command: AcceptRevisionCommand) -> Workout:
        return self.accept(workout_id, command)

    def accept(self, workout_id: int, command: AcceptRevisionCommand) -> Workout:
        workout = self.get(workout_id)
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
        if command.context_fingerprint != default_context_fingerprint(revision.content_hash):
            raise WorkoutConflictError(
                "Der Prüfkontext dieser Workout-Revision ist nicht mehr aktuell.",
                code="workout.validation_context_stale",
            )
        if workout.accepted_revision_id == revision.id and workout.approval_status == "accepted":
            return workout
        binding = self._binding(workout)
        self._ensure_garmin_state_known(binding)
        validation = self._validate_context(workout, revision, command.context_fingerprint)
        if not validation.valid:
            raise WorkoutTransitionError(
                "Diese Workout-Revision kann nicht angenommen werden.",
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
        self._event(workout, revision, "accept")
        self.session.commit()
        self.session.refresh(workout)
        return workout

    def schedule(self, workout_id: int, command: ScheduleWorkoutCommand) -> Workout:
        workout = self.get(workout_id)
        if workout.accepted_revision_id != command.revision_id:
            raise WorkoutConflictError(
                "Nur die exakt angenommene Revision kann eingeplant werden.",
                code="workout.accepted_revision_mismatch",
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
        revision = self._revision(workout, command.revision_id)
        self._event(workout, revision, "schedule")
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
        run = self._validate_context(workout, revision, context_fingerprint)
        self.session.commit()
        return run

    def _create_revision(
        self,
        workout: Workout,
        data: WorkoutInput,
        *,
        revision_number: int,
        parent_revision_id: int | None,
    ) -> WorkoutRevision:
        revision = WorkoutRevision(
            workout_id=workout.id,
            revision_number=revision_number,
            parent_revision_id=parent_revision_id,
            name=data.name,
            sport=data.sport,
            suggested_for=data.scheduled_for,
            description=data.description or None,
            definition_version=1,
            definition=definition_to_json(data.definition),
            validation_report_json=structural_validation_report(),
            source_type="manual",
            content_hash=workout_content_hash(data),
            edit_source="manual",
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

    def _execution(self, workout: Workout, revision: WorkoutRevision) -> AcceptedWorkoutExecution:
        binding = self._binding(workout)
        return AcceptedWorkoutExecution(
            workout_id=workout.id,
            revision_id=revision.id,
            revision_number=revision.revision_number,
            name=revision.name,
            sport=revision.sport,
            description=revision.description,
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
        validation = self._validate_context(
            workout, revision, default_context_fingerprint(revision.content_hash)
        )
        if not validation.valid:
            raise WorkoutTransitionError(
                "Das Workout erfüllt die aktuellen Prüfungen nicht.",
                code="workout.validation_failed",
            )

    def _validate_context(
        self,
        workout: Workout,
        revision: WorkoutRevision,
        context_fingerprint: str,
    ) -> WorkoutValidationRun:
        now = utcnow()
        existing = self.session.scalar(
            select(WorkoutValidationRun)
            .where(
                WorkoutValidationRun.workout_id == workout.id,
                WorkoutValidationRun.revision_id == revision.id,
                WorkoutValidationRun.validation_kind == "contextual",
                WorkoutValidationRun.rule_set_version == STRUCTURAL_RULE_SET_VERSION,
                WorkoutValidationRun.context_fingerprint == context_fingerprint,
                WorkoutValidationRun.expires_at > now,
            )
            .order_by(WorkoutValidationRun.evaluated_at.desc())
        )
        if existing is not None:
            return existing
        run = WorkoutValidationRun(
            workout_id=workout.id,
            revision_id=revision.id,
            validation_kind="contextual",
            rule_set_version=STRUCTURAL_RULE_SET_VERSION,
            context_fingerprint=context_fingerprint,
            feedback_ids_json=[],
            evaluated_at=now,
            expires_at=now + timedelta(hours=1),
            valid=True,
            report_json={"valid": True, "issues": []},
        )
        self.session.add(run)
        return run

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
