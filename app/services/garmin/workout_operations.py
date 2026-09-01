import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from garminconnect.exceptions import GarminConnectTooManyRequestsError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    GarminAccount,
    Workout,
    WorkoutGarminAttempt,
    WorkoutGarminBinding,
    WorkoutGarminOperation,
    WorkoutGarminRemoteIdentity,
    WorkoutRevision,
)
from app.models.user import utcnow
from app.services.garmin.client import GarminUnavailableError, connect_garmin_account
from app.services.garmin.health_backfill import GarminPacer, retry_after_seconds
from app.services.garmin.locks import GarminAccountBusyError, garmin_account_slot
from app.services.garmin.sync import rate_limit_cooldown_remaining
from app.services.garmin.workout_export import (
    WorkoutExecution,
    delete_remote_workout,
    push_workout,
    schedule_workout_on_date,
    scheduled_workout_ids,
    unschedule_workout_on_date,
    update_workout_content,
    upload_workout,
)
from app.services.planning.validator import WorkoutValidationError

type GarminCall[T] = Callable[[], T]
type GarminConnector = Callable[[Session, GarminAccount], Any]
type SuccessHandler[T] = Callable[[T, WorkoutGarminOperation], None]

RECONCILIATION_CAPABILITIES = {
    "upload": False,
    "update": False,
    "schedule": True,
    "unschedule": True,
    "push": False,
    "delete": False,
}


@dataclass(frozen=True)
class GarminTrainingFitAuthorization:
    policy_version: str
    assessment_fingerprint: str
    effective_date: date
    acknowledged_by_user_id: int
    acknowledged_at: datetime
    revision_id: int


def operation_idempotency_key(
    *,
    operation_type: str,
    binding_id: int,
    revision_id: int,
    remote_identity_id: int | None,
    scheduled_for: date | None,
    generation: int,
) -> str:
    payload = {
        "binding_id": binding_id,
        "generation": generation,
        "operation_type": operation_type,
        "remote_identity_id": remote_identity_id,
        "revision_id": revision_id,
        "scheduled_for": scheduled_for.isoformat() if scheduled_for is not None else None,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _exception_chain(exc: Exception) -> list[Exception]:
    chain: list[Exception] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while isinstance(current, Exception) and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _rate_limit_seconds(exc: Exception) -> int | None:
    settings = get_settings()
    for current in _exception_chain(exc):
        status = getattr(getattr(current, "response", None), "status_code", None)
        if isinstance(current, GarminConnectTooManyRequestsError) or status == 429:
            retry_after = getattr(getattr(current, "response", None), "headers", {}).get(
                "Retry-After"
            )
            return round(
                retry_after_seconds(retry_after, settings.garmin_rate_limit_cooldown_seconds)
            )
    return None


def _axis(operation_type: str) -> str:
    if operation_type in {"upload", "update", "delete"}:
        return "content_status"
    if operation_type in {"schedule", "unschedule"}:
        return "calendar_status"
    return "device_status"


class GarminWorkoutOperations:
    """Executes durable, serialized Garmin workout operations."""

    def __init__(
        self,
        session: Session,
        account: GarminAccount,
        *,
        connect_garmin: GarminConnector = connect_garmin_account,
        training_fit_authorization: GarminTrainingFitAuthorization | None = None,
    ) -> None:
        self.session = session
        self.account = account
        self.connect_garmin = connect_garmin
        self.training_fit_authorization = training_fit_authorization

    def upload(
        self,
        workout: Workout,
        binding: WorkoutGarminBinding,
        revision: WorkoutRevision,
        execution: WorkoutExecution,
        *,
        on_uploaded: Callable[[], None] | None = None,
    ) -> WorkoutGarminRemoteIdentity | None:
        def record_upload(remote_id: str, operation: WorkoutGarminOperation) -> None:
            identity = WorkoutGarminRemoteIdentity(
                binding_id=binding.id,
                garmin_account_id=self.account.id,
                garmin_workout_id=remote_id,
                principal_fingerprint=self.account.principal_fingerprint,
                status="active",
            )
            self.session.add(identity)
            self.session.flush()
            binding.active_remote_identity_id = identity.id
            binding.content_status = "synced"
            workout.garmin_workout_id = remote_id
            operation.remote_reference = remote_id
            if on_uploaded is not None:
                on_uploaded()

        self._execute(
            workout=workout,
            binding=binding,
            revision=revision,
            operation_type="upload",
            remote_identity=None,
            call=lambda: self._call(
                "workout.upload", lambda client: upload_workout(client, execution)
            ),
            on_success=record_upload,
        )
        if binding.active_remote_identity_id is None:
            return None
        return self.session.get(WorkoutGarminRemoteIdentity, binding.active_remote_identity_id)

    def update_content(
        self,
        workout: Workout,
        binding: WorkoutGarminBinding,
        revision: WorkoutRevision,
        identity: WorkoutGarminRemoteIdentity,
        execution: WorkoutExecution,
    ) -> None:
        self._execute(
            workout=workout,
            binding=binding,
            revision=revision,
            operation_type="update",
            remote_identity=identity,
            call=lambda: self._call(
                "workout.update", lambda client: update_workout_content(client, execution)
            ),
            on_success=lambda _result, _operation: setattr(binding, "content_status", "synced"),
        )

    def unschedule(
        self,
        workout: Workout,
        binding: WorkoutGarminBinding,
        revision: WorkoutRevision,
        identity: WorkoutGarminRemoteIdentity,
        scheduled_for: date,
    ) -> None:
        self._unschedule(
            workout,
            binding,
            revision,
            identity,
            scheduled_for,
            clear_calendar_status=True,
        )

    def unschedule_before_delete(
        self,
        workout: Workout,
        binding: WorkoutGarminBinding,
        revision: WorkoutRevision,
        identity: WorkoutGarminRemoteIdentity,
        scheduled_for: date,
    ) -> None:
        self._unschedule(
            workout,
            binding,
            revision,
            identity,
            scheduled_for,
            clear_calendar_status=False,
        )

    def _unschedule(
        self,
        workout: Workout,
        binding: WorkoutGarminBinding,
        revision: WorkoutRevision,
        identity: WorkoutGarminRemoteIdentity,
        scheduled_for: date,
        *,
        clear_calendar_status: bool,
    ) -> None:
        remote_id = identity.garmin_workout_id

        def record_unschedule(_result: None, _operation: WorkoutGarminOperation) -> None:
            binding.remote_scheduled_for = None
            if clear_calendar_status:
                binding.calendar_status = "not_requested"

        self._execute(
            workout=workout,
            binding=binding,
            revision=revision,
            operation_type="unschedule",
            remote_identity=identity,
            scheduled_for=scheduled_for,
            call=lambda: self._call(
                "workout.unschedule",
                lambda client: unschedule_workout_on_date(client, remote_id, scheduled_for),
            ),
            reconcile=lambda: self._call(
                "workout.unschedule.reconcile",
                lambda client: not scheduled_workout_ids(client, remote_id, scheduled_for),
            ),
            on_success=record_unschedule,
            on_reconciled=lambda operation: record_unschedule(None, operation),
        )

    def schedule(
        self,
        workout: Workout,
        binding: WorkoutGarminBinding,
        revision: WorkoutRevision,
        identity: WorkoutGarminRemoteIdentity,
        scheduled_for: date,
    ) -> None:
        remote_id = identity.garmin_workout_id

        def record_schedule(_result: None, _operation: WorkoutGarminOperation) -> None:
            binding.remote_scheduled_for = scheduled_for
            binding.calendar_status = "synced"

        self._execute(
            workout=workout,
            binding=binding,
            revision=revision,
            operation_type="schedule",
            remote_identity=identity,
            scheduled_for=scheduled_for,
            call=lambda: self._call(
                "workout.schedule",
                lambda client: schedule_workout_on_date(client, remote_id, scheduled_for),
            ),
            reconcile=lambda: self._call(
                "workout.schedule.reconcile",
                lambda client: bool(scheduled_workout_ids(client, remote_id, scheduled_for)),
            ),
            on_success=record_schedule,
            on_reconciled=lambda operation: record_schedule(None, operation),
        )

    def push(
        self,
        workout: Workout,
        binding: WorkoutGarminBinding,
        revision: WorkoutRevision,
        identity: WorkoutGarminRemoteIdentity,
        execution: WorkoutExecution,
        *,
        on_accepted: Callable[[], None] | None = None,
    ) -> WorkoutGarminOperation:
        def record_push(_result: None, _operation: WorkoutGarminOperation) -> None:
            binding.device_status = "request_accepted"
            if on_accepted is not None:
                on_accepted()

        return self._execute(
            workout=workout,
            binding=binding,
            revision=revision,
            operation_type="push",
            remote_identity=identity,
            call=lambda: self._call("workout.push", lambda client: push_workout(client, execution)),
            on_success=record_push,
        )

    def delete(
        self,
        workout: Workout,
        binding: WorkoutGarminBinding,
        revision: WorkoutRevision,
        identity: WorkoutGarminRemoteIdentity,
    ) -> None:
        remote_id = identity.garmin_workout_id

        def record_delete(_result: None, _operation: WorkoutGarminOperation) -> None:
            identity.status = "removed"
            identity.removed_at = utcnow()
            binding.active_remote_identity_id = None
            binding.content_status = "removed"
            binding.calendar_status = "removed"
            binding.device_status = "not_requested"

        self._execute(
            workout=workout,
            binding=binding,
            revision=revision,
            operation_type="delete",
            remote_identity=identity,
            call=lambda: self._call(
                "workout.delete", lambda client: delete_remote_workout(client, remote_id)
            ),
            on_success=record_delete,
        )

    def _execute[T](
        self,
        *,
        workout: Workout,
        binding: WorkoutGarminBinding,
        revision: WorkoutRevision,
        operation_type: str,
        remote_identity: WorkoutGarminRemoteIdentity | None,
        scheduled_for: date | None = None,
        call: GarminCall[T],
        on_success: SuccessHandler[T],
        reconcile: GarminCall[bool] | None = None,
        on_reconciled: Callable[[WorkoutGarminOperation], None] | None = None,
    ) -> WorkoutGarminOperation:
        accepted_revision_id, lock_version = self.session.execute(
            select(Workout.accepted_revision_id, Workout.lock_version).where(
                Workout.id == workout.id
            )
        ).one()
        if accepted_revision_id != revision.id or lock_version != workout.lock_version:
            raise GarminUnavailableError(
                "Das Workout wurde vor dem Garmin-Aufruf zwischenzeitlich geändert."
            )
        unresolved = self.session.scalar(
            select(WorkoutGarminOperation.id)
            .where(
                WorkoutGarminOperation.binding_id == binding.id,
                WorkoutGarminOperation.status == "unknown",
                WorkoutGarminOperation.operation_type.in_(
                    {"upload", "update"}
                    if operation_type in {"upload", "update"}
                    else {operation_type}
                ),
            )
            .limit(1)
        )
        if unresolved is not None and operation_type not in {"schedule", "unschedule"}:
            raise GarminUnavailableError(
                "Der Ausgang einer früheren Garmin-Operation ist unklar. Eine automatische "
                "Wiederholung wurde zum Schutz vor Duplikaten blockiert."
            )
        key = operation_idempotency_key(
            operation_type=operation_type,
            binding_id=binding.id,
            revision_id=revision.id,
            remote_identity_id=remote_identity.id if remote_identity else None,
            scheduled_for=scheduled_for,
            generation=workout.lock_version,
        )
        operation = self.session.scalar(
            select(WorkoutGarminOperation).where(WorkoutGarminOperation.idempotency_key == key)
        )
        created = operation is None
        if operation is None:
            authorization = self.training_fit_authorization
            operation = WorkoutGarminOperation(
                workout_id=workout.id,
                binding_id=binding.id,
                operation_type=operation_type,
                revision_id=revision.id,
                remote_identity_id=remote_identity.id if remote_identity else None,
                scheduled_for=scheduled_for,
                idempotency_key=key,
                status="pending",
                training_fit_policy_version=(
                    authorization.policy_version if authorization is not None else None
                ),
                training_fit_assessment_fingerprint=(
                    authorization.assessment_fingerprint if authorization is not None else None
                ),
                training_fit_effective_date=(
                    authorization.effective_date if authorization is not None else None
                ),
                training_fit_acknowledged_by_user_id=(
                    authorization.acknowledged_by_user_id if authorization is not None else None
                ),
                training_fit_acknowledged_at=(
                    authorization.acknowledged_at if authorization is not None else None
                ),
                training_fit_authorized_revision_id=(
                    authorization.revision_id if authorization is not None else None
                ),
            )
            self.session.add(operation)
            setattr(binding, _axis(operation_type), "pending")
            binding.last_attempt_at = utcnow()
            try:
                self.session.commit()
            except IntegrityError:
                self.session.rollback()
                created = False
                operation = self.session.scalar(
                    select(WorkoutGarminOperation).where(
                        WorkoutGarminOperation.idempotency_key == key
                    )
                )
                if operation is None:
                    raise

        if operation.status == "succeeded":
            return operation
        if operation.status == "pending" and not created:
            raise GarminUnavailableError("Die Garmin-Operation wird bereits ausgeführt.")
        if operation.status == "failed_final":
            raise GarminUnavailableError("Die Garmin-Operation ist endgültig fehlgeschlagen.")
        if operation.status == "unknown":
            if reconcile is None:
                raise GarminUnavailableError(
                    "Der Ausgang der Garmin-Operation ist unklar. Eine automatische "
                    "Wiederholung wurde zum Schutz vor Duplikaten blockiert."
                )
            if self._reconcile(operation, binding, reconcile):
                if on_reconciled is None:
                    raise RuntimeError("Reconciled Garmin operation has no success handler")
                on_reconciled(operation)
                self._succeed(operation, binding)
                self.session.commit()
                return operation

        cooldown = rate_limit_cooldown_remaining(self.session, self.account)
        if cooldown:
            operation.status = "retryable"
            operation.error_code = "garmin.rate_limited"
            setattr(binding, _axis(operation_type), "retryable")
            self.session.commit()
            raise GarminUnavailableError(
                "Garmin ist vorübergehend limitiert. "
                f"Bitte in {cooldown} Sekunden erneut versuchen."
            )

        attempt = self._start_attempt(
            operation,
            "execute",
            expected_status=None if created else operation.status,
        )
        try:
            result = call()
        except WorkoutValidationError as exc:
            self._fail(operation, attempt, binding, exc, status="failed_final")
            raise
        except Exception as exc:
            retry_after = _rate_limit_seconds(exc)
            if retry_after is not None:
                self.account.rate_limit_until = utcnow() + timedelta(seconds=retry_after)
                GarminPacer.defer_all(retry_after)
                self._fail(operation, attempt, binding, exc, status="retryable")
            elif any(
                isinstance(current, GarminAccountBusyError) for current in _exception_chain(exc)
            ):
                self._fail(operation, attempt, binding, exc, status="retryable")
            else:
                self._fail(operation, attempt, binding, exc, status="unknown")
            raise

        try:
            self.session.refresh(workout)
            if workout.accepted_revision_id != revision.id or workout.lock_version != lock_version:
                raise GarminUnavailableError(
                    "Das Workout wurde während der Garmin-Operation geändert. "
                    "Der Remote-Zustand muss geprüft werden."
                )
            on_success(result, operation)
            attempt.status = "succeeded"
            attempt.completed_at = utcnow()
            self._succeed(operation, binding)
            self.session.commit()
        except Exception as exc:
            operation_id = operation.id
            attempt_id = attempt.id
            self.session.rollback()
            operation = self.session.get(WorkoutGarminOperation, operation_id)
            attempt = self.session.get(WorkoutGarminAttempt, attempt_id)
            reloaded_binding = self.session.get(WorkoutGarminBinding, binding.id)
            if operation is not None and attempt is not None and reloaded_binding is not None:
                self._fail(operation, attempt, reloaded_binding, exc, status="unknown")
            raise
        return operation

    def _start_attempt(
        self,
        operation: WorkoutGarminOperation,
        attempt_kind: str,
        *,
        expected_status: str | None,
    ) -> WorkoutGarminAttempt:
        if expected_status is not None:
            result = self.session.execute(
                update(WorkoutGarminOperation)
                .where(
                    WorkoutGarminOperation.id == operation.id,
                    WorkoutGarminOperation.status == expected_status,
                )
                .values(status="pending")
                .execution_options(synchronize_session=False)
            )
            if getattr(result, "rowcount", 0) != 1:
                self.session.rollback()
                raise GarminUnavailableError("Die Garmin-Operation wird bereits ausgeführt.")
            operation.status = "pending"
        attempt_number = (
            self.session.scalar(
                select(func.max(WorkoutGarminAttempt.attempt_number)).where(
                    WorkoutGarminAttempt.operation_id == operation.id
                )
            )
            or 0
        ) + 1
        attempt = WorkoutGarminAttempt(
            operation_id=operation.id,
            attempt_number=attempt_number,
            attempt_kind=attempt_kind,
            status="pending",
        )
        self.session.add(attempt)
        operation.status = "pending"
        self.session.commit()
        return attempt

    def _reconcile(
        self,
        operation: WorkoutGarminOperation,
        binding: WorkoutGarminBinding,
        reconcile: GarminCall[bool],
    ) -> bool:
        attempt = self._start_attempt(
            operation,
            "reconcile",
            expected_status="unknown",
        )
        try:
            effect_present = reconcile()
        except Exception as exc:
            self._fail(operation, attempt, binding, exc, status="unknown")
            raise
        attempt.status = "succeeded"
        attempt.completed_at = utcnow()
        if not effect_present:
            operation.status = "retryable"
            setattr(binding, _axis(operation.operation_type), "retryable")
        self.session.commit()
        return effect_present

    def _succeed(self, operation: WorkoutGarminOperation, binding: WorkoutGarminBinding) -> None:
        now = utcnow()
        operation.status = "succeeded"
        operation.completed_at = now
        operation.error_code = None
        binding.last_success_at = now
        binding.last_error_code = None
        binding.last_error_message = None

    def _fail(
        self,
        operation: WorkoutGarminOperation,
        attempt: WorkoutGarminAttempt,
        binding: WorkoutGarminBinding,
        exc: Exception,
        *,
        status: str,
    ) -> None:
        now = utcnow()
        error_code = (
            "garmin.rate_limited"
            if status == "retryable"
            else "garmin.remote_outcome_unknown"
            if status == "unknown"
            else getattr(exc, "code", "garmin.validation_failed")
        )
        message = str(exc)[:1000]
        operation.status = status
        operation.error_code = error_code
        operation.completed_at = now if status in {"unknown", "failed_final"} else None
        attempt.status = "failed" if status == "failed_final" else status
        attempt.completed_at = now
        attempt.error_code = error_code
        attempt.error_message = message
        setattr(binding, _axis(operation.operation_type), status)
        binding.last_error_code = error_code
        binding.last_error_message = message
        self.session.commit()

    @contextmanager
    def _client(self) -> Iterator[Any]:
        try:
            with garmin_account_slot(self.account.id):
                yield self.connect_garmin(self.session, self.account)
        except GarminAccountBusyError as exc:
            raise GarminUnavailableError(
                "Für dieses Garmin-Konto läuft gerade eine andere Operation."
            ) from exc

    def _call[T](self, operation: str, call: Callable[[Any], T]) -> T:
        settings = get_settings()
        pacer = GarminPacer(
            settings.garmin_call_delay_seconds,
            {"garmin_account_id": self.account.id, "workout_operation": operation},
            rate_limit_cooldown=settings.garmin_rate_limit_cooldown_seconds,
        )
        with self._client() as client:
            return pacer.call(operation, lambda: call(client))
