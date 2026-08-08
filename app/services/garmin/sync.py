import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from functools import partial
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import DailyHealth, GarminAccount, GarminDevice, SyncRun
from app.models.user import utcnow
from app.services.garmin.activity_backfill import sync_activity_history
from app.services.garmin.client import (
    connect_garmin,
    connect_garmin_account,
    message_from_exception,
)
from app.services.garmin.health_backfill import sync_health_history

sync_lock = threading.Lock()
logger = logging.getLogger("uvicorn.error")


class SyncAlreadyRunningError(RuntimeError):
    pass


def _timed_garmin_call[T](run_id: int, operation: str, call: Callable[[], T]) -> T:
    started = time.perf_counter()
    logger.info("Garmin sync %s: %s started", run_id, operation)
    try:
        result = call()
    except Exception:
        logger.exception(
            "Garmin sync %s: %s failed after %.1f s",
            run_id,
            operation,
            time.perf_counter() - started,
        )
        raise
    logger.info(
        "Garmin sync %s: %s finished in %.1f s",
        run_id,
        operation,
        time.perf_counter() - started,
    )
    return result


def _set_progress(
    session: Session,
    run: SyncRun,
    *,
    stage: str,
    message: str,
    current: int | None = None,
    total: int | None = None,
) -> None:
    run.stage = stage
    run.message = message
    if current is not None:
        run.current_item = current
    if total is not None:
        run.total_items = total
    session.commit()


def _number(data: dict[str, Any], *keys: str) -> int | float | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (int, float)):
            return value
    return None


def _integer(data: dict[str, Any], *keys: str) -> int | None:
    number = _number(data, *keys)
    return round(number) if number is not None else None


def _store_daily_summary(
    session: Session, user_id: int, day: date, summary: dict[str, Any]
) -> DailyHealth:
    health = session.scalar(
        select(DailyHealth).where(DailyHealth.user_id == user_id, DailyHealth.day == day)
    )
    if health is None:
        health = DailyHealth(user_id=user_id, day=day)
        session.add(health)
    health.steps = _integer(summary, "totalSteps", "steps")
    health.resting_hr = _integer(summary, "restingHeartRate", "restingHR")
    health.stress_average = _integer(summary, "averageStressLevel")
    health.body_battery_high = _integer(summary, "bodyBatteryHighestValue")
    return health


def refresh_daily_summary(session: Session, user_id: int, day: date | None = None) -> None:
    if not sync_lock.acquire(blocking=False):
        raise SyncAlreadyRunningError("Eine Garmin-Synchronisierung läuft bereits.")
    try:
        target_day = day or date.today()
        account = session.scalar(select(GarminAccount).where(GarminAccount.user_id == user_id))
        client = (
            connect_garmin_account(session, account) if account is not None else connect_garmin()
        )
        summary = client.get_user_summary(target_day.isoformat()) or {}
        _store_daily_summary(session, user_id, target_day, summary)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        sync_lock.release()


def _sync_devices(session: Session, client: Any, account: GarminAccount, run_id: int) -> None:
    devices = _timed_garmin_call(run_id, "devices", client.get_devices) or []
    for item in devices:
        if not isinstance(item, dict):
            continue
        device_id = str(
            item.get("deviceId") or item.get("unitId") or item.get("userDeviceId") or ""
        )
        if not device_id:
            continue
        device = session.scalar(
            select(GarminDevice).where(
                GarminDevice.account_id == account.id,
                GarminDevice.garmin_device_id == device_id,
            )
        )
        if device is None:
            device = GarminDevice(account_id=account.id, garmin_device_id=device_id)
            session.add(device)
        device.name = str(item.get("displayName") or item.get("deviceName") or "Garmin-Gerät")
        device.model = str(item.get("productDisplayName") or item.get("productType") or "") or None
        device.last_seen_at = utcnow()


def sync_garmin(session: Session, account: GarminAccount) -> SyncRun:
    if not sync_lock.acquire(blocking=False):
        raise SyncAlreadyRunningError("Eine Garmin-Synchronisierung läuft bereits.")
    run = SyncRun(
        user_id=account.user_id,
        stage="starting",
        message="Synchronisierung wird vorbereitet",
    )
    session.add(run)
    account.sync_status = "running"
    account.sync_error = None
    session.commit()
    started = time.perf_counter()
    logger.info("Garmin sync %s: started for account %s", run.id, account.id)
    try:
        settings = get_settings()
        _set_progress(
            session,
            run,
            stage="login",
            message="Verbindung zu Garmin Connect wird hergestellt",
        )
        client = _timed_garmin_call(
            run.id, "login", partial(connect_garmin_account, session, account)
        )
        _set_progress(
            session,
            run,
            stage="activities",
            message="Aktivitätsverlauf wird aktualisiert",
        )

        def report_activity(_activity_id: str, current: int, total: int) -> None:
            _set_progress(
                session,
                run,
                stage="activities",
                message=f"Aktivität {current} von {total} wird geladen",
                current=current,
                total=total,
            )

        activity_result = sync_activity_history(
            session,
            client,
            account.user_id,
            delay=settings.garmin_call_delay_seconds,
            progress=report_activity,
        )
        run.activities_synced = activity_result.inserted + activity_result.updated
        session.commit()
        health_progress = 0

        def report_health(resource: str, day: date) -> None:
            nonlocal health_progress
            health_progress += 1
            _set_progress(
                session,
                run,
                stage="health",
                message=f"{resource} für {day.strftime('%d.%m.%Y')} wird geladen",
                current=health_progress,
            )

        health_result = sync_health_history(
            session,
            client,
            account.user_id,
            overlap_days=settings.health_sync_overlap_days,
            delay=settings.garmin_call_delay_seconds,
            progress=report_health,
        )
        run.health_days_synced = health_result.unique_days_processed
        run.current_item = health_result.unique_days_processed
        run.total_items = health_result.unique_days_processed
        session.commit()
        _set_progress(
            session,
            run,
            stage="devices",
            message="Garmin-Geräte werden aktualisiert",
        )
        _sync_devices(session, client, account, run.id)
        account.last_sync_at = datetime.now(UTC).replace(tzinfo=None)
        account.sync_status = "ok"
        account.sync_error = None
        run.status = "ok"
        run.stage = "complete"
        run.message = "Synchronisierung abgeschlossen"
        run.current_item = run.total_items
        run.finished_at = utcnow()
        session.commit()
        logger.info(
            "Garmin sync %s: completed in %.1f s (%s activities, %s health days)",
            run.id,
            time.perf_counter() - started,
            run.activities_synced,
            run.health_days_synced,
        )
    except Exception as exc:
        session.rollback()
        failed_account = session.get(GarminAccount, account.id)
        failed_run = session.get(SyncRun, run.id)
        if failed_account is not None:
            failed_account.sync_status = "error"
            failed_account.sync_error = message_from_exception(exc)
        if failed_run is not None:
            failed_run.status = "error"
            failed_run.stage = "error"
            failed_run.message = "Synchronisierung fehlgeschlagen"
            failed_run.error = message_from_exception(exc)
            failed_run.finished_at = utcnow()
        session.commit()
        logger.error(
            "Garmin sync %s: aborted after %.1f s: %s",
            run.id,
            time.perf_counter() - started,
            message_from_exception(exc),
        )
    finally:
        sync_lock.release()
    return run
