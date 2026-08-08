import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import GarminAccount
from app.services.garmin.sync import SyncAlreadyRunningError, sync_garmin

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone="UTC")
_executor_lock = threading.Lock()
_sync_executor: ThreadPoolExecutor | None = None
_queued_account_ids: set[int] = set()


def synchronize_account(account_id: int, *, wait_for_slot: bool = False) -> None:
    with SessionLocal() as session:
        account = session.get(GarminAccount, account_id)
        if account is None or account.connected_at is None:
            return
        try:
            sync_garmin(session, account, wait_for_slot=wait_for_slot)
        except SyncAlreadyRunningError:
            logger.info("Skipping sync because another sync is active")


def synchronize_accounts() -> None:
    with SessionLocal() as session:
        account_ids = list(session.scalars(select(GarminAccount.id)))
    for account_id in account_ids:
        synchronize_account(account_id)


def _run_queued_account_sync(account_id: int) -> None:
    try:
        synchronize_account(account_id, wait_for_slot=True)
    finally:
        with _executor_lock:
            _queued_account_ids.discard(account_id)


def queue_account_sync(account_id: int, mark_queued: Callable[[], None]) -> bool:
    global _sync_executor
    with _executor_lock:
        if account_id in _queued_account_ids:
            return False
        _queued_account_ids.add(account_id)
        try:
            mark_queued()
        except Exception:
            _queued_account_ids.discard(account_id)
            raise
        if _sync_executor is None:
            _sync_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="garmin-sync")
        try:
            _sync_executor.submit(_run_queued_account_sync, account_id)
        except Exception:
            _queued_account_ids.discard(account_id)
            raise
        return True


def start_scheduler() -> None:
    settings = get_settings()
    if not settings.scheduler_enabled or scheduler.running:
        return
    scheduler.add_job(
        synchronize_accounts,
        "interval",
        minutes=settings.sync_interval_minutes,
        id="garmin-sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC),
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
