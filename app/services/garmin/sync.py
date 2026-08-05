import gzip
import json
import threading
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Activity, DailyHealth, GarminAccount, GarminDevice, SyncRun
from app.models.user import utcnow
from app.services.garmin.client import connect_garmin, message_from_exception

sync_lock = threading.Lock()


class SyncAlreadyRunningError(RuntimeError):
    pass


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


def _sync_activities(session: Session, client: Any, user_id: int, start: date) -> int:
    payload = client.get_activities_by_date(start.isoformat(), date.today().isoformat()) or []
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
        activity.synced_at = utcnow()
        count += 1
    session.flush()
    return count


def _sync_health_day(session: Session, client: Any, user_id: int, day: date) -> None:
    day_string = day.isoformat()
    summary = client.get_user_summary(day_string) or {}
    try:
        sleep = client.get_sleep_data(day_string) or {}
    except Exception:
        sleep = {}
    try:
        hrv = client.get_hrv_data(day_string) or {}
    except Exception:
        hrv = {}

    sleep_dto = sleep.get("dailySleepDTO") if isinstance(sleep, dict) else {}
    sleep_dto = sleep_dto if isinstance(sleep_dto, dict) else {}
    hrv_summary = hrv.get("hrvSummary") if isinstance(hrv, dict) else {}
    hrv_summary = hrv_summary if isinstance(hrv_summary, dict) else {}
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


def _sync_devices(session: Session, client: Any, account: GarminAccount) -> None:
    for item in client.get_devices() or []:
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
    run = SyncRun(user_id=account.user_id)
    session.add(run)
    account.sync_status = "running"
    account.sync_error = None
    session.commit()
    try:
        client = connect_garmin()
        settings = get_settings()
        start = date.today() - timedelta(days=settings.sync_days - 1)
        run.activities_synced = _sync_activities(session, client, account.user_id, start)
        for offset in range(settings.sync_days):
            _sync_health_day(session, client, account.user_id, start + timedelta(days=offset))
            run.health_days_synced += 1
        _sync_devices(session, client, account)
        account.last_sync_at = datetime.now(UTC).replace(tzinfo=None)
        account.sync_status = "ok"
        account.sync_error = None
        run.status = "ok"
        run.finished_at = utcnow()
        session.commit()
    except Exception as exc:
        session.rollback()
        failed_account = session.get(GarminAccount, account.id)
        failed_run = session.get(SyncRun, run.id)
        if failed_account is not None:
            failed_account.sync_status = "error"
            failed_account.sync_error = message_from_exception(exc)
        if failed_run is not None:
            failed_run.status = "error"
            failed_run.error = message_from_exception(exc)
            failed_run.finished_at = utcnow()
        session.commit()
    finally:
        sync_lock.release()
    return run
