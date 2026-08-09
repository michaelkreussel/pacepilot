from pathlib import Path

from alembic import command
from alembic.config import Config

from app.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def upgrade_database(database_url: str | None = None) -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(PROJECT_ROOT))
    config.attributes["database_url"] = database_url or get_settings().database_url
    command.upgrade(config, "head")
