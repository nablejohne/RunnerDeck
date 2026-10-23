"""Tests for runnerdeck.app.workers: results, failures and progress cross to the main thread."""

from __future__ import annotations

import logging
import threading
from typing import Any

import pytest
from PyQt5.QtCore import QCoreApplication, QObject, QThread, QThreadPool, pyqtSignal

from runnerdeck.app import workers
from runnerdeck.app.workers import INTERNAL_ERROR_SUFFIX, Task, TaskSignals, submit
from runnerdeck.core.errors import InstallError, RunnerDeckError
from runnerdeck.core.redact import register_secret

WAIT_MS = 5000


def _on_main_thread() -> bool:
    app = QCoreApplication.instance()
    assert app is not None
    return QThread.currentThread() is app.thread()


class Collector:
    """Records every delivery with the thread it arrived on."""

    def __init__(self) -> None:
        self.finished: list[tuple[Any, bool]] = []
        self.failed: list[tuple[str, BaseException, bool]] = []
        self.progress: list[tuple[str, str, bool]] = []

    def on_finished(self, result: Any) -> None:
        self.finished.append((result, _on_main_thread()))

    def on_failed(self, message: str, exc: BaseException) -> None:
        self.failed.append((message, exc, _on_main_thread()))

    def on_progress(self, step: str, text: str) -> None:
        self.progress.append((step, text, _on_main_thread()))

    @property
    def done(self) -> bool:
        return bool(self.finished or self.failed)


def _run(
    pool: QThreadPool, qtbot: Any, fn: Any, *args: Any, **kwargs: Any
) -> tuple[Task, Collector]:
    seen = Collector()
    task = submit(
        pool,
        fn,
        *args,
        on_finished=seen.on_finished,
        on_failed=seen.on_failed,
        on_progress=seen.on_progress,
        **kwargs,
    )
    qtbot.waitUntil(lambda: seen.done, timeout=WAIT_MS)
    return task, seen


def test_finished_carries_the_return_value_on_the_main_thread(
    pool: QThreadPool, qtbot: Any
) -> None:
    worker_threads: list[threading.Thread] = []

    def work(a: int, *, b: int) -> dict[str, int]:
        worker_threads.append(threading.current_thread())
        return {"sum": a + b}

    task, seen = _run(pool, qtbot, work, 1, b=2)
    assert seen.finished == [({"sum": 3}, True)]
    assert seen.failed == []
    assert worker_threads and worker_threads[0] is not threading.main_thread()
    assert task.name.endswith("work")


def test_runnerdeck_error_becomes_failed_with_its_message(pool: QThreadPool, qtbot: Any) -> None:
    error = InstallError("config.cmd 失败", output="line 1\nline 2", returncode=1)

    def work() -> None:
        raise error

    _, seen = _run(pool, qtbot, work)
    assert len(seen.failed) == 1
    message, exc, on_main = seen.failed[0]
    assert message == "config.cmd 失败"
    assert exc is error
    assert exc.output == "line 1\nline 2"  # type: ignore[attr-defined]
    assert on_main
    assert seen.finished == []


def test_other_exceptions_are_internal_errors_and_logged(
    pool: QThreadPool, qtbot: Any, caplog: pytest.LogCaptureFixture
) -> None:
    def work() -> None:
        raise ValueError("boom")

    with caplog.at_level(logging.ERROR, logger="runnerdeck.app.workers"):
        _, seen = _run(pool, qtbot, work)
    message, exc, _ = seen.failed[0]
    assert message == "内部错误：ValueError: boom" + INTERNAL_ERROR_SUFFIX
    assert isinstance(exc, ValueError)
    crash = [r for r in caplog.records if "crashed" in r.getMessage()]
    assert crash and crash[0].exc_info is not None  # log.exception: traceback attached


def test_exception_without_text_still_names_its_type(pool: QThreadPool, qtbot: Any) -> None:
    def work() -> None:
        raise KeyError

    _, seen = _run(pool, qtbot, work)
    assert seen.failed[0][0] == "内部错误：KeyError" + INTERNAL_ERROR_SUFFIX


def test_base_exceptions_do_not_escape_the_pool(pool: QThreadPool, qtbot: Any) -> None:
    class Abort(BaseException):
        pass

    def work() -> None:
        raise Abort("stop everything")

    _, seen = _run(pool, qtbot, work)
    assert seen.failed[0][0].startswith("内部错误：Abort: stop everything")
    assert isinstance(seen.failed[0][1], Abort)


def test_failed_messages_are_redacted(pool: QThreadPool, qtbot: Any) -> None:
    secret = "github_pat_" + "REDACTME" * 5
    register_secret(secret)

    def expected() -> None:
        raise RunnerDeckError(f"GitHub 拒绝了 {secret}")

    def unexpected() -> None:
        raise RuntimeError(f"token {secret} leaked")

    _, seen = _run(pool, qtbot, expected)
    assert secret not in seen.failed[0][0]
    assert "***" in seen.failed[0][0]
    _, seen = _run(pool, qtbot, unexpected)
    assert secret not in seen.failed[0][0]


def test_progress_callback_emits_from_the_worker_to_the_main_thread(
    pool: QThreadPool, qtbot: Any
) -> None:
    calls: list[threading.Thread] = []

    def work(n: int, *, progress: Any) -> str:
        for i in range(n):
            calls.append(threading.current_thread())
            progress(f"step{i}", f"第 {i} 步")
        return "ok"

    _, seen = _run(pool, qtbot, work, 3, progress=True)
    qtbot.waitUntil(lambda: len(seen.progress) == 3, timeout=WAIT_MS)
    assert seen.progress == [(f"step{i}", f"第 {i} 步", True) for i in range(3)]
    assert all(t is not threading.main_thread() for t in calls)
    assert seen.finished == [("ok", True)]


def test_progress_false_does_not_pass_the_keyword(pool: QThreadPool, qtbot: Any) -> None:
    def work(**kwargs: Any) -> list[str]:
        return sorted(kwargs)

    _, seen = _run(pool, qtbot, work, x=1)
    assert seen.finished == [(["x"], True)]


def test_progress_accepts_str_enums(pool: QThreadPool, qtbot: Any) -> None:
    from runnerdeck.core.manager import Step

    def work(*, progress: Any) -> None:
        progress(Step.DOWNLOAD, "下载")

    _, seen = _run(pool, qtbot, work, progress=True)
    qtbot.waitUntil(lambda: bool(seen.progress), timeout=WAIT_MS)
    assert seen.progress[0][:2] == ("download", "下载")
    assert type(seen.progress[0][0]) is str


def test_submit_without_slots_and_task_release(pool: QThreadPool, qtbot: Any) -> None:
    flag = threading.Event()
    task = submit(pool, flag.set)
    assert task in workers._LIVE_TASKS
    assert isinstance(task.signals, TaskSignals)
    assert flag.wait(5)
    qtbot.waitUntil(lambda: task not in workers._LIVE_TASKS, timeout=WAIT_MS)


def test_failed_task_is_released_too(pool: QThreadPool, qtbot: Any) -> None:
    def work() -> None:
        raise RunnerDeckError("x")

    task, seen = _run(pool, qtbot, work)
    qtbot.waitUntil(lambda: task not in workers._LIVE_TASKS, timeout=WAIT_MS)
    assert seen.failed


def test_on_progress_may_be_a_signal(pool: QThreadPool, qtbot: Any) -> None:
    class Relay(QObject):
        progress = pyqtSignal(str, str)

    relay = Relay()
    seen: list[tuple[str, str]] = []
    relay.progress.connect(lambda s, t: seen.append((s, t)))

    def work(*, progress: Any) -> None:
        progress("a", "b")

    submit(pool, work, progress=True, on_progress=relay.progress)
    qtbot.waitUntil(lambda: seen == [("a", "b")], timeout=WAIT_MS)


def test_task_run_directly_is_safe(qtbot: Any) -> None:
    """Task.run() on the current thread: still converts exceptions, still emits."""
    seen: list[str] = []
    task = Task(lambda: 1 / 0)
    task.signals.failed.connect(lambda m, e: seen.append(m))
    task.run()
    assert seen and seen[0].startswith("内部错误：ZeroDivisionError")
