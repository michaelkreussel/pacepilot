from datetime import datetime
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

MAX_ORIGINAL_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_FIT_BYTES = 25 * 1024 * 1024
FIT_RUNNING_TYPES = {
    "running",
    "street_running",
    "track_running",
    "trail_running",
    "trail_run",
    "ultra_run",
    "obstacle_run",
    "treadmill_running",
    "indoor_running",
}


def fit_eligible_activity_type(activity_type: str) -> bool:
    return activity_type.lower() in FIT_RUNNING_TYPES


def activity_fit_path(started_at: datetime, activity_id: str, user_id: int, data_dir: Path) -> Path:
    if not activity_id.isdecimal():
        raise ValueError("Garmin activity ID must be numeric")
    if user_id < 1:
        raise ValueError("User ID must be positive")
    return (
        data_dir
        / "raw"
        / "activities"
        / f"user-{user_id}"
        / str(started_at.year)
        / f"{activity_id}.fit"
    )


def extract_original_fit(payload: bytes) -> bytes | None:
    if not payload or len(payload) > MAX_ORIGINAL_ARCHIVE_BYTES:
        return None
    try:
        with ZipFile(BytesIO(payload)) as archive:
            candidates = [
                entry
                for entry in archive.infolist()
                if not entry.is_dir()
                and entry.filename.lower().endswith(".fit")
                and not entry.flag_bits & 0x1
                and 0 < entry.file_size <= MAX_FIT_BYTES
            ]
            if len(candidates) != 1:
                return None
            fit_data = archive.read(candidates[0], pwd=None)
    except (BadZipFile, OSError, RuntimeError, ValueError):
        return None
    return fit_data if 0 < len(fit_data) <= MAX_FIT_BYTES else None


def write_activity_fit(path: Path, fit_data: bytes) -> None:
    if not fit_data or len(fit_data) > MAX_FIT_BYTES:
        raise ValueError("FIT payload is empty or too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_bytes(fit_data)
    temporary_path.replace(path)
