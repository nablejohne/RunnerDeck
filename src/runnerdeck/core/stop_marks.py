"""Cross-process record of each runner's most recent stop (time + whether it was a hard kill).

status.merge_status needs to know whether a runner that GitHub still reports online was just
hard-killed (session lingers ~3-3.5 min, spike §5) or stopped gracefully (GitHub lags 11-70 s,
spike §10). RunnerProcess only knows that for its own runs and only in memory, so a stop done by
the CLI in another terminal, or a GUI restart, lost it (the M1 gap in docs/m1-design.md §13).
The GUI and the CLI both write here through RunnerManager.record_stop.

One small JSON file per runner directory (`<sha256(path key)[:16]>.json`, content
`{"path", "stopped_at", "forced"}`), so two processes stopping different runners never
read-modify-write the same file. A stop record is only a hint: every method logs OSError as a
warning and never raises.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from runnerdeck.core import paths
from runnerdeck.core.status import SESSION_LINGER_S
from runnerdeck.core.store import _replace_with_retry

log = logging.getLogger(__name__)

STOPS_DIR_NAME = "stops"  # under paths.local_dir()
_KEY_CHARS = 16
_MAX_AGE = timedelta(seconds=SESSION_LINGER_S)


def path_key(path: str | os.PathLike[str]) -> str:
    """Normalized form of a runner directory (absolute, case-folded on Windows).

    The single definition shared with manager._path_key, so records and stop marks agree.
    """
    return os.path.normcase(os.path.abspath(os.fspath(path)))


@dataclass(frozen=True)
class StopMark:
    """When the runner last stopped (UTC) and whether that stop was a hard kill."""

    stopped_at: datetime
    forced: bool


def _utc(value: datetime) -> datetime:
    """Aware UTC datetime; a naive one is taken to be UTC already."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class StopMarks:
    """Stop records under `directory` (default `paths.local_dir() / "stops"`).

    Thread-safe and process-safe in the sense that matters here: every write is an atomic
    replace of one runner's file. Methods never raise for IO problems.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory if directory is not None else paths.local_dir() / STOPS_DIR_NAME

    @property
    def directory(self) -> Path:
        return self._directory

    def path_for(self, runner_dir: str | os.PathLike[str]) -> Path:
        """The file holding `runner_dir`'s stop record (may not exist)."""
        digest = hashlib.sha256(path_key(runner_dir).encode("utf-8")).hexdigest()
        return self._directory / f"{digest[:_KEY_CHARS]}.json"

    def record(
        self, runner_dir: str | os.PathLike[str], *, forced: bool, at: datetime | None = None
    ) -> None:
        """Remember that `runner_dir` stopped at `at` (default now); forced = hard kill."""
        when = _utc(at) if at is not None else datetime.now(UTC)
        payload = {
            "path": os.path.abspath(os.fspath(runner_dir)),
            "stopped_at": when.isoformat(),
            "forced": bool(forced),
        }
        target = self.path_for(runner_dir)
        tmp: Path | None = None
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=self._directory, prefix=f".{target.stem}.", suffix=".tmp"
            )
            tmp = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            _replace_with_retry(tmp, target)
            tmp = None
            log.debug("stop of %s recorded (forced=%s)", runner_dir, forced)
        except OSError as e:
            log.warning("cannot record the stop of %s in %s: %s", runner_dir, target, e)
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    tmp.unlink()

    def get(self, runner_dir: str | os.PathLike[str], *, now: datetime) -> StopMark | None:
        """The stop record of `runner_dir`, or None.

        None when the file is missing, unreadable, corrupt or belongs to another path. A record
        older than status.SESSION_LINGER_S says nothing any more (both the forced-kill session
        and GitHub's offline lag are over): None. So is one dated more than that into the
        future (the clock was set back). Expired files are left alone, not deleted: another
        process (the CLI) may have just replaced the file with a fresh record, and the next
        record() overwrites it anyway (one small file per runner).
        """
        target = self.path_for(runner_dir)
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            log.warning("cannot read the stop record %s: %s", target, e)
            return None
        mark = self._parse(raw, runner_dir, target)
        if mark is None:
            return None
        age = _utc(now) - mark.stopped_at
        if age >= _MAX_AGE or -age > _MAX_AGE:
            return None
        return mark

    def clear(self, runner_dir: str | os.PathLike[str]) -> None:
        """Forget `runner_dir`'s stop record (e.g. after the runner was removed)."""
        self._delete(self.path_for(runner_dir))

    @staticmethod
    def _parse(raw: str, runner_dir: str | os.PathLike[str], target: Path) -> StopMark | None:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            path, stopped_at, forced = data["path"], data["stopped_at"], data["forced"]
            if not isinstance(path, str) or not isinstance(forced, bool):
                raise ValueError("wrong field types")
            when = datetime.fromisoformat(stopped_at)
        except (ValueError, KeyError, TypeError) as e:
            log.warning("ignoring corrupt stop record %s: %s", target, e)
            return None
        if when.tzinfo is None:
            log.warning("ignoring stop record %s without a time zone", target)
            return None
        if path_key(path) != path_key(runner_dir):
            log.debug("stop record %s belongs to %s, not %s", target, path, runner_dir)
            return None
        return StopMark(stopped_at=when.astimezone(UTC), forced=forced)

    @staticmethod
    def _delete(target: Path) -> None:
        try:
            target.unlink(missing_ok=True)
        except OSError as e:
            log.warning("cannot delete the stop record %s: %s", target, e)
