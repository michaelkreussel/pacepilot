import json
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Activity, GarminAccount, User
from app.rate_limits import limiter
from app.services.account_lifecycle import (
    collect_user_rows,
    create_user_export,
    delete_user_account,
    remove_export,
    repair_account_lifecycle,
)


def _configure_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path / "data")
    monkeypatch.setattr(get_settings(), "garmin_token_dir", tmp_path / "tokens")


def test_complete_export_is_user_scoped_and_excludes_tokens(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_storage(monkeypatch, tmp_path)
    with session_factory() as session:
        first = User(display_name="Export Athlete")
        second = User(display_name="Other Athlete")
        session.add_all([first, second])
        session.flush()
        account = GarminAccount(user_id=first.id, email="athlete@example.invalid")
        session.add(account)
        raw = tmp_path / "data" / "raw" / "activities" / f"user-{first.id}" / "2026"
        raw.mkdir(parents=True)
        raw_file = raw / "synthetic-1.json.gz"
        session.add_all(
            [
                Activity(
                    user_id=first.id,
                    garmin_activity_id="synthetic-1",
                    name="Synthetic Run",
                    activity_type="running",
                    started_at=datetime(2026, 1, 1, 8),
                    raw_file=str(raw_file),
                ),
                Activity(
                    user_id=second.id,
                    garmin_activity_id="synthetic-2",
                    name="Private Other Run",
                    activity_type="running",
                    started_at=datetime(2026, 1, 2, 8),
                ),
            ]
        )
        session.commit()

        raw_file.write_bytes(b"synthetic raw payload")
        token = tmp_path / "tokens" / f"account-{account.id}"
        token.mkdir(parents=True)
        (token / "oauth-token.json").write_text("secret-token", encoding="utf-8")

        export_path = create_user_export(session, first)
        try:
            with ZipFile(export_path) as archive:
                names = set(archive.namelist())
                users = json.loads(archive.read("database/users.json"))
                activities = json.loads(archive.read("database/activities.json"))
                manifest = json.loads(archive.read("manifest.json"))

                assert users == [
                    {
                        "id": first.id,
                        "display_name": "Export Athlete",
                        "created_at": users[0]["created_at"],
                        "onboarding_notice_acknowledged_at": None,
                        "onboarding_completed_at": None,
                        "onboarding_completed_version": 0,
                    }
                ]
                assert [item["name"] for item in activities] == ["Synthetic Run"]
                assert activities[0]["raw_file"] == "raw/activities/2026/synthetic-1.json.gz"
                assert str(tmp_path) not in json.dumps(activities)
                assert "raw/activities/2026/synthetic-1.json.gz" in names
                assert all("token" not in name for name in names)
                assert manifest["schema_version"] == 1
                assert manifest["table_counts"]["users"] == 1
                assert len(manifest["table_counts"]) == 40
        finally:
            remove_export(export_path)


def test_export_inventory_covers_every_application_table(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Inventory")
        session.add(user)
        session.commit()

        rows = collect_user_rows(session, user.id)

    from app.database import Base

    assert set(rows) == set(Base.metadata.tables)


def test_account_deletion_removes_database_rows_files_and_tokens(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_storage(monkeypatch, tmp_path)
    with session_factory() as session:
        deleted_user = User(display_name="Delete Me")
        retained_user = User(display_name="Keep Me")
        session.add_all([deleted_user, retained_user])
        session.flush()
        account = GarminAccount(user_id=deleted_user.id, email="delete@example.invalid")
        session.add(account)
        session.add(
            Activity(
                user_id=deleted_user.id,
                garmin_activity_id="delete-1",
                name="Delete Run",
                activity_type="running",
                started_at=datetime(2026, 1, 1, 8),
            )
        )
        session.commit()
        deleted_id = deleted_user.id
        retained_id = retained_user.id

        raw = tmp_path / "data" / "raw" / "activities" / f"user-{deleted_id}"
        raw.mkdir(parents=True)
        (raw / "payload.json.gz").write_bytes(b"raw")
        tokens = tmp_path / "tokens" / f"account-{account.id}"
        tokens.mkdir(parents=True)
        (tokens / "token").write_text("secret", encoding="utf-8")

        result = delete_user_account(session, deleted_user)

        assert result.user_id == deleted_id
        assert session.get(User, deleted_id) is None
        assert session.get(User, retained_id) is not None
        assert not raw.exists()
        assert not tokens.exists()


def test_account_routes_export_and_require_exact_delete_confirmation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_storage(monkeypatch, tmp_path)

    exported = client.get("/account/export")
    rejected = client.post(
        "/account/delete",
        data={"confirmation": "delete"},
        follow_redirects=False,
    )

    assert exported.status_code == 200
    assert exported.content.startswith(b"PK")
    assert exported.headers["content-type"] == "application/zip"
    assert exported.headers["cache-control"] == "private, no-store, max-age=0"
    assert rejected.status_code == 303
    assert rejected.headers["location"].startswith("/settings?error=")


def test_complete_export_endpoint_is_rate_limited(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_storage(monkeypatch, tmp_path)
    monkeypatch.setattr(get_settings(), "account_export_rate_limit_per_minute", 1)
    limiter.clear()

    first = client.get("/account/export")
    second = client.get("/account/export")

    assert first.status_code == 200
    assert second.status_code == 429


def test_startup_repair_restores_or_removes_staged_account_directories(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_storage(monkeypatch, tmp_path)
    with session_factory() as session:
        user = User(display_name="Restore Me")
        session.add(user)
        session.commit()
        original = tmp_path / "data" / "raw" / "activities" / f"user-{user.id}"
        original.mkdir(parents=True)
        staged = original.with_name(f".{original.name}.deleting-{'a' * 32}")
        original.replace(staged)
        orphan = original.parent / f".user-999.deleting-{'b' * 32}"
        orphan.mkdir()

        repair_account_lifecycle(session)

        assert original.exists()
        assert not staged.exists()
        assert not orphan.exists()
