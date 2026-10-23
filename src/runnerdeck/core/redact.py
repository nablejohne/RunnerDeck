"""Secret redaction for logs, exception messages and captured command output.

Every module that sees a secret (PAT, registration/remove token) calls register_secret()
first; redact() then masks it anywhere. Token-shaped strings are masked even if never
registered.
"""

from __future__ import annotations

import logging
import re
import threading

MASK = "***"
_MIN_SECRET_LEN = 8  # don't turn short common words into *** everywhere

_TOKEN_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
)
# Keep the key, mask the value: `--token X`, `Authorization: Bearer X`, `token=X`.
_KEYED_PATTERNS = (
    re.compile(r"(--token\s+)\S+", re.IGNORECASE),
    re.compile(r"(Authorization:\s*(?:Bearer|token)\s+)\S+", re.IGNORECASE),
    re.compile(r"""(["']?token["']?\s*[:=]\s*["']?)[^\s"',}]+""", re.IGNORECASE),
)


class Redactor:
    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def add(self, secret: str | None) -> None:
        if secret and len(secret) >= _MIN_SECRET_LEN:
            with self._lock:
                self._secrets.add(secret)

    def __call__(self, text: str) -> str:
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            text = text.replace(secret, MASK)
        for pattern in _TOKEN_PATTERNS:
            text = pattern.sub(MASK, text)
        for pattern in _KEYED_PATTERNS:
            text = pattern.sub(lambda m: m.group(1) + MASK, text)
        return text


_global = Redactor()


def register_secret(secret: str | None) -> None:
    _global.add(secret)


def redact(text: str) -> str:
    return _global(text)


class RedactingFormatter(logging.Formatter):
    """Redacts the fully formatted record, including exception tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
