"""Run blocking core calls on a QThreadPool and report back through Qt signals.

docs/m2-design.md §3. A Task wraps one callable; its TaskSignals object is created on the
calling (main) thread, so slots connected to it run on the main thread through Qt's queued
connections while the callable itself runs on a pool thread. Task.run() turns every exception
into `failed`: a RunnerDeckError's message is shown as is (it is Chinese and already redacted),
anything else becomes "内部错误：..." and is logged with its traceback. Nothing ever escapes
into the thread pool.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PyQt5.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from runnerdeck.core.errors import RunnerDeckError
from runnerdeck.core.redact import redact

log = logging.getLogger(__name__)

INTERNAL_ERROR_SUFFIX = "，详情见日志"

# Tasks are kept alive until their result was delivered on the main thread (see submit()).
# Qt owns and deletes the C++ QRunnable (autoDelete) right after run(); the Python Task and its
# TaskSignals must outlive that until the queued finished/failed slots have run, otherwise the
# slot proxies die with the signals object and the result is silently dropped.
_LIVE_TASKS: set[Task] = set()


class TaskSignals(QObject):
    """Signals of one Task. Created on the submitting thread, emitted from the worker."""

    finished = pyqtSignal(object)  # the return value of fn
    failed = pyqtSignal(str, object)  # (user-readable message, exception object)
    progress = pyqtSignal(str, str)  # (step id, Chinese description), see manager.ProgressFn

    def __init__(self, task: Task) -> None:
        super().__init__()
        self._task: Task | None = task

    def _release(self, *_args: object) -> None:
        """Main thread, after finished/failed were delivered: let the task be collected.

        A bound method of this QObject, not of the Task: PyQt skips slots whose sip object's
        C++ side is gone, which is the Task's fate once the pool auto-deleted it.
        """
        task, self._task = self._task, None
        if task is not None:
            _LIVE_TASKS.discard(task)


def internal_error_message(exc: BaseException) -> str:
    """The text shown for an exception that is not a RunnerDeckError."""
    detail = redact(str(exc))
    head = f"内部错误：{type(exc).__name__}"
    return (f"{head}: {detail}" if detail else head) + INTERNAL_ERROR_SUFFIX


class Task(QRunnable):
    """One blocking call on a pool thread.

    progress=True: fn is called with an extra keyword `progress=<callable>` that emits
    `signals.progress` from the worker thread (manager.ProgressFn shape).
    """

    def __init__(
        self, fn: Callable[..., Any], *args: Any, progress: bool = False, **kwargs: Any
    ) -> None:
        super().__init__()
        self.signals = TaskSignals(self)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._with_progress = progress
        self.name = getattr(fn, "__qualname__", None) or repr(fn)

    def _report(self, step: str, text: str) -> None:
        self.signals.progress.emit(str(step), text)

    def run(self) -> None:  # pool thread
        try:
            kwargs = dict(self._kwargs)
            if self._with_progress:
                kwargs["progress"] = self._report
            result = self._fn(*self._args, **kwargs)
        except RunnerDeckError as e:
            log.info("task %s failed: %s", self.name, redact(str(e)))
            self.signals.failed.emit(redact(str(e)), e)
        except BaseException as e:  # noqa: BLE001 - nothing may escape into the pool
            log.exception("task %s crashed", self.name)
            self.signals.failed.emit(internal_error_message(e), e)
        else:
            self.signals.finished.emit(result)


def submit(
    pool: QThreadPool,
    fn: Callable[..., Any],
    *args: Any,
    on_finished: Callable[[Any], Any] | None = None,
    on_failed: Callable[[str, BaseException], Any] | None = None,
    on_progress: Callable[[str, str], Any] | None = None,
    progress: bool = False,
    **kwargs: Any,
) -> Task:
    """Create a Task, connect the slots and start it on `pool`.

    Slots run on the thread that owns the task's signals (the caller's, normally the main
    thread) through queued connections. `on_progress` may be a bound Qt signal.
    """
    task = Task(fn, *args, progress=progress, **kwargs)
    if on_finished is not None:
        task.signals.finished.connect(on_finished)
    if on_failed is not None:
        task.signals.failed.connect(on_failed)
    if on_progress is not None:
        task.signals.progress.connect(on_progress)
    # Connected last so the user's slots run before the task is released.
    task.signals.finished.connect(task.signals._release)
    task.signals.failed.connect(task.signals._release)
    _LIVE_TASKS.add(task)
    pool.start(task)
    return task
