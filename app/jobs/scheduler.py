import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import GarminAccount, SyncRun
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
        if not accounts and not running_runs:
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
        session.commit()


def start_scheduler() -> None:
    settings = get_settings()
    if not settings.scheduler_enabled or scheduler.running:
        return
    repair_interrupted_syncs()
    scheduler.add_job(
        synchronize_accounts,
        "interval",
        minutes=settings.sync_interval_minutes,
        id="garmin-sync",
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
