from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_initial_migration_matches_models(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = f"sqlite:///{database_path.as_posix()}"

    command.upgrade(config, "head")
    command.check(config)

    inspector = inspect(create_engine(f"sqlite:///{database_path.as_posix()}"))
    assert {
        "activities",
        "activity_exercise_sets",
        "activity_splits",
        "activity_zones",
        "alembic_version",
        "daily_data_statuses",
        "daily_fitness",
        "daily_health",
        "garmin_accounts",
        "garmin_devices",
        "garmin_sync_states",
        "sleep_stages",
        "sync_runs",
        "users",
        "workout_steps",
        "workouts",
    } == set(inspector.get_table_names())


def test_migration_adopts_legacy_create_all_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20260805_01")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, display_name, created_at) "
                "VALUES (1, 'Legacy', '2026-08-05 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO activities "
                "(id, user_id, garmin_activity_id, name, activity_type, started_at, synced_at) "
                "VALUES (1, 1, '123', 'Legacy Run', 'running', "
                "'2026-08-05 07:00:00', '2026-08-05 08:00:00')"
            )
        )
        connection.execute(
            text("INSERT INTO daily_health (id, user_id, day) VALUES (1, 1, '2026-08-05')")
        )
        connection.execute(text("DELETE FROM alembic_version"))

    command.upgrade(config, "head")

    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        activity_count = connection.execute(text("SELECT count(*) FROM activities")).scalar()
        health_updated_at = connection.execute(
            text("SELECT updated_at FROM daily_health WHERE id = 1")
        ).scalar()
    assert revision == "20260808_02"
    assert activity_count == 1
    assert health_updated_at is not None
