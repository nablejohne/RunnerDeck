"""Application logging: rotating file under %LOCALAPPDATA%\\RunnerDeck\\logs, always redacted."""

from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from runnerdeck.core import paths
from runnerdeck.core.redact import RedactingFormatter

_FORMAT = "%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s"
_HANDLER_TAG = "_runnerdeck_handler"
ROLLOVER_RETRY_S = 60.0  # after a failed rollover, keep appending this long before retrying


class _TolerantRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that keeps appending when the file cannot be rotated right now.

    On Windows the rename in doRollover fails (WinError 32) while another process has the file
    open, e.g. a long-running `runnerdeck-cli start` next to another CLI command. The stock
    handler then prints a traceback to stderr for every record and drops it.
    """

    _retry_at = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if time.monotonic() < self._retry_at:
            return False
        return bool(super().shouldRollover(record))

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError:
            self._retry_at = time.monotonic() + ROLLOVER_RETRY_S
            if self.stream is None:  # closed for the rename: go on in the current file
                self.stream = self._open()


def setup_logging(
    log_dir: Path | None = None,
    *,
    level: int = logging.INFO,
    stream: bool = False,
    file_name: str = paths.LOG_FILE_NAME,
) -> Path:
    """Configure the `runnerdeck` logger. Idempotent; returns the log file path.

    file_name: separate programs (tray app, CLI) use separate files so that none of them
    rotates a file another one holds open.
    """
    log_dir = log_dir or paths.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / file_name

    logger = logging.getLogger("runnerdeck")
    for handler in [h for h in logger.handlers if getattr(h, _HANDLER_TAG, False)]:
        logger.removeHandler(handler)
        handler.close()

    formatter = RedactingFormatter(_FORMAT)
    handlers: list[logging.Handler] = [
        _TolerantRotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
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
