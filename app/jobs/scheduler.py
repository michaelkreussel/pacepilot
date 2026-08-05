import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import GarminAccount
from app.services.garmin.sync import SyncAlreadyRunningError, sync_garmin

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone="UTC")


def synchronize_accounts() -> None:
    with SessionLocal() as session:
        account_ids = list(session.scalars(select(GarminAccount.id)))
    for account_id in account_ids:
        with SessionLocal() as session:
            account = session.get(GarminAccount, account_id)
            if account is None or account.connected_at is None:
                continue
            try:
                sync_garmin(session, account)
            except SyncAlreadyRunningError:
                logger.info("Skipping scheduled sync because another sync is active")


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
    )
    scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
