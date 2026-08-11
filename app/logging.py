import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
CONSOLE_HANDLER = "pacepilot.console"
FILE_HANDLER = "pacepilot.file"


def configure_logging(log_path: Path, *, level: int = logging.INFO) -> None:
    resolved_path = log_path.resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in tuple(root_logger.handlers):
        if handler.get_name() in {CONSOLE_HANDLER, FILE_HANDLER}:
            root_logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT)
    console_handler = logging.StreamHandler()
    console_handler.set_name(CONSOLE_HANDLER)
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        resolved_path,
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.set_name(FILE_HANDLER)
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Uvicorn installs dedicated handlers before importing the application. Route those
    # records through the same global sinks to avoid duplicate or missing output.
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.disabled = False
        uvicorn_logger.setLevel(logging.NOTSET)
        uvicorn_logger.propagate = True

    for logger_name, configured_logger in logging.root.manager.loggerDict.items():
        if logger_name.startswith("app.") and isinstance(configured_logger, logging.Logger):
            configured_logger.disabled = False
            configured_logger.setLevel(logging.NOTSET)
            configured_logger.propagate = True

    logging.getLogger(__name__).info("Logging initialized file=%s", resolved_path)
