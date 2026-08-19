from app.web import format_precise_duration, format_speed_as_pace


def test_performance_values_use_pace_and_precise_duration_formats() -> None:
    assert format_speed_as_pace(4.0) == "4:10 min/km"
    assert format_speed_as_pace(3.5) == "4:46 min/km"
    assert format_speed_as_pace(0) == "–"
    assert format_speed_as_pace(None) == "–"
    assert format_precise_duration(1_245) == "20:45 min"
    assert format_precise_duration(5_850) == "1:37:30 h"
