from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import get_settings
from app.http_security import RequestIdMiddleware


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


def test_request_id_is_generated_for_every_response(client: TestClient) -> None:
    response = client.get("/api/health")

    assert str(UUID(response.headers["X-Request-ID"])) == response.headers["X-Request-ID"]


def test_valid_request_id_is_preserved(client: TestClient) -> None:
    request_id = str(uuid4())

    response = client.get("/api/health", headers={"X-Request-ID": request_id})

    assert response.headers["X-Request-ID"] == request_id


def test_invalid_request_id_is_replaced_on_error_response(client: TestClient) -> None:
    response = client.get("/does-not-exist", headers={"X-Request-ID": "not-a-uuid"})

    assert response.status_code == 404
    assert response.headers["X-Request-ID"] != "not-a-uuid"
    assert str(UUID(response.headers["X-Request-ID"])) == response.headers["X-Request-ID"]


def test_unhandled_error_response_includes_request_id() -> None:
    error_app = FastAPI()
    error_app.add_middleware(RequestIdMiddleware)
    error_app.add_exception_handler(Exception, main_module.unhandled_exception)

    @error_app.get("/boom", response_class=PlainTextResponse)
    def boom() -> str:
        raise RuntimeError("boom")

    with TestClient(error_app, raise_server_exceptions=False) as error_client:
        response = error_client.get("/boom")

    assert response.status_code == 500
    assert str(UUID(response.headers["X-Request-ID"])) == response.headers["X-Request-ID"]
