"""Application logging: rotating file under %LOCALAPPDATA%\\RunnerDeck\\logs, always redacted."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from runnerdeck.core import paths
from runnerdeck.core.redact import RedactingFormatter

_FORMAT = "%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s"
_HANDLER_TAG = "_runnerdeck_handler"


def setup_logging(
    log_dir: Path | None = None, *, level: int = logging.INFO, stream: bool = False
) -> Path:
    """Configure the `runnerdeck` logger. Idempotent; returns the log file path."""
    log_dir = log_dir or paths.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / paths.LOG_FILE_NAME

    logger = logging.getLogger("runnerdeck")
    for handler in [h for h in logger.handlers if getattr(h, _HANDLER_TAG, False)]:
        logger.removeHandler(handler)
        handler.close()

    formatter = RedactingFormatter(_FORMAT)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    ]
    if stream:
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_TAG, True)
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return log_file
