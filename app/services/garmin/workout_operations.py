import hashlib
import json
from collections.abc import Callable
from datetime import date, timedelta

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
from app.services.garmin.client import GarminUnavailableError
from app.services.garmin.health_backfill import GarminPacer, retry_after_seconds
from app.services.garmin.locks import GarminAccountBusyError
from app.services.garmin.sync import rate_limit_cooldown_remaining
from app.services.planning.validator import WorkoutValidationError

type GarminCall[T] = Callable[[], T]
type SuccessHandler[T] = Callable[[T, WorkoutGarminOperation], None]

RECONCILIATION_CAPABILITIES = {
    "upload": False,
    "update": False,
    "schedule": True,
    "unschedule": True,
    "push": False,
    "delete": False,
}


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


class GarminWorkoutOperationRunner:
    """Persists logical Garmin commands and every network attempt around them."""

    def __init__(self, session: Session, account: GarminAccount) -> None:
        self.session = session
        self.account = account

    def execute[T](
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
            operation = WorkoutGarminOperation(
                workout_id=workout.id,
                binding_id=binding.id,
                operation_type=operation_type,
                revision_id=revision.id,
                remote_identity_id=remote_identity.id if remote_identity else None,
                scheduled_for=scheduled_for,
                idempotency_key=key,
                status="pending",
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
