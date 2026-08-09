from pathlib import Path
from typing import Any

import pytest

from app import main as main_module
from app.config import get_settings


@pytest.mark.asyncio
async def test_lifespan_migrates_before_starting_scheduler(
    monkeypatch: Any, tmp_path: Path
) -> None:
    events: list[str] = []
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "garmin_token_dir", tmp_path / "tokens")
    monkeypatch.setattr(main_module, "upgrade_database", lambda: events.append("migrate"))
    monkeypatch.setattr(main_module, "start_scheduler", lambda: events.append("start"))
    monkeypatch.setattr(main_module, "stop_scheduler", lambda: events.append("stop"))

    async with main_module.lifespan(main_module.app):
        assert events == ["migrate", "start"]

    assert events == ["migrate", "start", "stop"]


@pytest.mark.asyncio
async def test_lifespan_aborts_before_scheduler_when_migration_fails(
    monkeypatch: Any, tmp_path: Path
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "garmin_token_dir", tmp_path / "tokens")
    monkeypatch.setattr(
        main_module,
        "upgrade_database",
        lambda: (_ for _ in ()).throw(RuntimeError("migration failed")),
    )
    started = False

    def start_scheduler() -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(main_module, "start_scheduler", start_scheduler)

    with pytest.raises(RuntimeError, match="migration failed"):
        async with main_module.lifespan(main_module.app):
            pass

    assert not started
