import logging
import re
import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from typing import Any

from garminconnect.exceptions import GarminConnectTooManyRequestsError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import DailyHealth, GarminAccount, GarminDevice, SyncEvent, SyncRun
from app.models.user import utcnow
from app.onboarding import complete_onboarding
from app.services.garmin.activity_backfill import sync_activity_history
from app.services.garmin.client import (
    GarminUnavailableError,
    connect_garmin_account,
    message_from_exception,
)
from app.services.garmin.health_backfill import (
    GarminPacer,
    HealthProgressEvent,
    retry_after_seconds,
    sync_health_history,
)
from app.services.garmin.locks import (
    GarminAccountBusyError,
    garmin_account_active,
    garmin_account_slot,
)
from app.services.garmin.performance_sync import sync_performance_metrics

logger = logging.getLogger(__name__)

METRIC_LABELS = {
    "daily_summary": "Tagesübersicht",
    "body_battery": "Body Battery",
    "sleep": "Schlaf",
    "hrv": "HRV",
    "spo2": "Sauerstoffsättigung",
    "vo2max": "VO₂max",
    "training_readiness": "Trainingsbereitschaft",
    "training_status": "Trainingsstatus",
    "fitness_age": "Fitnessalter",
    "endurance_score": "Ausdauerwert",
    "hill_score": "Anstiegswert",
    "running_thresholds": "Laufschwellen",
    "cycling_ftp": "Cycling FTP",
    "race_predictions": "Rennprognosen",
    "activities": "Aktivitäten",
    "devices": "Geräte",
    "login": "Anmeldung",
}

SyncAlreadyRunningError = GarminAccountBusyError
_sync_slot = garmin_account_slot
account_sync_active = garmin_account_active


def rate_limit_cooldown_remaining(
    session: Session, account: GarminAccount, *, now: datetime | None = None
) -> int:
    current = now or utcnow()
    if account.rate_limit_until is not None:
        return max(round((account.rate_limit_until - current).total_seconds()), 0)
    if account.sync_status != "rate_limited":
        return 0
    latest = session.scalar(
        select(SyncRun)
        .where(SyncRun.user_id == account.user_id, SyncRun.status == "rate_limited")
        .order_by(SyncRun.id.desc())
        .limit(1)
    )
    if latest is None or latest.finished_at is None:
        return 0
    elapsed = (current - latest.finished_at).total_seconds()
    return max(round(get_settings().garmin_rate_limit_cooldown_seconds - elapsed), 0)


def _record_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value)
    if isinstance(value, Mapping):
        return int(bool(value))
    return 1


def _is_rate_limited(exc: Exception) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while isinstance(current, Exception) and id(current) not in seen:
        seen.add(id(current))
        if (
            isinstance(current, GarminConnectTooManyRequestsError)
            or getattr(getattr(current, "response", None), "status_code", None) == 429
            or re.search(r"\b429\b", str(current)) is not None
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _rate_limit_cooldown_seconds(exc: Exception, default: int) -> int:
    current: BaseException | None = exc
    seen: set[int] = set()
    while isinstance(current, Exception) and id(current) not in seen:
        seen.add(id(current))
        retry_after = getattr(getattr(current, "response", None), "headers", {}).get("Retry-After")
        if retry_after is not None:
            return round(retry_after_seconds(retry_after, default))
        current = current.__cause__ or current.__context__
    return default


def _event(
    session: Session,
    run: SyncRun,
    message: str,
    *,
    level: str = "info",
    category: str = "sync",
    status: str = "info",
    resource: str | None = None,
    day: date | None = None,
    operation: str | None = None,
    duration_ms: float | None = None,
    record_count: int | None = None,
) -> SyncEvent:
    event = SyncEvent(
        sync_run_id=run.id,
        level=level,
        category=category,
        status=status,
        resource=resource,
        day=day,
        operation=operation,
        message=message[:500],
        duration_ms=round(duration_ms) if duration_ms is not None else None,
        record_count=record_count,
    )
    session.add(event)
    session.commit()
    log_level = (
        logging.ERROR
        if level == "error"
        else logging.WARNING
        if level == "warning"
        else logging.INFO
    )
    logger.log(
        log_level,
        "Garmin sync event: %s",
        message,
        extra={
            "sync_run_id": run.id,
            "sync_user_id": run.user_id,
            "sync_category": category,
            "sync_status": status,
            "garmin_resource": resource,
            "sync_day": day.isoformat() if day else None,
            "garmin_operation": operation,
            "duration_ms": event.duration_ms,
            "record_count": record_count,
        },
    )
    return event


def _finish_event(
    session: Session,
    event: SyncEvent,
    *,
    status: str,
    level: str,
    message: str,
    duration_ms: float,
    record_count: int | None = None,
) -> None:
    event.status = status
    event.level = level
    event.message = message[:500]
    event.duration_ms = round(duration_ms)
    event.record_count = record_count


def _timed_garmin_call[T](
    session: Session,
    run: SyncRun,
    *,
    resource: str,
    operation: str,
    message: str,
    call: Callable[[], T],
) -> T:
    run.current_operation = operation
    event = _event(
        session,
        run,
        message,
        category="request",
        status="running",
        resource=resource,
        operation=operation,
    )
    started = time.perf_counter()
    try:
        result = call()
    except Exception as exc:
        duration_ms = (time.perf_counter() - started) * 1000
        _finish_event(
            session,
            event,
            status="error",
            level="error",
            message=f"{message} fehlgeschlagen",
            duration_ms=duration_ms,
        )
        session.commit()
        logger.exception(
            "Garmin sync request failed",
            extra={
                "sync_run_id": run.id,
                "sync_user_id": run.user_id,
                "garmin_resource": resource,
                "garmin_operation": operation,
                "duration_ms": round(duration_ms),
                "error_type": type(exc).__name__,
            },
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    count = _record_count(result)
    _finish_event(
        session,
        event,
        status="success",
        level="success",
        message=f"{message} abgeschlossen",
        duration_ms=duration_ms,
        record_count=count,
    )
    session.commit()
    return result


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
    account = session.scalar(select(GarminAccount).where(GarminAccount.user_id == user_id))
    if account is None or account.connected_at is None:
        raise GarminUnavailableError("Garmin ist noch nicht verbunden.")
    with garmin_account_slot(account.id):
        try:
            target_day = day or date.today()
            client = connect_garmin_account(session, account)
            summary = client.get_user_summary(target_day.isoformat()) or {}
            _store_daily_summary(session, user_id, target_day, summary)
            session.commit()
        except Exception:
            session.rollback()
            raise


def _sync_devices(
    session: Session,
    client: Any,
    account: GarminAccount,
    run: SyncRun,
    pacer: GarminPacer,
) -> None:
    devices = (
        _timed_garmin_call(
            session,
            run,
            resource="devices",
            operation="get_devices",
            message="Garmin-Geräte abrufen",
            call=lambda: pacer.call("devices", client.get_devices),
        )
        or []
    )
    writes = 0
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
        writes += 1
    session.commit()
    _event(
        session,
        run,
        f"{writes} Geräte in der Datenbank aktualisiert",
        category="database",
        status="success",
        level="success",
        resource="devices",
        operation="store_devices",
        record_count=writes,
    )


class _HealthProgress:
    def __init__(self, session: Session, run: SyncRun) -> None:
        self.session = session
        self.run = run
        self.active_operations: dict[tuple[str, date], SyncEvent] = {}
        self.active_planning: dict[str, SyncEvent] = {}

    def __call__(self, update: HealthProgressEvent) -> None:
        if update.phase == "resource_planning_start" and update.resource:
            label = METRIC_LABELS.get(update.resource, update.resource)
            self.run.stage = "health_planning"
            self.run.current_operation = label
            self.run.message = f"Datenbereich für {label} wird ermittelt"
            self.session.commit()
            self.active_planning[update.resource] = _event(
                self.session,
                self.run,
                f"Datenbereich für {label} wird ermittelt",
                category="plan",
                status="running",
                resource=update.resource,
                operation="discover_history",
            )
            return

        if (
            update.phase in {"resource_planning_complete", "resource_planning_error"}
            and update.resource
        ):
            label = METRIC_LABELS.get(update.resource, update.resource)
            event = self.active_planning.pop(update.resource, None)
            if event is None:
                event = _event(
                    self.session,
                    self.run,
                    f"Datenbereich für {label}",
                    category="plan",
                    status="running",
                    resource=update.resource,
                    operation="discover_history",
                )
            planning_error = update.phase == "resource_planning_error"
            _finish_event(
                self.session,
                event,
                status=(
                    "error" if planning_error else "success" if update.reason is None else "skipped"
                ),
                level=(
                    "error" if planning_error else "success" if update.reason is None else "info"
                ),
                message=(
                    f"Datenbereich für {label} fehlgeschlagen: {update.reason}"
                    if planning_error
                    else f"Datenbereich für {label} ermittelt"
                    if update.reason is None
                    else f"{label}: {update.reason}"
                ),
                duration_ms=0,
                record_count=update.record_count,
            )
            self.session.commit()
            return

        if update.phase == "plan":
            planned = update.planned or {}
            days = {day for resource_days in planned.values() for day in resource_days}
            self.run.days_total = len(days)
            self.run.operations_total = sum(
                len(resource_days) for resource_days in planned.values()
            )
            self.run.current_item = 0
            self.run.total_items = self.run.days_total
            self.run.stage = "health"
            self.run.message = f"{self.run.days_total} Tage werden synchronisiert"
            self.session.commit()
            if days:
                _event(
                    self.session,
                    self.run,
                    (
                        f"Health-Plan: {len(days)} Tage von {min(days).isoformat()} "
                        f"bis {max(days).isoformat()}, {self.run.operations_total} Operationen"
                    ),
                    category="plan",
                    status="success",
                    level="success",
                )
            else:
                _event(
                    self.session,
                    self.run,
                    "Keine Health-Tage müssen aktualisiert werden",
                    category="plan",
                    status="skipped",
                    level="info",
                )
            return

        label = METRIC_LABELS.get(update.resource or "", update.resource or "Garmin")
        if update.phase == "day_start" and update.day is not None:
            self.run.current_day = update.day
            self.run.current_operation = None
            self.run.message = f"Daten für {update.day.strftime('%d.%m.%Y')} werden geladen"
            self.session.commit()
            return

        if update.phase == "operation_start" and update.resource and update.day:
            self.run.current_day = update.day
            self.run.current_operation = label
            self.run.message = f"{label} für {update.day.strftime('%d.%m.%Y')} wird geladen"
            event = _event(
                self.session,
                self.run,
                f"{label} wird abgerufen",
                category="metric",
                status="running",
                resource=update.resource,
                day=update.day,
                operation="fetch_and_store",
            )
            self.active_operations[(update.resource, update.day)] = event
            return

        if (
            update.phase in {"operation_complete", "operation_error"}
            and update.resource
            and update.day
        ):
            event = self.active_operations.pop((update.resource, update.day), None)
            if event is None:
                event = _event(
                    self.session,
                    self.run,
                    label,
                    category="metric",
                    status="running",
                    resource=update.resource,
                    day=update.day,
                    operation="fetch_and_store",
                )
            if update.phase == "operation_complete":
                suffix = (
                    f"{update.record_count} Datensätze"
                    if update.record_count is not None
                    else "gespeichert"
                )
                _finish_event(
                    self.session,
                    event,
                    status="success",
                    level="success",
                    message=f"{label}: {suffix}",
                    duration_ms=update.duration_ms or 0,
                    record_count=update.record_count,
                )
                self.run.operations_completed += 1
            else:
                self.run.operations_completed += 1
                _finish_event(
                    self.session,
                    event,
                    status="error",
                    level="error",
                    message=f"{label}: {update.reason or 'Operation fehlgeschlagen'}",
                    duration_ms=update.duration_ms or 0,
                )
            self.session.commit()
            return

        if update.phase == "operation_skipped" and update.resource and update.day:
            self.run.operations_completed += 1
            _event(
                self.session,
                self.run,
                f"{label} übersprungen: {update.reason or 'keine Aktualisierung erforderlich'}",
                category="metric",
                status="skipped",
                level="info",
                resource=update.resource,
                day=update.day,
                operation="skip",
            )
            return

        if update.phase == "resource_skipped" and update.resource:
            _event(
                self.session,
                self.run,
                f"{label} übersprungen: {update.reason or 'nicht verfügbar'}",
                category="metric",
                status="skipped",
                level="warning" if "unsupported" in (update.reason or "") else "info",
                resource=update.resource,
            )
            return

        if update.phase == "day_complete" and update.day is not None:
            self.run.days_completed = min(self.run.days_completed + 1, self.run.days_total)
            self.run.health_days_synced = self.run.days_completed
            self.run.current_item = self.run.days_completed
            self.run.current_operation = "Tag abgeschlossen"
            self.run.message = f"{update.day.strftime('%d.%m.%Y')} vollständig synchronisiert"
            self.session.commit()
            _event(
                self.session,
                self.run,
                "Tag vollständig synchronisiert",
                category="day",
                status="success",
                level="success",
                day=update.day,
            )


def sync_garmin(
    session: Session,
    account: GarminAccount,
    *,
    wait_for_slot: bool = False,
    slot_acquired: bool = False,
) -> SyncRun:
    slot = nullcontext() if slot_acquired else garmin_account_slot(account.id, wait=wait_for_slot)
    with slot:
        run = SyncRun(
            user_id=account.user_id,
            stage="starting",
            message="Synchronisierung wird vorbereitet",
        )
        started = time.perf_counter()
        settings = get_settings()
        try:
            session.add(run)
            account.sync_status = "running"
            account.sync_error = None
            session.commit()
            _event(
                session,
                run,
                f"Sync für Benutzer {account.user_id} und Konto {account.id} gestartet",
                category="sync",
                status="running",
            )
            run.stage = "login"
            run.message = "Garmin-Sitzung wird für dieses Konto initialisiert"
            session.commit()
            client = _timed_garmin_call(
                session,
                run,
                resource="login",
                operation="initialize_account_session",
                message="Kontoeigene Garmin-Sitzung initialisieren",
                call=lambda: connect_garmin_account(session, account),
            )
            pacer = GarminPacer(
                settings.garmin_call_delay_seconds,
                {"sync_run_id": run.id, "sync_user_id": account.user_id},
                rate_limit_cooldown=settings.garmin_rate_limit_cooldown_seconds,
            )

            run.stage = "activities"
            run.message = "Aktivitätsverlauf wird geprüft"
            run.current_operation = "Aktivitätsliste"
            session.commit()
            active_activities: dict[str, SyncEvent] = {}

            def report_activity(activity_id: str, current: int, total: int) -> None:
                run.activities_processed = current - 1
                run.activities_total = total
                run.current_operation = f"Aktivität {activity_id}"
                run.message = f"Aktivität {current} wird geprüft"
                active_activities[activity_id] = _event(
                    session,
                    run,
                    f"Aktivität {activity_id} wird geprüft",
                    category="activity",
                    status="running",
                    resource="activities",
                    operation="sync_activity",
                )

            def complete_activity(
                activity_id: str,
                current: int,
                total: int,
                outcome: str,
                activity_day: date | None,
                duration_ms: float,
            ) -> None:
                run.activities_processed = current
                run.activities_total = total
                event = active_activities.pop(activity_id)
                skipped = outcome == "skipped"
                _finish_event(
                    session,
                    event,
                    status="skipped" if skipped else "success",
                    level="info" if skipped else "success",
                    message=(
                        f"Aktivität {activity_id} unverändert"
                        if skipped
                        else f"Aktivität {activity_id} {outcome}"
                    ),
                    duration_ms=duration_ms,
                    record_count=0 if skipped else 1,
                )
                event.day = activity_day
                session.commit()

            activity_result = sync_activity_history(
                session,
                client,
                account.user_id,
                delay=settings.garmin_call_delay_seconds,
                initial_enrichment_limit=settings.garmin_activity_initial_enrichment,
                incremental_enrichment_limit=settings.garmin_activity_enrichment_per_sync,
                pacer=pacer,
                progress=report_activity,
                completion=complete_activity,
                log_context={"sync_run_id": run.id, "sync_user_id": account.user_id},
            )
            run.activities_synced = activity_result.inserted + activity_result.updated
            session.commit()
            _event(
                session,
                run,
                (
                    f"Aktivitäten abgeschlossen: {activity_result.inserted} neu, "
                    f"{activity_result.updated} aktualisiert, {activity_result.skipped} unverändert"
                ),
                category="activity",
                status="success",
                level="success",
                resource="activities",
                record_count=run.activities_synced,
            )
            if activity_result.enrichment_deferred:
                _event(
                    session,
                    run,
                    (
                        f"{activity_result.enrichment_deferred} Aktivitätsdetails werden "
                        "in späteren Läufen ergänzt"
                    ),
                    category="activity",
                    status="deferred",
                    level="info",
                    resource="activities",
                    operation="defer_enrichment",
                    record_count=activity_result.enrichment_deferred,
                )

            run.stage = "health_planning"
            run.message = "Health-Datenbereiche und Metriken werden ermittelt"
            run.current_operation = "Health-Planung"
            session.commit()
            health_progress = _HealthProgress(session, run)
            health_result = sync_health_history(
                session,
                client,
                account.user_id,
                overlap_days=settings.health_sync_overlap_days,
                delay=settings.garmin_call_delay_seconds,
                pacer=pacer,
                progress=health_progress,
                log_context={"sync_run_id": run.id, "sync_user_id": account.user_id},
            )
            run.health_days_synced = run.days_completed
            session.commit()

            run.stage = "performance"
            run.message = "Garmin-Leistungswerte werden aktualisiert"
            run.current_day = date.today()
            run.current_operation = "Leistung und Schwellen"
            session.commit()
            performance_result = sync_performance_metrics(
                session,
                client,
                account.user_id,
                delay=settings.garmin_call_delay_seconds,
                pacer=pacer,
                log_context={"sync_run_id": run.id, "sync_user_id": account.user_id},
            )
            attempted_performance = sum(
                not item.skipped for item in performance_result.resources.values()
            )
            run.operations_total += attempted_performance
            run.operations_completed += attempted_performance
            for resource, item in performance_result.resources.items():
                label = METRIC_LABELS[resource]
                _event(
                    session,
                    run,
                    (
                        f"{label}: {item.stored_values} Werte gespeichert"
                        if item.status in {"ok", "partial"}
                        else f"{label}: keine Daten"
                        if item.status == "empty"
                        else f"{label}: nicht unterstützt"
                        if item.status == "unsupported"
                        else f"{label}: {item.status}"
                    ),
                    category="metric",
                    status=(
                        "success"
                        if item.status in {"ok", "partial"}
                        else "skipped"
                        if item.status in {"empty", "unsupported"} or item.skipped
                        else "error"
                    ),
                    level=(
                        "success"
                        if item.status in {"ok", "partial"}
                        else "info"
                        if item.status in {"empty", "unsupported"} or item.skipped
                        else "warning"
                    ),
                    resource=resource,
                    day=date.today(),
                    operation="sync_performance_metric",
                    record_count=item.stored_values,
                )
            session.commit()

            run.stage = "devices"
            run.message = "Garmin-Geräte werden aktualisiert"
            run.current_operation = "Geräteliste"
            session.commit()
            _sync_devices(session, client, account, run, pacer)

            account.last_sync_at = datetime.now(UTC).replace(tzinfo=None)
            account.rate_limit_until = None
            account.sync_status = "ok"
            account.sync_error = None
            run.status = "ok"
            run.stage = "complete"
            run.message = "Synchronisierung abgeschlossen"
            run.current_operation = None
            run.current_item = run.days_completed
            run.total_items = run.days_total
            run.finished_at = utcnow()
            complete_onboarding(session, account.user_id)
            session.commit()
            duration_ms = (time.perf_counter() - started) * 1000
            _event(
                session,
                run,
                (
                    f"Sync abgeschlossen: {run.activities_synced} Aktivitäten, "
                    f"{run.days_completed} Health-Tage, "
                    f"{health_result.api_calls} Health-API-Aufrufe, "
                    f"{performance_result.stored_values} Leistungswerte"
                ),
                category="sync",
                status="success",
                level="success",
                duration_ms=duration_ms,
            )
        except Exception as exc:
            session.rollback()
            failed_account = session.get(GarminAccount, account.id)
            failed_run = session.get(SyncRun, run.id) if run.id is not None else None
            error_message = message_from_exception(exc)
            rate_limited = _is_rate_limited(exc)
            cooldown_seconds = settings.garmin_rate_limit_cooldown_seconds
            if rate_limited:
                cooldown_seconds = _rate_limit_cooldown_seconds(exc, cooldown_seconds)
                GarminPacer.defer_all(cooldown_seconds)
            if failed_account is not None:
                failed_account.sync_status = "rate_limited" if rate_limited else "error"
                failed_account.sync_error = error_message
                failed_account.rate_limit_until = (
                    utcnow() + timedelta(seconds=cooldown_seconds) if rate_limited else None
                )
            if failed_run is not None:
                failed_run.status = "rate_limited" if rate_limited else "error"
                failed_run.stage = "cooldown" if rate_limited else "error"
                failed_run.message = (
                    "Garmin-Pause wegen Anfragelimit"
                    if rate_limited
                    else "Synchronisierung fehlgeschlagen"
                )
                failed_run.error = error_message
                failed_run.finished_at = utcnow()
                session.add(
                    SyncEvent(
                        sync_run_id=failed_run.id,
                        level="warning" if rate_limited else "error",
                        category="sync",
                        status="rate_limited" if rate_limited else "error",
                        message=(
                            "Synchronisierung pausiert; der nächste Lauf setzt sie fort"
                            if rate_limited
                            else "Synchronisierung fehlgeschlagen"
                        ),
                        duration_ms=round((time.perf_counter() - started) * 1000),
                    )
                )
            session.commit()
            logger.exception(
                "Garmin sync aborted",
                extra={
                    "sync_run_id": run.id,
                    "sync_user_id": account.user_id,
                    "garmin_account_id": account.id,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                    "error_type": type(exc).__name__,
                },
            )
        return run
