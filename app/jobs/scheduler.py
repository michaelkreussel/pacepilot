import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    GarminAccount,
    SyncRun,
    Workout,
    WorkoutGarminAttempt,
    WorkoutGarminBinding,
    WorkoutGarminOperation,
)
from app.models.user import utcnow
from app.services.garmin.locks import GarminAccountBusyError, garmin_account_slot
from app.services.garmin.sync import rate_limit_cooldown_remaining, sync_garmin

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone="UTC")
_executor_lock = threading.Lock()
_sync_executor: ThreadPoolExecutor | None = None
_queued_account_ids: set[int] = set()


def synchronize_account(account_id: int, *, wait_for_slot: bool = False) -> None:
    try:
        with garmin_account_slot(account_id, wait=wait_for_slot), SessionLocal() as session:
            account = session.get(GarminAccount, account_id)
            if account is None or account.connected_at is None:
                if account is not None and account.sync_status == "queued":
                    account.sync_status = "not_connected"
                    session.commit()
                return
            if rate_limit_cooldown_remaining(session, account):
                return
            sync_garmin(session, account, slot_acquired=True)
    except GarminAccountBusyError:
        logger.info("Skipping Garmin sync because this account is already active")


def synchronize_accounts() -> None:
    cutoff = utcnow() - timedelta(minutes=get_settings().sync_interval_minutes)
    with SessionLocal() as session:
        account_ids = list(
            session.scalars(
                select(GarminAccount.id).where(
                    GarminAccount.connected_at.is_not(None),
                    GarminAccount.last_sync_at.is_not(None),
                    GarminAccount.last_sync_at <= cutoff,
                )
            )
        )
    for account_id in account_ids:

        def mark_queued(current_account_id: int = account_id) -> bool:
            with SessionLocal() as session:
                account = session.get(GarminAccount, current_account_id)
                if (
                    account is not None
                    and account.connected_at is not None
                    and account.last_sync_at is not None
                    and account.last_sync_at
                    <= utcnow() - timedelta(minutes=get_settings().sync_interval_minutes)
                    and account.sync_status not in {"queued", "running"}
                    and not rate_limit_cooldown_remaining(session, account)
                ):
                    account.sync_status = "queued"
                    session.commit()
                    return True
                return False

        queue_account_sync(account_id, mark_queued)


def _run_queued_account_sync(account_id: int) -> None:
    try:
        synchronize_account(account_id, wait_for_slot=True)
    finally:
        with _executor_lock:
            _queued_account_ids.discard(account_id)


def queue_account_sync(account_id: int, mark_queued: Callable[[], bool]) -> bool:
    global _sync_executor
    with _executor_lock:
        if account_id in _queued_account_ids:
            return False
        _queued_account_ids.add(account_id)
        try:
            queued = mark_queued()
        except Exception:
            _queued_account_ids.discard(account_id)
            raise
        if not queued:
            _queued_account_ids.discard(account_id)
            return False
        if _sync_executor is None:
            _sync_executor = ThreadPoolExecutor(
                max_workers=get_settings().garmin_sync_workers,
                thread_name_prefix="garmin-sync",
            )
        try:
            _sync_executor.submit(_run_queued_account_sync, account_id)
        except Exception:
            _queued_account_ids.discard(account_id)
            raise
        return True


def repair_interrupted_syncs() -> None:
    """Make process-local jobs retryable after an application restart."""
    message = "Der vorherige Sync wurde durch einen Neustart unterbrochen. Bitte erneut starten."
    with SessionLocal() as session:
        accounts = list(
            session.scalars(
                select(GarminAccount).where(GarminAccount.sync_status.in_({"queued", "running"}))
            )
        )
        running_runs = list(session.scalars(select(SyncRun).where(SyncRun.status == "running")))
        pending_operations = list(
            session.scalars(
                select(WorkoutGarminOperation).where(WorkoutGarminOperation.status == "pending")
            )
        )
        pending_attempts = list(
            session.scalars(
                select(WorkoutGarminAttempt).where(WorkoutGarminAttempt.status == "pending")
            )
        )
        if not accounts and not running_runs and not pending_operations and not pending_attempts:
            return
        for account in accounts:
            account.sync_status = "error"
            account.sync_error = message
        for run in running_runs:
            run.status = "error"
            run.stage = "error"
            run.message = "Synchronisierung unterbrochen"
            run.error = message
            run.finished_at = utcnow()
        interrupted_at = utcnow()
        attempted_operation_ids = {attempt.operation_id for attempt in pending_attempts}
        for attempt in pending_attempts:
            attempt.status = "unknown"
            attempt.completed_at = interrupted_at
            attempt.error_code = "garmin.process_interrupted"
            attempt.error_message = "Garmin-Operation durch Neustart unterbrochen"
        for operation in pending_operations:
            attempted = operation.id in attempted_operation_ids
            operation.status = "unknown" if attempted else "retryable"
            operation.completed_at = interrupted_at if attempted else None
            operation.error_code = (
                "garmin.process_interrupted" if attempted else "garmin.operation_not_started"
            )
            binding = session.get(WorkoutGarminBinding, operation.binding_id)
            if binding is not None:
                axis = (
                    "content_status"
                    if operation.operation_type in {"upload", "update", "delete"}
                    else "calendar_status"
                    if operation.operation_type in {"schedule", "unschedule"}
                    else "device_status"
                )
                setattr(binding, axis, "unknown" if attempted else "retryable")
                binding.last_error_code = operation.error_code
                binding.last_error_message = (
                    "Der Ausgang der Garmin-Operation muss geprüft werden."
                    if attempted
                    else "Die Garmin-Operation wurde vor dem Netzwerkaufruf unterbrochen."
                )
        session.commit()


def repair_stale_garmin_operations() -> None:
    """Classify operations that remain pending while the process keeps running."""
    cutoff = utcnow() - timedelta(minutes=get_settings().garmin_operation_stale_minutes)
    with SessionLocal() as session:
        pending_operations = list(
            session.scalars(
                select(WorkoutGarminOperation).where(
                    WorkoutGarminOperation.status == "pending",
                    WorkoutGarminOperation.created_at <= cutoff,
                )
            )
        )
        if not pending_operations:
            return
        for operation in pending_operations:
            account_id = session.scalar(
                select(GarminAccount.id)
                .join(Workout, Workout.user_id == GarminAccount.user_id)
                .where(Workout.id == operation.workout_id)
            )
            if account_id is None:
                continue
            try:
                with garmin_account_slot(account_id):
                    session.refresh(operation)
                    if operation.status != "pending":
                        continue
                    pending_attempts = list(
                        session.scalars(
                            select(WorkoutGarminAttempt).where(
                                WorkoutGarminAttempt.operation_id == operation.id,
                                WorkoutGarminAttempt.status == "pending",
                            )
                        )
                    )
                    if any(attempt.started_at > cutoff for attempt in pending_attempts):
                        continue
                    repaired_at = utcnow()
                    attempted = bool(pending_attempts)
                    for attempt in pending_attempts:
                        attempt.status = "unknown"
                        attempt.completed_at = repaired_at
                        attempt.error_code = "garmin.operation_stale"
                        attempt.error_message = "Garmin-Operation hat das Zeitlimit überschritten"
                    operation.status = "unknown" if attempted else "retryable"
                    operation.completed_at = repaired_at if attempted else None
                    operation.error_code = (
                        "garmin.operation_stale" if attempted else "garmin.operation_not_started"
                    )
                    binding = session.get(WorkoutGarminBinding, operation.binding_id)
                    if binding is not None:
                        axis = (
                            "content_status"
                            if operation.operation_type in {"upload", "update", "delete"}
                            else "calendar_status"
                            if operation.operation_type in {"schedule", "unschedule"}
                            else "device_status"
                        )
                        setattr(binding, axis, "unknown" if attempted else "retryable")
                        binding.last_error_code = operation.error_code
                        binding.last_error_message = (
                            "Der Ausgang der Garmin-Operation muss geprüft werden."
                            if attempted
                            else (
                                "Die Garmin-Operation wurde nicht gestartet und kann "
                                "wiederholt werden."
                            )
                        )
                    session.commit()
            except GarminAccountBusyError:
                session.rollback()
                continue


def start_scheduler() -> None:
    settings = get_settings()
    if scheduler.running:
        return
    repair_interrupted_syncs()
    if not settings.scheduler_enabled:
        return
    scheduler.add_job(
        synchronize_accounts,
        "interval",
        minutes=settings.sync_interval_minutes,
        id="garmin-sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        repair_stale_garmin_operations,
        "interval",
        minutes=5,
        id="garmin-operation-repair",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()


def stop_scheduler() -> None:
    global _sync_executor
    if scheduler.running:
        scheduler.shutdown(wait=False)
    with _executor_lock:
        if _sync_executor is not None:
            _sync_executor.shutdown(wait=False, cancel_futures=True)
            _sync_executor = None
        _queued_account_ids.clear()
