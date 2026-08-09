import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock

from app.config import get_settings

_handler_lock = Lock()


def ensure_coach_file_logging(logger: logging.Logger) -> Path:
    """Attach one rotating Coach log file to the process logger."""
    log_path = get_settings().data_dir / "logs" / "coach.log"
    resolved_path = log_path.resolve()
    with _handler_lock:
        for handler in logger.handlers:
            if (
                isinstance(handler, RotatingFileHandler)
                and Path(handler.baseFilename).resolve() == resolved_path
            ):
                return log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
            delay=True,
        )
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    logger.info("Coach file logging enabled path=%s", log_path)
    return log_path
