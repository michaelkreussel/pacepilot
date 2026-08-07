import gzip
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Activity, DailyHealth, GarminAccount, GarminDevice, SyncRun
from app.models.user import utcnow
from app.services.garmin.activity_details import activity_details_path, write_activity_details
from app.services.garmin.client import connect_garmin, message_from_exception

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


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        return utcnow()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return utcnow()


def _write_raw_activity(activity: dict[str, Any], activity_id: str) -> str:
    settings = get_settings()
    started_at = _parse_datetime(activity.get("startTimeLocal") or activity.get("startTimeGMT"))
    target = settings.data_dir / "raw" / "activities" / str(started_at.year)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{activity_id}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as raw_file:
        json.dump(activity, raw_file, ensure_ascii=False)
    return str(path)


def _sync_activities(session: Session, client: Any, user_id: int, start: date, run_id: int) -> int:
    payload = (
        _timed_garmin_call(
            run_id,
            f"activities {start.isoformat()} to {date.today().isoformat()}",
            lambda: client.get_activities_by_date(start.isoformat(), date.today().isoformat()),
        )
        or []
    )
    count = 0
    for item in payload:
        if not isinstance(item, dict):
            continue
        activity_id = str(item.get("activityId") or "")
        if not activity_id:
            continue
        activity = session.scalar(
            select(Activity).where(
                Activity.user_id == user_id, Activity.garmin_activity_id == activity_id
            )
        )
        if activity is None:
            activity = Activity(user_id=user_id, garmin_activity_id=activity_id)
            session.add(activity)
        type_data = item.get("activityType") or {}
        activity.name = str(item.get("activityName") or "Garmin-Aktivität")
        activity.activity_type = str(
            type_data.get("typeKey") if isinstance(type_data, dict) else type_data or "other"
        )
        activity.started_at = _parse_datetime(
            item.get("startTimeLocal") or item.get("startTimeGMT")
        )
        activity.distance_m = _number(item, "distance")
        activity.duration_s = _number(item, "duration", "elapsedDuration")
        activity.average_hr = _number(item, "averageHR")  # type: ignore[assignment]
        activity.max_hr = _number(item, "maxHR")  # type: ignore[assignment]
        activity.calories = _number(item, "calories")  # type: ignore[assignment]
        activity.elevation_gain_m = _number(item, "elevationGain")
        activity.raw_file = _write_raw_activity(item, activity_id)
        try:
            details_path = activity_details_path(activity.started_at, activity_id)
            if not details_path.is_file():
                details = _timed_garmin_call(
                    run_id,
                    f"activity details {activity_id}",
                    partial(client.get_activity_details, activity_id, maxchart=2000, maxpoly=2000),
                )
                if isinstance(details, dict) and details.get("detailsAvailable") is not False:
                    write_activity_details(details_path, details)
        except Exception as exc:
            logger.warning(
                "Garmin sync %s: activity details %s skipped: %s",
                run_id,
                activity_id,
                exc,
            )
        activity.synced_at = utcnow()
        count += 1
    session.flush()
    return count


def _sync_health_day(
    session: Session,
    client: Any,
    user_id: int,
    day: date,
    run_id: int,
    report: Callable[[str], None],
) -> None:
    day_string = day.isoformat()
    report(f"Tagesübersicht für {day.strftime('%d.%m.%Y')} wird geladen")
    summary = (
        _timed_garmin_call(
            run_id, f"daily summary {day_string}", lambda: client.get_user_summary(day_string)
        )
        or {}
    )
    report(f"Schlafdaten für {day.strftime('%d.%m.%Y')} werden geladen")
    try:
        sleep = (
            _timed_garmin_call(
                run_id, f"sleep {day_string}", lambda: client.get_sleep_data(day_string)
            )
            or {}
        )
    except Exception as exc:
        logger.warning("Garmin sync %s: sleep %s skipped: %s", run_id, day_string, exc)
        sleep = {}
    report(f"HRV-Daten für {day.strftime('%d.%m.%Y')} werden geladen")
    try:
        hrv = (
            _timed_garmin_call(run_id, f"HRV {day_string}", lambda: client.get_hrv_data(day_string))
            or {}
        )
    except Exception as exc:
        logger.warning("Garmin sync %s: HRV %s skipped: %s", run_id, day_string, exc)
        hrv = {}

    sleep_dto = sleep.get("dailySleepDTO") if isinstance(sleep, dict) else {}
    sleep_dto = sleep_dto if isinstance(sleep_dto, dict) else {}
    hrv_summary = hrv.get("hrvSummary") if isinstance(hrv, dict) else {}
    hrv_summary = hrv_summary if isinstance(hrv_summary, dict) else {}
    health = _store_daily_summary(session, user_id, day, summary)

    health.sleep_seconds = _number(sleep_dto, "sleepTimeSeconds")  # type: ignore[assignment]
    sleep_scores = sleep_dto.get("sleepScores")
    if isinstance(sleep_scores, dict):
        overall = sleep_scores.get("overall")
        if isinstance(overall, dict):
            health.sleep_score = _number(overall, "value", "score")  # type: ignore[assignment]
        else:
            health.sleep_score = overall if isinstance(overall, int) else None
    else:
        health.sleep_score = _number(sleep_dto, "sleepScore")  # type: ignore[assignment]
    health.hrv_average = _number(hrv_summary, "lastNightAvg", "weeklyAvg")


def _store_daily_summary(
    session: Session, user_id: int, day: date, summary: dict[str, Any]
) -> DailyHealth:
    health = session.scalar(
        select(DailyHealth).where(DailyHealth.user_id == user_id, DailyHealth.day == day)
    )
    if health is None:
        health = DailyHealth(user_id=user_id, day=day)
        session.add(health)
    health.steps = _number(summary, "totalSteps", "steps")  # type: ignore[assignment]
    health.resting_hr = _number(summary, "restingHeartRate", "restingHR")  # type: ignore[assignment]
    health.stress_average = _number(summary, "averageStressLevel")  # type: ignore[assignment]
    health.body_battery_high = _number(summary, "bodyBatteryHighestValue")  # type: ignore[assignment]
    return health


def refresh_daily_summary(session: Session, user_id: int, day: date | None = None) -> None:
    if not sync_lock.acquire(blocking=False):
        raise SyncAlreadyRunningError("Eine Garmin-Synchronisierung läuft bereits.")
    try:
        target_day = day or date.today()
        client = connect_garmin()
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
        start = date.today() - timedelta(days=settings.sync_days - 1)
        _set_progress(
            session,
            run,
            stage="login",
            message="Verbindung zu Garmin Connect wird hergestellt",
        )
        client = _timed_garmin_call(run.id, "login", connect_garmin)
        _set_progress(
            session,
            run,
            stage="activities",
            message=f"Aktivitäten seit {start.strftime('%d.%m.%Y')} werden geladen",
        )
        run.activities_synced = _sync_activities(session, client, account.user_id, start, run.id)
        session.commit()
        for offset in range(settings.sync_days):
            day = start + timedelta(days=offset)

            def report(message: str, current: int = offset + 1) -> None:
                _set_progress(
                    session,
                    run,
                    stage="health",
                    message=message,
                    current=current,
                    total=settings.sync_days,
                )

            _sync_health_day(session, client, account.user_id, day, run.id, report)
            run.health_days_synced += 1
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
