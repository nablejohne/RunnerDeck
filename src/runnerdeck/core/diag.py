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
import re
from dataclasses import dataclass
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
            # os.stat, not entry.stat(): on Windows the scandir data is the directory entry's,
            # which for the log the running Listener writes still says 0 B and the creation
            # time (real XM spike, m3-design §10).
            st = os.stat(entry.path)
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


# --- service-mode runners: the Runner log carries the stdout (m4-design §2.7) ------------

# [2026-10-24 06:08:11Z INFO Terminal] WRITE LINE: 2026-10-24 06:08:11Z: Running job: spike
# [2026-10-24 06:04:42Z ERR  Terminal] WRITE ERROR: An error occurred: ...
_TERMINAL_RE = re.compile(r"^\[[^\]]*\sTerminal\] WRITE (LINE|ERROR): ?(.*)$")
_FOLLOW_INITIAL_BYTES = 64 * 1024


def terminal_line(raw: str) -> tuple[bool, str] | None:
    """(is_error, text) for a Runner log line the Listener wrote to its console, else None."""
    match = _TERMINAL_RE.match(raw.rstrip("\r\n"))
    if match is None:
        return None
    return match.group(1) == "ERROR", match.group(2)


def newest_runner_log(runner_dir: Path) -> Path | None:
    """The `_diag/Runner_*.log` modified last (each Listener start opens a new one)."""
    newest: tuple[int, Path] | None = None
    try:
        with os.scandir(Path(runner_dir) / DIAG_DIR) as it:
            for entry in it:
                if not (_kind(entry.name) == "runner" and entry.name.endswith(LOG_SUFFIX)):
                    continue
                try:
                    mtime = os.stat(entry.path).st_mtime_ns  # live value, spike-notes §11
                except OSError:
                    continue
                if newest is None or mtime > newest[0]:
                    newest = (mtime, Path(entry.path))
    except OSError:
        return None
    return None if newest is None else newest[1]


def last_terminal_error(runner_dir: Path, *, max_bytes: int = _FOLLOW_INITIAL_BYTES) -> str | None:
    """The last `WRITE ERROR:` text in the newest Runner log (why a service quit), redacted."""
    path = newest_runner_log(runner_dir)
    if path is None:
        return None
    try:
        text = read_tail(path, max_bytes=max_bytes)
    except RunnerDeckError:
        return None
    errors = [line[1] for raw in text.splitlines() if (line := terminal_line(raw)) and line[0]]
    return errors[-1] if errors else None


@dataclass(frozen=True)
class FollowResult:
    """lines: console lines appended since the previous poll. catch_up: they come from the
    first read of a file that was already being written (state recovery: do not notify)."""

    lines: tuple[str, ...] = ()
    catch_up: bool = False


class LogFollower:
    """Follows the newest Runner log of a service-mode runner (not thread-safe).

    The first poll starts at most 64 KB before the end of the current file; a new file (the
    service restarted) is read from its start. Only complete lines are consumed. last_error is
    the newest `WRITE ERROR:` text seen. Texts are redacted.
    """

    def __init__(self, runner_dir: Path, *, initial_bytes: int = _FOLLOW_INITIAL_BYTES) -> None:
        self.runner_dir = Path(runner_dir)
        self.last_error: str | None = None
        self._initial_bytes = initial_bytes
        self._path: Path | None = None
        self._offset = 0
        self._first = True

    def poll(self) -> FollowResult:
        path = newest_runner_log(self.runner_dir)
        if path is None:
            return FollowResult()
        catch_up = False
        drained: tuple[str, ...] = ()
        if path != self._path:
            if self._path is not None and not self._first:
                # The service restarted: finish the old file first (its last job result).
                drained = self._read_new(self._path, catch_up=False)
            catch_up = self._first
            self._path, self._offset = path, 0
        lines = self._read_new(path, catch_up=catch_up)
        self._first = False
        return FollowResult(drained + lines, catch_up)

    def _read_new(self, path: Path, *, catch_up: bool) -> tuple[str, ...]:
        """Complete new console lines of `path` from self._offset (moved past them)."""
        try:
            with path.open("rb") as f:
                size = f.seek(0, os.SEEK_END)
                if catch_up:
                    self._offset = max(0, size - self._initial_bytes)
                elif size < self._offset:  # truncated / replaced under the same name
                    self._offset = 0
                f.seek(self._offset)
                data = f.read(size - self._offset)
        except OSError as e:
            log.debug("cannot read %s: %s", path, e)
            return ()
        end = data.rfind(b"\n")
        if end < 0:
            return ()
        skip_partial = catch_up and self._offset > 0
        self._offset += end + 1
        chunk = data[: end + 1]
        if chunk.startswith(codecs.BOM_UTF8):
            chunk = chunk[len(codecs.BOM_UTF8) :]
        raws = chunk.decode("utf-8", errors="replace").splitlines()
        if skip_partial and raws:
            raws = raws[1:]  # started in the middle of a line
        lines: list[str] = []
        for raw in raws:
            parsed = terminal_line(raw)
            if parsed is None:
                continue
            is_error, text = parsed
            text = redact(text)
            if is_error:
                self.last_error = text
            else:
                lines.append(text)
        return tuple(lines)
