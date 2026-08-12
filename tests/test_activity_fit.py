from datetime import datetime, timedelta
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import fitdecode

from app.models import Activity
from app.services.analytics import detail_evidence as evidence_module
from app.services.analytics.detail_evidence import _fit_samples, _rolling_effort
from app.services.garmin.activity_fit import extract_original_fit


def test_extract_original_fit_requires_one_bounded_fit_member() -> None:
    archive = BytesIO()
    with ZipFile(archive, "w", ZIP_DEFLATED) as output:
        output.writestr("activity.fit", b"FIT-data")
        output.writestr("notes.txt", b"ignored")

    assert extract_original_fit(archive.getvalue()) == b"FIT-data"
    assert extract_original_fit(b"not-a-zip") is None

    multiple = BytesIO()
    with ZipFile(multiple, "w") as output:
        output.writestr("first.fit", b"one")
        output.writestr("second.FIT", b"two")
    assert extract_original_fit(multiple.getvalue()) is None


class _Message:
    def __init__(self, name: str, **values: object) -> None:
        self.name = name
        self.values = values

    def get_value(self, name: str, *, fallback: object = None) -> object:
        return self.values.get(name, fallback)


class _Reader:
    def __init__(self, *_args, **_kwargs) -> None:
        start = datetime(2026, 8, 12, 8)
        self.frames = [
            _Message("record", timestamp=start, distance=0),
            _Message("record", timestamp=start + timedelta(seconds=300), distance=1_000),
            _Message(
                "event",
                timestamp=start + timedelta(seconds=300),
                event="timer",
                event_type="stop_all",
            ),
            _Message(
                "event",
                timestamp=start + timedelta(seconds=420),
                event="timer",
                event_type="start",
            ),
            _Message("record", timestamp=start + timedelta(seconds=720), distance=2_000),
        ]

    def __enter__(self):
        return iter(self.frames)

    def __exit__(self, *_args) -> None:
        return None


def test_fit_samples_use_timer_events_for_pause_aware_efforts(monkeypatch, tmp_path) -> None:
    fit_path = tmp_path / "paused.fit"
    fit_path.write_bytes(b"placeholder")
    activity = Activity(
        id=1,
        user_id=1,
        garmin_activity_id="123",
        name="Paused",
        activity_type="running",
        started_at=datetime(2026, 8, 12, 8),
        fit_file=str(fit_path),
    )
    monkeypatch.setattr(evidence_module.fitdecode, "FitReader", _Reader)
    monkeypatch.setattr(
        evidence_module.fitdecode,
        "FitDataMessage",
        _Message,
    )

    samples = _fit_samples(activity)
    effort = _rolling_effort(samples, 2_000)

    assert [sample.timer_s for sample in samples] == [0, 300, 600]
    assert [sample.elapsed_s for sample in samples] == [0, 300, 720]
    assert effort == (600, 3)
    assert fitdecode.CrcCheck.WARN is not None
