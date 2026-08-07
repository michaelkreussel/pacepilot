from typing import Any

from app.services.garmin.activity_details import normalize_activity_details


def test_activity_details_normalize_running_metrics() -> None:
    details: dict[str, Any] = {
        "metricDescriptors": [
            {"key": "directHeartRate", "metricsIndex": 2},
            {"key": "sumElapsedDuration", "metricsIndex": 0},
            {"key": "directSpeed", "metricsIndex": 1},
            {"key": "directRunCadence", "metricsIndex": 3},
            {"key": "directLatitude", "metricsIndex": 4},
            {"key": "directLongitude", "metricsIndex": 5},
            {"key": "sumMovingDuration", "metricsIndex": 6},
            {"key": "sumDuration", "metricsIndex": 7},
            {"key": "sumDistance", "metricsIndex": 8},
            {"key": "directPower", "metricsIndex": 9},
        ],
        "activityDetailMetrics": [
            {"metrics": [0, 2.5, 140, 85, 50.0, 9.0, 0, 0, 0, 250]},
            {"metrics": [60, 0, 150, 87, 50.1, 9.1, 40, 45, 100, 300]},
        ],
    }

    result = normalize_activity_details(details, "running")

    assert result["series"]["heart_rate"] == [[0.0, 140.0], [40.0, 150.0]]
    assert result["series"]["speed"] == [[0.0, 9.0]]
    assert result["series"]["pace"] == [[0.0, 400.0]]
    assert result["series"]["cadence"] == [[0.0, 170.0], [40.0, 174.0]]
    assert result["route"] == [[50.0, 9.0], [50.1, 9.1]]
    assert result["summary"]["moving_time"] == 40.0
    assert result["summary"]["timer_time"] == 45.0
    assert result["summary"]["elapsed_time"] == 60.0
    assert result["summary"]["average_pace"] == 400.0
    assert result["summary"]["average_power"] == 275.0


def test_activity_details_remove_stationary_pace_outlier() -> None:
    details: dict[str, Any] = {
        "metricDescriptors": [
            {"key": "sumMovingDuration", "metricsIndex": 0},
            {"key": "directSpeed", "metricsIndex": 1},
        ],
        "activityDetailMetrics": [
            {"metrics": [0, 3.0]},
            {"metrics": [10, 3.1]},
            {"metrics": [20, 0.2]},
        ],
    }

    result = normalize_activity_details(details, "running")

    assert len(result["series"]["pace"]) == 2
    assert all(point[1] < 500 for point in result["series"]["pace"])


def test_activity_details_fall_back_when_moving_time_does_not_advance() -> None:
    details: dict[str, Any] = {
        "metricDescriptors": [
            {"key": "sumMovingDuration", "metricsIndex": 0},
            {"key": "sumDuration", "metricsIndex": 1},
            {"key": "directHeartRate", "metricsIndex": 2},
        ],
        "activityDetailMetrics": [
            {"metrics": [0, 0, 100]},
            {"metrics": [0, 30, 120]},
            {"metrics": [0, 60, 130]},
        ],
    }

    result = normalize_activity_details(details, "strength_training")

    assert result["time_axis_label"] == "Timerzeit"
    assert result["series"]["heart_rate"] == [
        [0.0, 100.0],
        [30.0, 120.0],
        [60.0, 130.0],
    ]
