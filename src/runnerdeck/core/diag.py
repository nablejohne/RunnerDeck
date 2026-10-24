"""List and read a runner's `_diag` logs (Runner_*.log / Worker_*.log) for the detail page.

The runner writes these itself; while it runs the newest file is still being appended to, so
reading only takes what is there at that moment. Texts are redacted like everything else shown
to the user (a job's log may echo a token). Whether Windows lets us read a file the running
Listener holds open is an open spike item (docs/m3-design.md §9); a failure becomes a
RunnerDeckError with a readable message instead of a crash.
"""

from __future__ import annotations

import codecs
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from runnerdeck.core.errors import RunnerDeckError
from runnerdeck.core.models import DiagLog
from runnerdeck.core.redact import redact

log = logging.getLogger(__name__)

DIAG_DIR = "_diag"
MAX_TAIL_BYTES = 1_048_576
LOG_SUFFIX = ".log"

_KINDS: tuple[tuple[str, Literal["runner", "worker"]], ...] = (
    ("runner_", "runner"),
    ("worker_", "worker"),
)
_MB = 1024 * 1024


def _reason(e: OSError) -> str:
    return e.strerror or str(e) or type(e).__name__


def _kind(name: str) -> Literal["runner", "worker", "other"]:
    folded = name.casefold()
    for prefix, kind in _KINDS:
        if folded.startswith(prefix):
            return kind
    return "other"


def list_diag_logs(runner_dir: Path) -> list[DiagLog]:
    """`runner_dir/_diag/*.log`, newest first (then by name). Missing `_diag` -> [].

    Files that vanish or cannot be stat'ed while listing are skipped; an unreadable `_diag`
    directory raises RunnerDeckError.
    """
    diag_dir = Path(runner_dir) / DIAG_DIR
    try:
        with os.scandir(diag_dir) as it:
            entries = list(it)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as e:
        raise RunnerDeckError(f"无法读取诊断日志目录 {diag_dir}：{_reason(e)}") from e

    logs: list[DiagLog] = []
    for entry in entries:
        if not entry.name.casefold().endswith(LOG_SUFFIX):
            continue
        try:
            if not entry.is_file():
                continue
            st = entry.stat()
        except OSError as e:
            log.debug("skipping %s: %s", entry.path, e)
            continue
        logs.append(
            DiagLog(
                path=Path(entry.path),
                kind=_kind(entry.name),
                modified=datetime.fromtimestamp(st.st_mtime, UTC),
                size=st.st_size,
            )
        )
    logs.sort(key=lambda d: d.name.casefold())
    logs.sort(key=lambda d: d.modified, reverse=True)
    return logs


def _size_text(n: int) -> str:
    if n >= _MB:
        return f"{n / _MB:.4g} MB"
    if n >= 1024:
        return f"{n / 1024:.4g} KB"
    return f"{n} 字节"


def read_tail(path: Path, *, max_bytes: int = MAX_TAIL_BYTES) -> str:
    """The last `max_bytes` of `path` as redacted text with "\\n" line ends.

    When the file is longer, the partial first line is dropped and a note line starts the
    text. Decoded as UTF-8 (a leading BOM is removed, bad bytes become U+FFFD). A file still
    being written is read up to whatever its end is at that moment. Any OSError ->
    RunnerDeckError("无法读取 …：…").
    """
    if max_bytes < 1:
        raise ValueError("max_bytes must be >= 1")
    path = Path(path)
    try:
        with path.open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read(max_bytes)
    except OSError as e:
        raise RunnerDeckError(f"无法读取 {path}：{_reason(e)}") from e

    truncated = start > 0
    if truncated:
        newline = data.find(b"\n")
        if newline >= 0:
            data = data[newline + 1 :]
    elif data.startswith(codecs.BOM_UTF8):
        data = data[len(codecs.BOM_UTF8) :]
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if truncated:
        text = f"…（只显示最后 {_size_text(max_bytes)}）\n{text}"
    return redact(text)
