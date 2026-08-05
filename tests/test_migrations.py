from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.database import Base


def test_initial_migration_matches_models(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = f"sqlite:///{database_path.as_posix()}"

    command.upgrade(config, "head")
    command.check(config)

    inspector = inspect(create_engine(f"sqlite:///{database_path.as_posix()}"))
    assert {
        "activities",
        "alembic_version",
        "daily_health",
        "garmin_accounts",
        "garmin_devices",
        "sync_runs",
        "users",
        "workout_steps",
        "workouts",
    } == set(inspector.get_table_names())


def test_migration_adopts_legacy_create_all_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url

    command.upgrade(config, "head")

    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert revision == "20260805_01"
