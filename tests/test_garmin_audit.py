from datetime import date
from typing import Any

from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from scripts.audit_garmin_history import (
    AuditHalted,
    AuditRunner,
    MetricSpec,
    body_battery_data,
    classify_exception,
    daily_summary_data,
    field_paths,
    find_earliest_date,
    hrv_data,
    intensity_minutes_data,
    respiration_data,
    sleep_data,
    spo2_data,
    steps_data,
)


def test_find_earliest_date_uses_progressive_probes() -> None:
    first_day = date(2021, 4, 17)
    calls: list[date] = []

    def fetch(day: str) -> dict[str, Any]:
        parsed = date.fromisoformat(day)
        calls.append(parsed)
        return {"totalSteps": 1000} if parsed >= first_day else {"totalSteps": 0}

    result = find_earliest_date(
        MetricSpec("daily_health", "get_user_summary", fetch, daily_summary_data),
        AuditRunner(delay=0, max_calls=100),
        end_date=date(2026, 8, 7),
        min_date=date(2005, 1, 1),
    )

    assert result["earliest_observed"] == "2021-04-17"
    assert result["boundary_confirmed"] is True
    assert len(calls) < 40


def test_find_earliest_date_reports_empty_metric() -> None:
    result = find_earliest_date(
        MetricSpec("hrv", "get_hrv_data", lambda _day: None, hrv_data),
        AuditRunner(delay=0, max_calls=20),
        end_date=date(2026, 8, 7),
        min_date=date(2005, 1, 1),
    )

    assert result["available"] is False
    assert result["earliest_observed"] is None
    assert result["probe_count"] == 9


def test_presence_checks_do_not_treat_missing_values_as_data() -> None:
    assert not daily_summary_data({"calendarDate": "2026-08-07", "totalSteps": 0})
    assert daily_summary_data({"totalSteps": 1})
    assert not sleep_data({"dailySleepDTO": {"sleepTimeSeconds": 0}})
    assert sleep_data({"dailySleepDTO": {"deepSleepSeconds": 1200}})
    assert not hrv_data({"hrvSummary": {"lastNightAvg": None}})
    assert hrv_data({"hrvSummary": {"lastNightAvg": 51}})
    assert not body_battery_data([])
    assert not body_battery_data([{"bodyBatteryValuesArray": [[1, None]]}])
    assert body_battery_data([{"bodyBatteryValuesArray": [[1, 80]]}])
    assert not steps_data([{"steps": 0}])
    assert steps_data([{"steps": 12}])
    assert respiration_data({"avgWakingRespirationValue": 14.2})
    assert not spo2_data({"averageSpO2": None, "continuousReadingDTOList": []})
    assert intensity_minutes_data({"vigorousMinutes": 4})


def test_field_paths_omit_personal_identifiers_but_keep_metric_names() -> None:
    result = field_paths(
        {
            "userProfilePK": 123,
            "activityName": "Secret route",
            "summary": {"averageHR": 145},
            "samples": [{"directPower": 250, "latitude": 50.0}],
        }
    )

    assert "userProfilePK" not in result
    assert "activityName" not in result
    assert "samples[].latitude" not in result
    assert "summary.averageHR" in result
    assert "samples[].directPower" in result


def test_api_error_classification() -> None:
    assert classify_exception(GarminConnectAuthenticationError("401")) == "authentication_failure"
    assert classify_exception(GarminConnectTooManyRequestsError("429")) == "rate_limited"
    assert classify_exception(GarminConnectNotFoundError("404")) == "unsupported"
    assert classify_exception(RuntimeError("server unavailable")) == "api_failure"


def test_runner_halts_on_authentication_and_rate_limit() -> None:
    for error in (
        GarminConnectAuthenticationError("bad token"),
        GarminConnectTooManyRequestsError("slow down"),
    ):
        runner = AuditRunner(delay=0, max_calls=1)

        try:
            runner.call("probe", lambda error=error: (_ for _ in ()).throw(error))
        except AuditHalted:
            pass
        else:
            raise AssertionError("AuditRunner did not halt")
