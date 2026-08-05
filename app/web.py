from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "–"
    hours, remainder = divmod(int(seconds), 3600)
    minutes = remainder // 60
    return f"{hours}:{minutes:02d} h" if hours else f"{minutes} min"


def format_distance(meters: float | int | None) -> str:
    return "–" if meters is None else f"{meters / 1000:.1f} km"


def format_date(value: date | datetime | None, include_time: bool = False) -> str:
    if value is None:
        return "–"
    if isinstance(value, datetime) and include_time:
        return value.strftime("%d.%m.%Y, %H:%M")
    return value.strftime("%d.%m.%Y")


templates.env.filters.update(
    duration=format_duration,
    distance=format_distance,
    date=format_date,
    datetime=lambda value: format_date(value, include_time=True),
)


def context(request: Request, **values: Any) -> dict[str, Any]:
    return {"request": request, **values}
