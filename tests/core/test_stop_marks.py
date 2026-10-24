"""Tests for runnerdeck.core.stop_marks (per-runner stop records shared by the GUI and the CLI)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from runnerdeck.core import stop_marks as stop_marks_mod
from runnerdeck.core.status import SESSION_LINGER_S
from runnerdeck.core.stop_marks import StopMark, StopMarks, path_key

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _propagate_logs() -> Iterator[None]:
    logger = logging.getLogger("runnerdeck")
    previous = logger.propagate
    logger.propagate = True
    yield
    logger.propagate = previous


@pytest.fixture
def marks(tmp_path: Path) -> StopMarks:
    return StopMarks(tmp_path / "stops")


@pytest.fixture
def runner_dir(tmp_path: Path) -> Path:
    return tmp_path / "runners" / "demo"


def test_default_directory_is_under_the_local_app_dir(isolated_home: Path) -> None:
    assert StopMarks().directory == isolated_home / "stops"


def test_record_then_get(marks: StopMarks, runner_dir: Path) -> None:
    marks.record(runner_dir, forced=True, at=NOW - timedelta(seconds=30))
    assert marks.get(runner_dir, now=NOW) == StopMark(NOW - timedelta(seconds=30), True)
    marks.record(runner_dir, forced=False, at=NOW)  # overwrites the previous one
    assert marks.get(runner_dir, now=NOW) == StopMark(NOW, False)


def test_file_name_and_content(marks: StopMarks, runner_dir: Path) -> None:
    marks.record(runner_dir, forced=True, at=NOW)
    digest = hashlib.sha256(path_key(runner_dir).encode("utf-8")).hexdigest()[:16]
    target = marks.directory / f"{digest}.json"
    assert marks.path_for(runner_dir) == target
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "path": os.path.abspath(runner_dir),
        "stopped_at": NOW.isoformat(),
        "forced": True,
    }
    assert [p.name for p in marks.directory.iterdir()] == [target.name]  # no temp file left


def test_one_file_per_runner(marks: StopMarks, tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    marks.record(a, forced=True, at=NOW)
    marks.record(b, forced=False, at=NOW)
    assert marks.path_for(a) != marks.path_for(b)
    assert marks.get(a, now=NOW) == StopMark(NOW, True)
    assert marks.get(b, now=NOW) == StopMark(NOW, False)


def test_equivalent_spellings_share_a_record(marks: StopMarks, runner_dir: Path) -> None:
    marks.record(runner_dir, forced=True, at=NOW)
    spelled = runner_dir / ".." / runner_dir.name
    assert marks.get(spelled, now=NOW) == StopMark(NOW, True)
    assert marks.get(str(runner_dir) + os.sep, now=NOW) == StopMark(NOW, True)


def test_default_time_is_now(marks: StopMarks, runner_dir: Path) -> None:
    before = datetime.now(UTC)
    marks.record(runner_dir, forced=False)
    mark = marks.get(runner_dir, now=datetime.now(UTC))
    assert mark is not None and mark.stopped_at >= before and mark.stopped_at.tzinfo is not None


def test_times_are_stored_in_utc(marks: StopMarks, runner_dir: Path) -> None:
    marks.record(runner_dir, forced=True, at=NOW.replace(tzinfo=None))  # naive: taken as UTC
    mark = marks.get(runner_dir, now=NOW)
    assert mark is not None and mark.stopped_at == NOW and mark.stopped_at.tzinfo == UTC

    marks.record(runner_dir, forced=True, at=NOW.astimezone(timezone(timedelta(hours=8))))
    assert json.loads(marks.path_for(runner_dir).read_text(encoding="utf-8"))["stopped_at"] == (
        NOW.isoformat()
    )
    mark = marks.get(runner_dir, now=NOW.astimezone(timezone(timedelta(hours=-5))))
    assert (
        mark is not None and mark.stopped_at == NOW and mark.stopped_at.utcoffset() == timedelta()
    )


def test_missing_record(marks: StopMarks, runner_dir: Path) -> None:
    assert marks.get(runner_dir, now=NOW) is None  # directory not even created
    assert not marks.directory.exists()


def test_expired_record_is_none_but_not_deleted(marks: StopMarks, runner_dir: Path) -> None:
    marks.record(runner_dir, forced=True, at=NOW - timedelta(seconds=SESSION_LINGER_S - 1))
    assert marks.get(runner_dir, now=NOW) is not None
    marks.record(runner_dir, forced=True, at=NOW - timedelta(seconds=SESSION_LINGER_S))
    assert marks.get(runner_dir, now=NOW) is None
    # Left alone: deleting after the read could remove a record the CLI just wrote.
    assert marks.path_for(runner_dir).exists()
    marks.record(runner_dir, forced=False, at=NOW)  # the next stop simply overwrites it
    assert marks.get(runner_dir, now=NOW) == StopMark(NOW, False)


def test_record_far_in_the_future_is_stale(marks: StopMarks, runner_dir: Path) -> None:
    marks.record(runner_dir, forced=True, at=NOW + timedelta(seconds=5))  # small skew: kept
    assert marks.get(runner_dir, now=NOW) == StopMark(NOW + timedelta(seconds=5), True)
    marks.record(runner_dir, forced=True, at=NOW + timedelta(seconds=SESSION_LINGER_S + 1))
    assert marks.get(runner_dir, now=NOW) is None


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        "{}",
        '{"path": "X", "stopped_at": "yesterday", "forced": true}',
        '{"path": 1, "stopped_at": "2026-10-24T09:00:00+00:00", "forced": true}',
        '{"path": "X", "stopped_at": "2026-10-24T09:00:00+00:00", "forced": "yes"}',
        '{"path": "X", "stopped_at": 5, "forced": true}',
        '{"path": "X", "stopped_at": "2026-10-24T09:00:00", "forced": true}',  # naive
    ],
)
def test_corrupt_record_is_none(
    marks: StopMarks, runner_dir: Path, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    target = marks.path_for(runner_dir)
    target.parent.mkdir(parents=True)
    target.write_text(content.replace('"X"', json.dumps(str(runner_dir))), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="runnerdeck.core.stop_marks"):
        assert marks.get(runner_dir, now=NOW) is None
    assert "stop record" in caplog.text


def test_record_of_another_path_is_ignored_and_kept(marks: StopMarks, runner_dir: Path) -> None:
    other = runner_dir.parent / "other"
    marks.record(other, forced=True, at=NOW)
    # simulate a hash collision: other's content under runner_dir's file name
    marks.directory.mkdir(parents=True, exist_ok=True)
    marks.path_for(other).replace(marks.path_for(runner_dir))
    assert marks.get(runner_dir, now=NOW) is None
    assert marks.path_for(runner_dir).exists()


def test_clear(marks: StopMarks, runner_dir: Path) -> None:
    marks.clear(runner_dir)  # nothing there: silent
    marks.record(runner_dir, forced=True, at=NOW)
    marks.clear(runner_dir)
    assert marks.get(runner_dir, now=NOW) is None
    assert not marks.path_for(runner_dir).exists()


def test_record_never_raises(
    tmp_path: Path, runner_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    marks = StopMarks(blocker / "stops")  # parent is a file: mkdir fails
    with caplog.at_level(logging.WARNING, logger="runnerdeck.core.stop_marks"):
        marks.record(runner_dir, forced=True, at=NOW)
        assert marks.get(runner_dir, now=NOW) is None
        marks.clear(runner_dir)
    assert "cannot record the stop" in caplog.text


def test_failed_replace_removes_the_temp_file(
    marks: StopMarks, runner_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(src: Path, dst: Path) -> None:
        raise PermissionError(13, "busy")

    monkeypatch.setattr(stop_marks_mod, "_replace_with_retry", deny)
    marks.record(runner_dir, forced=True, at=NOW)
    assert list(marks.directory.iterdir()) == []


def test_unreadable_record_is_none(
    marks: StopMarks, runner_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    marks.path_for(runner_dir).mkdir(parents=True)  # a directory where the file should be
    with caplog.at_level(logging.WARNING, logger="runnerdeck.core.stop_marks"):
        assert marks.get(runner_dir, now=NOW) is None
        marks.clear(runner_dir)  # unlink fails: only a warning
    assert "cannot read the stop record" in caplog.text
    assert "cannot delete the stop record" in caplog.text
