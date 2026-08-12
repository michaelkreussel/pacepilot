from datetime import date, datetime
from typing import Any

from app.models import Activity, DailyFitness, User
from app.services.analytics.automatic_profile import get_automatic_athlete_profile
from app.services.analytics.detail_evidence import analyze_detail_evidence
from app.services.garmin.activity_details import activity_details_path, write_activity_details


def _detail_payload(
    *,
    distance_m: int = 10_000,
    duration_s: int = 3_600,
    start_hr: float = 140,
    end_hr: float = 147,
    speeds: list[float] | None = None,
) -> dict[str, Any]:
    descriptors = [
        {"key": "sumDuration", "metricsIndex": 0},
        {"key": "sumElapsedDuration", "metricsIndex": 1},
        {"key": "sumDistance", "metricsIndex": 2},
        {"key": "directHeartRate", "metricsIndex": 3},
        {"key": "directElevation", "metricsIndex": 4},
    ]
    rows = []
    step = 60
    count = duration_s // step
    speed_values = speeds or [distance_m / duration_s] * count
    distance = 0.0
    for index in range(count + 1):
        timer = index * step
        if index:
            distance += speed_values[min(index - 1, len(speed_values) - 1)] * step
        heart_rate = start_hr + (end_hr - start_hr) * index / count
        rows.append({"metrics": [timer, timer, distance, heart_rate, 100 + index * 0.1]})
    return {"metricDescriptors": descriptors, "activityDetailMetrics": rows}


def _run(user_id: int, key: str, day: date) -> Activity:
    return Activity(
        user_id=user_id,
        garmin_activity_id=key,
        name=key,
        activity_type="running",
        started_at=datetime.combine(day, datetime.min.time()),
        duration_s=3_600,
        elapsed_duration_s=3_600,
        moving_duration_s=3_600,
        distance_m=10_000,
        details_complete=True,
    )


def test_detail_evidence_finds_arbitrary_rolling_best_efforts(
    session_factory, monkeypatch, tmp_path
):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Rolling")
        session.add(user)
        session.flush()
        run = _run(user.id, "1001", as_of)
        session.add(run)
        session.flush()
        path = activity_details_path(run.started_at, run.garmin_activity_id, user.id)
        write_activity_details(path, _detail_payload())
        run.details_file = str(path)
        session.commit()

        profile = get_automatic_athlete_profile(session, user.id, as_of=as_of)

    efforts = {item.distance_key: item for item in profile.best_efforts}
    assert efforts["1k"].duration_s == 360
    assert efforts["5k"].duration_s == 1_800
    assert efforts["10k"].duration_s == 3_600
    assert efforts["5k"].source == "sampled_detail"
    assert efforts["5k"].clock == "timer"
    assert profile.detail_evidence.coverage.sampled_detail_activities == 1


def test_detail_evidence_reports_drift_after_three_steady_runs(
    session_factory, monkeypatch, tmp_path
):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Drift")
        session.add(user)
        session.flush()
        for index in range(3):
            run = _run(user.id, str(2000 + index), as_of)
            session.add(run)
            session.flush()
            path = activity_details_path(run.started_at, run.garmin_activity_id, user.id)
            write_activity_details(path, _detail_payload(start_hr=140, end_hr=147))
            run.details_file = str(path)
        session.commit()
        activities = list(session.query(Activity).order_by(Activity.id))

        evidence = analyze_detail_evidence(activities, [], as_of)

    assert evidence.heart_rate_drift.sufficient is True
    assert evidence.heart_rate_drift.sample_sessions == 3
    assert 2 <= (evidence.heart_rate_drift.median_percent or 0) < 6
    assert evidence.heart_rate_drift.confidence == "medium"


def test_detail_evidence_finds_stable_threshold_segment(session_factory, monkeypatch, tmp_path):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Schwelle")
        session.add(user)
        session.flush()
        run = _run(user.id, "3001", as_of)
        run.distance_m = 14_400
        session.add_all(
            [
                run,
                DailyFitness(
                    user_id=user.id,
                    day=as_of,
                    lactate_threshold_speed_mps=4.0,
                    lactate_threshold_hr=170,
                ),
            ]
        )
        session.flush()
        path = activity_details_path(run.started_at, run.garmin_activity_id, user.id)
        write_activity_details(
            path,
            _detail_payload(
                distance_m=14_400,
                start_hr=168,
                end_hr=171,
                speeds=[4.0] * 60,
            ),
        )
        run.details_file = str(path)
        session.commit()
        activities = list(session.query(Activity))
        fitness = list(session.query(DailyFitness))

        evidence = analyze_detail_evidence(activities, fitness, as_of)

    assert evidence.threshold_segments
    segment = evidence.threshold_segments[0]
    assert segment.duration_s == 2_400
    assert segment.pace_s_per_km == 250
    assert segment.pace_cv_percent == 0
    assert segment.heart_rate is not None


def test_detail_evidence_keeps_garmin_record_precedence(session_factory, monkeypatch, tmp_path):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Vorrang")
        session.add(user)
        session.flush()
        run = _run(user.id, "4001", as_of)
        session.add_all(
            [
                run,
                DailyFitness(user_id=user.id, day=as_of, personal_record_5k_seconds=1_700),
            ]
        )
        session.flush()
        path = activity_details_path(run.started_at, run.garmin_activity_id, user.id)
        write_activity_details(path, _detail_payload())
        run.details_file = str(path)
        session.commit()

        profile = get_automatic_athlete_profile(session, user.id, as_of=as_of)

    effort = next(item for item in profile.best_efforts if item.distance_key == "5k")
    assert effort.duration_s == 1_700
    assert effort.source == "garmin_personal_record"


def test_automatic_profile_can_skip_detail_file_processing(session_factory, monkeypatch, tmp_path):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Kompakt")
        session.add(user)
        session.flush()
        run = _run(user.id, "5001", as_of)
        session.add(run)
        session.flush()
        path = activity_details_path(run.started_at, run.garmin_activity_id, user.id)
        write_activity_details(path, _detail_payload())
        run.details_file = str(path)
        session.commit()

        profile = get_automatic_athlete_profile(
            session,
            user.id,
            as_of=as_of,
            include_detail_evidence=False,
        )

    assert profile.detail_evidence.coverage.eligible_activities == 0
    assert "detail_evidence_unavailable" not in profile.warnings


def test_detail_evidence_cache_reuses_files_and_invalidates_on_change(
    session_factory, monkeypatch, tmp_path
):
    from app.config import get_settings
    from app.services.analytics import detail_evidence as evidence_module

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    evidence_module.clear_detail_evidence_cache()
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Cache")
        session.add(user)
        session.flush()
        run = _run(user.id, "6001", as_of)
        session.add(run)
        session.flush()
        path = activity_details_path(run.started_at, run.garmin_activity_id, user.id)
        write_activity_details(path, _detail_payload())
        run.details_file = str(path)
        session.commit()
        activities = list(session.query(Activity))
        calls = 0
        original = evidence_module._sampled_detail_samples

        def counted(activity):
            nonlocal calls
            calls += 1
            return original(activity)

        monkeypatch.setattr(evidence_module, "_sampled_detail_samples", counted)
        first = analyze_detail_evidence(activities, [], as_of)
        second = analyze_detail_evidence(activities, [], as_of)
        write_activity_details(path, _detail_payload(duration_s=3_660, start_hr=130, end_hr=150))
        third = analyze_detail_evidence(activities, [], as_of)

    assert first is second
    assert third is not second
    assert calls == 2
    evidence_module.clear_detail_evidence_cache()
