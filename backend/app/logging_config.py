from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


APP_LOGGER_NAME = "industrial_agent.app"
SCHEDULER_LOGGER_NAME = "industrial_agent.scheduler"
DEFAULT_LOG_DIR = Path(__file__).resolve().parents[1] / "log"
_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5


for _logger_name in (APP_LOGGER_NAME, SCHEDULER_LOGGER_NAME):
    _logger = logging.getLogger(_logger_name)
    if not any(isinstance(handler, logging.NullHandler) for handler in _logger.handlers):
        _logger.addHandler(logging.NullHandler())
    _logger.propagate = False


def configure_logging(log_dir: Path | str | None = None) -> None:
    target_dir = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    _configure_file_logger(logging.getLogger(APP_LOGGER_NAME), target_dir / "app.log")
    _configure_file_logger(logging.getLogger(SCHEDULER_LOGGER_NAME), target_dir / "scheduler.log")


def close_configured_logging() -> None:
    for logger_name in (APP_LOGGER_NAME, SCHEDULER_LOGGER_NAME):
        logger = logging.getLogger(logger_name)
        for handler in list(logger.handlers):
            if getattr(handler, "_industrial_agent_file_handler", False):
                logger.removeHandler(handler)
                handler.close()


def _configure_file_logger(logger: logging.Logger, log_path: Path) -> None:
    logger.setLevel(logging.INFO)
    logger.propagate = False
    resolved = str(log_path.resolve())
    for handler in logger.handlers:
        if getattr(handler, "baseFilename", None) == resolved:
            return
    for handler in list(logger.handlers):
        if getattr(handler, "_industrial_agent_file_handler", False):
            logger.removeHandler(handler)
            handler.close()

    handler = RotatingFileHandler(
        resolved,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler._industrial_agent_file_handler = True  # type: ignore[attr-defined]
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(handler)
