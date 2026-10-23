"""Run a runner directory's run.cmd as a child process and track its state from stdout.

Grounded in docs/spike-notes.md:
- §4: the stdout keywords below are the only local state signal. "Connected to GitHub" is also
  printed on every retry while a stale session blocks us, so it does not mean online.
- §5: only CTRL_BREAK delivered through a console shared with run.cmd stops the listener cleanly
  (it deletes its session, a restart works at once). Every other way is a hard kill and leaves
  the GitHub session behind for ~3 minutes.
- §5: run.cmd ends with `exit /b 0` whatever the Listener returned (kill_listener: Listener
  code 15, cmd.exe rc=0). The Listener's own exit code is only visible in run-helper's output
  ("Exiting after unknown error code: 15"), so an unexpected exit is detected from that line.
- §8: stdin must be DEVNULL so cmd.exe's "Terminate batch job (Y/N)?" prompt ends by itself, and
  the child must not get CREATE_NO_WINDOW or CTRL_BREAK silently stops reaching it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from runnerdeck.core import winproc
from runnerdeck.core.errors import ProcessError
from runnerdeck.core.models import LocalState
from runnerdeck.core.redact import redact

if TYPE_CHECKING:
    import psutil

log = logging.getLogger(__name__)

RUN_SCRIPT = "run.cmd"  # run as `<abs cmd.exe> /c run.cmd` with cwd = runner dir
DEFAULT_STOP_TIMEOUT_S = 15.0  # spike §8: Ctrl+Break normally ends run.cmd in ~0.4 s
# Real (2026-10-23): during a job the runner first cancels it ("completed with result: Canceled")
# and exited 15.2 s after Ctrl+Break for a trivial step; a hard kill instead orphans the job on
# GitHub (stuck in_progress, runner busy, DELETE refused). Steps with cleanup need longer.
BUSY_STOP_TIMEOUT_S = 90.0
KILL_WAIT_S = 10.0  # after kill_tree, how long to wait for cmd.exe to be gone
READER_JOIN_TIMEOUT_S = 5.0  # after exit, grace for the reader to drain the pipe
MAX_LINE_BYTES = 64 * 1024  # longer "lines" are split rather than buffered without bound


# --- stdout parsing ------------------------------------------------------------------------


class EventKind(StrEnum):
    CONNECTED = "connected"
    CONFLICT = "conflict"
    RECONNECTED = "reconnected"
    VERSION = "version"
    IDLE = "idle"
    BUSY = "busy"
    COMPLETED = "completed"
    STOPPING = "stopping"
    EXITED = "exited"
    DEREGISTERED = "deregistered"  # deleted on GitHub; the runner wipes .runner/.credentials


@dataclass(frozen=True)
class RunnerEvent:
    """One recognised run.cmd output line.

    detail: runner version (VERSION), job name (BUSY, COMPLETED) or the Listener's exit code
    as reported by run-helper (EXITED).
    result: job result such as "Failed" / "Succeeded" (COMPLETED only).
    """

    kind: EventKind
    detail: str | None = None
    result: str | None = None


# Spike §4 table, identical to scripts/spike_common.classify_line (runner 2.338.0 output).
_EVENT_PATTERNS: tuple[tuple[re.Pattern[str], EventKind], ...] = (
    (re.compile(r"Connected to GitHub\s*$"), EventKind.CONNECTED),
    (re.compile(r"A session for this runner already exists"), EventKind.CONFLICT),
    (re.compile(r"Runner reconnected\."), EventKind.RECONNECTED),
    (re.compile(r"Current runner version: '(?P<detail>[\d.]+)'"), EventKind.VERSION),
    (re.compile(r"Listening for Jobs"), EventKind.IDLE),
    (re.compile(r"Running job:\s*(?P<detail>.+?)\s*$"), EventKind.BUSY),
    (
        re.compile(r"Job (?P<detail>.+?) completed with result:\s*(?P<result>\w+)"),
        EventKind.COMPLETED,
    ),
    (re.compile(r"^Exiting\.\.\.\s*$"), EventKind.STOPPING),
    (re.compile(r"Runner listener exit with (?P<detail>-?\d+) return code"), EventKind.EXITED),
    (re.compile(r"Exiting after unknown error code: (?P<detail>-?\d+)"), EventKind.EXITED),
    # Real output 2026-10-23 (runner 2.338.1) after DELETE /actions/runners/{id} while it ran:
    # it removes .credentials/.runner itself and then exits with code 0 (not "terminated error").
    (re.compile(r"The runner no longer exists on the server"), EventKind.DEREGISTERED),
)

# run-helper.cmd's other Listener-exit lines carry no number; each stands for one Listener exit
# code (actions/runner src/Misc/layoutroot/run-helper.cmd.template). Not captured in a spike yet
# (only the "unknown error code" line is, §5): an unmatched wording only loses the detection.
# Codes 2/3/4 make run.cmd relaunch the Listener, whose own exit line then replaces the code.
_HELPER_EXIT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Runner listener exit with terminated error"), "1"),
    (re.compile(r"Runner listener exit with retryable error"), "2"),
    (re.compile(r"Runner listener exit because of updating, re-launch ephemeral runner"), "4"),
    (re.compile(r"Runner listener exit because of updating"), "3"),
    (re.compile(r"Runner listener exit with Session Conflict error"), "5"),
)


def parse_line(line: str) -> RunnerEvent | None:
    """Classify one decoded output line; None for everything else (banners, cmd.exe prompts)."""
    for pattern, kind in _EVENT_PATTERNS:
        m = pattern.search(line)
        if m:
            groups = m.groupdict()
            return RunnerEvent(kind, groups.get("detail"), groups.get("result"))
    for pattern, code in _HELPER_EXIT_PATTERNS:
        if pattern.search(line):
            return RunnerEvent(EventKind.EXITED, code)
    return None


class OutputStateMachine:
    """Pure mapping from parsed output events to LocalState. No IO, not thread-safe."""

    def __init__(self, state: LocalState = LocalState.STARTING) -> None:
        self.state = state
        self.current_job: str | None = None
        self.runner_version: str | None = None
        self.last_job: tuple[str, str] | None = None  # (job, result)

    def feed(self, event: RunnerEvent) -> bool:
        """Apply one event. True if state, current_job or last_job changed."""
        before = (self.state, self.current_job, self.last_job)
        match event.kind:
            case EventKind.CONFLICT:
                self.state = LocalState.RECONNECTING
            case EventKind.RECONNECTED:
                self.state = LocalState.STARTING
            case EventKind.IDLE:
                self.state, self.current_job = LocalState.IDLE, None
            case EventKind.BUSY:
                self.state, self.current_job = LocalState.BUSY, event.detail
            case EventKind.COMPLETED:
                self.state, self.current_job = LocalState.IDLE, None
                if event.detail is not None:
                    self.last_job = (event.detail, event.result or "")
            case EventKind.STOPPING:
                self.state = LocalState.STOPPING
            case EventKind.VERSION:
                self.runner_version = event.detail
            case _:
                # CONNECTED is also printed on every conflict retry, so it proves nothing;
                # EXITED / DEREGISTERED leave the state alone: RunnerProcess records them and
                # decides STOPPED / ERROR when run.cmd ends (cmd.exe itself exits 0).
                pass
        return (self.state, self.current_job, self.last_job) != before


# --- process control -----------------------------------------------------------------------


class StopMethod(StrEnum):
    CTRL_BREAK = "ctrl_break"
    KILL = "kill"


@dataclass(frozen=True)
class StopResult:
    """Outcome of RunnerProcess.stop().

    graceful=False means a hard kill: tell the user the GitHub-side session only expires after
    ~3 minutes and a restart before that shows "reconnecting" (spike §5).
    killed_pids may be non-empty even when graceful (leftover Runner.* processes cleaned up).
    """

    graceful: bool
    method: StopMethod
    exit_code: int | None
    killed_pids: list[int] = field(default_factory=list)


PopenFn = Callable[..., subprocess.Popen[bytes]]
_Notice = tuple[Literal["line", "state", "job"], tuple[Any, ...]]


def _now() -> datetime:
    return datetime.now(UTC)


def _format_exit_code(rc: int) -> str:
    # NTSTATUS-style codes (e.g. 0xC000013A after a console break) read better in hex.
    return f"{rc} (0x{rc & 0xFFFFFFFF:08X})" if rc < 0 or rc > 0xFFFF else str(rc)


def _survivor_error(pids: list[int]) -> str:
    return f"无法结束 runner 进程（PID {', '.join(map(str, pids))}）"


class RunnerProcess:
    """One run.cmd child process, its output ring buffer and its LocalState.

    Threads: start() spawns a daemon reader thread per run. Callbacks are invoked one at a time
    and in the order the changes happened, never while the internal state lock is held, on
    either the reader thread or the thread calling start()/stop() (whichever is delivering at
    that moment). A callback describes a change that already happened; `state` may have moved on
    by the time it runs. Callbacks must be quick and must not call start()/stop()/wait() on the
    same object (RuntimeError); hand that off to another thread. Exceptions from callbacks are
    logged and swallowed. Properties and output() may be called from any thread, also from
    callbacks.
    """

    def __init__(
        self,
        runner_dir: Path,
        *,
        env: Mapping[str, str] | None = None,
        on_line: Callable[[str], None] | None = None,
        on_state: Callable[[LocalState, str | None], None] | None = None,
        on_job_finished: Callable[[str, str], None] | None = None,
        popen: PopenFn = subprocess.Popen,
        max_lines: int = 2000,
    ) -> None:
        if max_lines < 1:
            raise ValueError("max_lines must be >= 1")
        self._runner_dir = Path(runner_dir)
        self._env = dict(env) if env else None
        self._on_line = on_line
        self._on_state = on_state
        self._on_job_finished = on_job_finished
        self._popen = popen

        self._lock = threading.Lock()  # guards every field below
        self._control_lock = threading.Lock()  # serialises start() / stop()
        self._tls = threading.local()  # callback nesting depth per thread

        self._lines: deque[str] = deque(maxlen=max_lines)
        self._pending: deque[_Notice] = deque()
        self._draining = False
        self._log_queue: list[tuple[str, tuple[Any, ...]]] = []  # logged by _emit, unlocked

        self._gen = 0  # incremented per start(); stale reader threads compare against it
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._active = False
        self._stop_requested = False
        self._machine = OutputStateMachine()
        self._state = LocalState.STOPPED
        self._current_job: str | None = None
        self._state_since = _now()
        self._exit_code: int | None = None
        self._listener_exit: int | None = None
        self._deregistered = False  # "The runner no longer exists on the server" seen
        self._last_error: str | None = None
        self._last_line: str | None = None
        self._started_at: datetime | None = None
        self._stopped_at: datetime | None = None
        self._last_stop_forced = False
        self._killed_elsewhere = False  # note_forced_kill() during the current run
        self._last_stop: tuple[int, StopResult] | None = None

    # --- thread-safe read access --------------------------------------------------------------

    @property
    def runner_dir(self) -> Path:
        return self._runner_dir

    @property
    def state(self) -> LocalState:
        with self._lock:
            return self._state

    @property
    def current_job(self) -> str | None:
        with self._lock:
            return self._current_job

    @property
    def runner_version(self) -> str | None:
        with self._lock:
            return self._machine.runner_version

    @property
    def last_job(self) -> tuple[str, str] | None:
        """(job, result) of the last job completed in the current / most recent run."""
        with self._lock:
            return self._machine.last_job

    @property
    def pid(self) -> int | None:
        """PID of the run.cmd cmd.exe while running, else None."""
        with self._lock:
            return self._proc.pid if self._active and self._proc is not None else None

    @property
    def exit_code(self) -> int | None:
        """Exit code of the run.cmd cmd.exe of the most recent run; None while running or before
        the first run. Normally 0 even after a Listener crash (spike §5): see listener_exit_code.
        """
        with self._lock:
            return self._exit_code

    @property
    def listener_exit_code(self) -> int | None:
        """Listener exit code that run-helper last reported in the current / most recent run.

        None until such a line appears (a hard kill of the cmd.exe chain prints none).
        """
        with self._lock:
            return self._listener_exit

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    @property
    def started_at(self) -> datetime | None:
        with self._lock:
            return self._started_at

    @property
    def stopped_at(self) -> datetime | None:
        """When the most recent run ended (UTC); None if it is still running or never ran."""
        with self._lock:
            return self._stopped_at

    @property
    def state_since(self) -> datetime:
        """When `state` last changed (UTC)."""
        with self._lock:
            return self._state_since

    @property
    def last_stop_forced(self) -> bool:
        """True if the most recent run was ended by a hard kill (session lingers ~3 min)."""
        with self._lock:
            return self._last_stop_forced

    def output(self) -> list[str]:
        """Copy of the last max_lines output lines (redacted, blank lines dropped).

        Kept across restarts so a log view shows the previous run too.
        """
        with self._lock:
            return list(self._lines)

    def is_running(self) -> bool:
        with self._lock:
            return self._active

    # --- control ------------------------------------------------------------------------------

    def start(self) -> None:
        """Spawn run.cmd and begin tracking it. State becomes STARTING.

        Raises ProcessError if already running, if another process already runs this runner,
        or if run.cmd cannot be launched.
        """
        self._forbid_in_callback("start")
        with self._control_lock:
            with self._lock:
                if self._active:
                    raise ProcessError(f"runner 已在运行（PID {self._proc_pid_locked()}）")
            run_cmd = self._runner_dir / RUN_SCRIPT
            if not run_cmd.is_file():
                raise ProcessError(f"找不到 {run_cmd}，这不是一个 runner 目录")
            others = self._external_pids()
            if others:
                raise ProcessError(
                    f"目录 {self._runner_dir} 下已有 runner 进程在运行"
                    f"（PID {', '.join(map(str, others))}），可能由其他终端或服务启动；请先停止它"
                )
            try:
                proc = self._popen(
                    # Absolute cmd.exe: never one planted in the runner dir or on PATH.
                    [winproc.cmd_exe(), "/c", RUN_SCRIPT],
                    cwd=str(self._runner_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    # Own process group so CTRL_BREAK can target it; deliberately NOT
                    # CREATE_NO_WINDOW, which makes CTRL_BREAK silently ineffective (spike §5).
                    creationflags=winproc.CREATE_NEW_PROCESS_GROUP,
                    # Always explicit: workflow jobs inherit run.cmd's environment, so the
                    # PAT variables (RUNNERDECK_PAT, GH_TOKEN, ...) must not be in it.
                    env=winproc.child_environment(self._env),
                )
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                raise ProcessError(f"无法启动 {run_cmd}：{e}") from e

            with self._lock:
                self._gen += 1
                gen = self._gen
                self._proc = proc
                self._active = True
                self._stop_requested = False
                self._machine = OutputStateMachine()
                self._exit_code = None
                self._listener_exit = None
                self._deregistered = False
                self._last_error = None
                self._last_line = None
                self._started_at = _now()
                self._stopped_at = None
                self._last_stop_forced = False
                self._killed_elsewhere = False
                self._publish_locked(LocalState.STARTING, None)
                reader = threading.Thread(
                    target=self._read_loop,
                    args=(proc, gen),
                    name=f"runner-output-{self._runner_dir.name}",
                    daemon=True,
                )
                self._reader = reader
            log.info("started %s (pid %s)", self._runner_dir, proc.pid)
            try:
                reader.start()
            except RuntimeError as e:  # interpreter cannot create threads any more
                self._kill(proc.pid)  # best effort; the run is reported as ERROR anyway
                self._finalize(gen, None, error=f"无法启动输出读取线程：{e}")
                raise ProcessError(f"无法启动输出读取线程：{e}") from e
        self._emit()

    def note_forced_kill(self) -> None:
        """Record that the caller is about to hard-kill this run's process tree itself.

        Used by the CLI's second Ctrl+C, which kills run.cmd while stop() is still waiting
        for Ctrl+Break: without this, stop() would see the exit and report it as graceful.
        Safe from any thread; does not take the control lock held by a running stop().
        """
        with self._lock:
            if self._active:
                self._killed_elsewhere = True
                self._last_stop_forced = True

    def stop(self, timeout: float | None = None) -> StopResult:
        """Stop the runner: Ctrl+Break first, hard kill if that fails. Final state STOPPED.

        timeout None: DEFAULT_STOP_TIMEOUT_S when idle, BUSY_STOP_TIMEOUT_S while a job runs (the
        runner cancels the job before exiting; killing it earlier orphans the job on GitHub).
        Blocks up to `timeout` (+ kill/drain time). Idempotent: when not running, returns the
        result of the stop that ended the latest run, or graceful=True / exit_code=None.
        Safe to call from any thread except from inside this object's callbacks.
        Raises ProcessError naming the PIDs that could not be killed:
        - run.cmd's cmd.exe survives the kill: state stays STOPPING, a later stop() retries;
        - run.cmd ended but Runner.* processes under the dir survive (e.g. access denied): the
          run is over, state becomes ERROR with that message in last_error.
        """
        self._forbid_in_callback("stop")
        with self._control_lock:
            with self._lock:
                if timeout is None:
                    busy = self._state == LocalState.BUSY or self._current_job is not None
                    timeout = BUSY_STOP_TIMEOUT_S if busy else DEFAULT_STOP_TIMEOUT_S
                if not self._active:
                    if self._last_stop is not None and self._last_stop[0] == self._gen:
                        return self._last_stop[1]
                    return StopResult(True, StopMethod.CTRL_BREAK, None, [])
                proc, gen, reader = self._proc, self._gen, self._reader
                assert proc is not None
                self._stop_requested = True
                self._publish_locked(LocalState.STOPPING, self._current_job)
            self._emit()

            pid = proc.pid
            graceful = False
            if winproc.has_console():
                try:
                    winproc.send_ctrl_break(pid)
                except ProcessError as e:
                    log.warning("Ctrl+Break to %s failed, killing instead: %s", pid, e)
                else:
                    graceful = self._wait_proc(proc, max(timeout, 0.0))
                    if not graceful:
                        log.warning("%s ignored Ctrl+Break for %.1fs, killing", pid, timeout)
            else:
                log.info("no console: Ctrl+Break cannot reach pid %s, killing", pid)

            with self._lock:
                if self._killed_elsewhere:
                    graceful = False  # the wait ended because note_forced_kill()'s caller killed it
            if graceful:
                killed, survivors = self._kill_leftovers()
            else:
                with self._lock:
                    self._last_stop_forced = True
                killed, survivors = self._kill(pid)
                if not self._wait_proc(proc, KILL_WAIT_S):
                    message = _survivor_error(sorted({pid, *survivors}))
                    with self._lock:
                        self._last_error = message
                    raise ProcessError(message)

            rc = proc.returncode
            self._join_reader(reader, gen, rc)
            result = StopResult(
                graceful,
                StopMethod.CTRL_BREAK if graceful else StopMethod.KILL,
                rc,
                killed,
            )
            with self._lock:
                self._last_stop = (gen, result)
            if survivors:
                message = _survivor_error(survivors)
                self._fail_ended_run(gen, message)
                raise ProcessError(message)
            log.info("stopped %s: %s", self._runner_dir, result)
            return result

    def wait(self, timeout: float | None = None) -> int | None:
        """Wait for the current run to end; its exit code, or None on timeout / never started.

        On return the run's final on_state notification has normally been delivered too.
        """
        self._forbid_in_callback("wait")
        with self._lock:
            proc, gen, reader, active = self._proc, self._gen, self._reader, self._active
        if proc is None:
            return None
        if active:
            try:
                rc = proc.wait(timeout)
            except subprocess.TimeoutExpired:
                return None
            self._join_reader(reader, gen, rc)
        elif reader is not None and reader is not threading.current_thread():
            reader.join(READER_JOIN_TIMEOUT_S)  # it may still be delivering callbacks
        return self.exit_code

    # --- reader thread ----------------------------------------------------------------------

    def _read_loop(self, proc: subprocess.Popen[bytes], gen: int) -> None:
        stream = proc.stdout
        try:
            if stream is not None:
                while True:
                    raw = stream.readline(MAX_LINE_BYTES)
                    if not raw:
                        break
                    self._handle_line(gen, raw)
        except (OSError, ValueError) as e:  # pipe broken / closed underneath us
            log.warning("reading output of %s failed: %s", self._runner_dir, e)
        except Exception:
            log.exception("unexpected error reading output of %s", self._runner_dir)
        finally:
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        try:
            rc: int | None = proc.wait()
        except Exception:
            log.exception("waiting for %s failed", self._runner_dir)
            rc = None
        self._finalize(gen, rc)

    def _handle_line(self, gen: int, raw: bytes) -> None:
        line = redact(winproc.decode_line(raw))
        if not line.strip():
            return
        event = parse_line(line)
        with self._lock:
            if gen != self._gen:
                return  # late output of a previous run
            self._lines.append(line)
            self._last_line = line
            if self._on_line is not None:
                self._pending.append(("line", (line,)))
            if event is not None and self._active:
                self._apply_event_locked(event)
        log.debug("[%s] %s", self._runner_dir.name, line)
        self._emit()

    def _apply_event_locked(self, event: RunnerEvent) -> None:
        machine = self._machine
        machine.feed(event)
        if event.kind == EventKind.DEREGISTERED:
            self._deregistered = True
        if event.kind == EventKind.EXITED and event.detail is not None:
            # A relaunch (codes 2/3/4) prints a new line when that Listener exits.
            self._listener_exit = int(event.detail)
        if event.kind == EventKind.COMPLETED and machine.last_job is not None:
            if self._on_job_finished is not None:
                self._pending.append(("job", machine.last_job))
            self._log_queue.append(
                ("%s: job %s finished: %s", (self._runner_dir.name, *machine.last_job))
            )
        # Once stop() is under way the published state stays STOPPING whatever the output says.
        state = LocalState.STOPPING if self._stop_requested else machine.state
        self._publish_locked(state, machine.current_job)

    def _finalize(self, gen: int, rc: int | None, *, error: str | None = None) -> bool:
        """End the run `gen` exactly once (reader thread, or stop()/wait() as a fallback)."""
        with self._lock:
            if gen != self._gen or not self._active:
                return False
            self._active = False
            self._exit_code = rc
            self._stopped_at = _now()
            if error is None and not self._stop_requested:
                error = self._unexpected_exit_locked(rc)
            if error is None:
                state = LocalState.STOPPED
            else:
                self._last_error = error
                state = LocalState.ERROR
            self._publish_locked(state, None)
            listener = self._listener_exit
        log.info("%s exited with %s (listener %s) -> %s", self._runner_dir, rc, listener, state)
        self._emit()
        return True

    def _unexpected_exit_locked(self, rc: int | None) -> str | None:
        """Error text for a run that ended without stop(), or None if it ended cleanly.

        cmd.exe's rc alone says nothing: run.cmd returns 0 after any Listener exit (spike §5),
        so a non-zero Listener code from run-helper's line counts as well.
        """
        if self._deregistered:
            return (
                "runner 已在 GitHub 端被删除，并清除了本地的 .runner / .credentials；"
                "请先 remove 再重新 add"
            )
        listener = self._listener_exit
        if rc == 0 and not listener:
            return None
        codes = []
        if rc != 0:
            codes.append(f"退出码 {'未知' if rc is None else _format_exit_code(rc)}")
        if listener:
            codes.append(f"Listener 退出码 {_format_exit_code(listener)}")
        error = f"runner 意外退出（{'，'.join(codes)}）"
        if self._last_line:
            error += f"：{self._last_line[:200]}"
        return error

    def _fail_ended_run(self, gen: int, message: str) -> None:
        """Mark the already finalized run `gen` as ERROR (stop() left runner processes behind)."""
        with self._lock:
            if gen != self._gen or self._active:
                return
            self._last_error = message
            self._publish_locked(LocalState.ERROR, None)
        self._emit()

    def _join_reader(self, reader: threading.Thread | None, gen: int, rc: int | None) -> None:
        """After the process exited: let the reader drain, finalize ourselves if it is stuck.

        A reader can stay blocked if an orphaned grandchild still holds the pipe open.
        """
        if reader is not None and reader is not threading.current_thread():
            reader.join(READER_JOIN_TIMEOUT_S)
            if reader.is_alive():
                log.warning("output pipe of %s still open after exit", self._runner_dir)
        self._finalize(gen, rc)

    # --- helpers ------------------------------------------------------------------------------

    def _publish_locked(self, state: LocalState, job: str | None) -> None:
        if state == self._state and job == self._current_job:
            return
        if state != self._state:
            self._state_since = _now()
            self._log_queue.append(("%s: %s -> %s", (self._runner_dir.name, self._state, state)))
        self._state, self._current_job = state, job
        if self._on_state is not None:
            self._pending.append(("state", (state, job)))

    def _emit(self) -> None:
        """Deliver queued notifications in order; only one thread delivers at a time.

        A thread that finds another one delivering returns at once (that one drains the queue),
        so producers never block on a slow callback.
        """
        with self._lock:
            logs, self._log_queue = self._log_queue, []
            deliver = not self._draining and bool(self._pending)
            if deliver:
                self._draining = True
        for message, args in logs:
            log.info(message, *args)
        if not deliver:
            return
        try:
            while True:
                with self._lock:
                    if not self._pending:
                        self._draining = False
                        return
                    kind, args = self._pending.popleft()
                self._deliver(kind, args)
        except BaseException:
            with self._lock:
                self._draining = False
            raise

    def _deliver(self, kind: str, args: tuple[Any, ...]) -> None:
        callback: Callable[..., None] | None
        if kind == "line":
            callback = self._on_line
        elif kind == "state":
            callback = self._on_state
        else:
            callback = self._on_job_finished
        if callback is None:
            return
        self._tls.depth = getattr(self._tls, "depth", 0) + 1
        try:
            callback(*args)
        except Exception:
            log.exception("RunnerProcess %s callback failed", kind)
        finally:
            self._tls.depth -= 1

    def _forbid_in_callback(self, name: str) -> None:
        if getattr(self._tls, "depth", 0):
            raise RuntimeError(f"RunnerProcess.{name}() must not be called from its own callbacks")

    def _proc_pid_locked(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @staticmethod
    def _wait_proc(proc: subprocess.Popen[bytes], timeout: float) -> bool:
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return False
        return True

    def _external_pids(self) -> list[int]:
        import psutil

        try:
            return [p.pid for p in winproc.find_runner_processes(self._runner_dir)]
        except (psutil.Error, OSError) as e:
            raise ProcessError(f"无法检查 {self._runner_dir} 下的 runner 进程：{e}") from e

    def _kill(self, pid: int) -> tuple[list[int], list[int]]:
        """Kill the run.cmd tree, then leftovers. (gone pids, surviving Runner.* pids).

        Whether cmd.exe itself is gone is left to the caller (it waits on the Popen).
        """
        import psutil

        killed: list[int] = []
        try:
            gone, alive = winproc.kill_tree_report(pid)
            killed.extend(gone)
            if alive:
                log.warning("still alive after kill_tree(%s): %s", pid, alive)
        except (psutil.Error, OSError) as e:
            log.warning("kill_tree(%s) failed: %s", pid, e)
        gone, survivors = self._kill_leftovers()
        killed.extend(gone)
        return list(dict.fromkeys(killed)), survivors

    def _kill_leftovers(self) -> tuple[list[int], list[int]]:
        """Kill Runner.* processes still under the dir (e.g. a Worker that outlived its parent).

        Returns (gone pids, surviving pids). If the process scan itself fails nothing is known
        to survive, so ([], []).
        """
        import psutil

        try:
            leftovers = winproc.find_runner_processes(self._runner_dir)
        except (psutil.Error, OSError) as e:
            log.warning("cleaning up leftovers in %s failed: %s", self._runner_dir, e)
            return [], []
        if not leftovers:
            return [], []
        log.warning(
            "killing leftover runner processes in %s: %s",
            self._runner_dir,
            [p.pid for p in leftovers],
        )
        return _kill_checked(leftovers)


# --- processes not owned by a RunnerProcess -----------------------------------------------

# cmd.exe running the runner's own scripts: `cmd /c run.cmd`, `cmd /c "D:\r\x\run-helper.cmd"`.
_RUN_SCRIPT_RE = re.compile(r"""(?:^|[\\/"'\s])run(?:-helper)?\.cmd(?=$|["'\s])""", re.IGNORECASE)


def running_processes(runner_dir: Path) -> list[psutil.Process]:
    """Runner.Listener / Worker / PluginHost processes whose executable is under runner_dir."""
    return winproc.find_runner_processes(runner_dir)


@dataclass(frozen=True)
class ExternalStop:
    """Outcome of stop_external()."""

    killed: list[int]  # pids confirmed gone
    survivors: list[int]  # Runner.*.exe / run.cmd cmd.exe pids still alive after the kill

    @property
    def ok(self) -> bool:
        return not self.survivors


def stop_external(runner_dir: Path) -> ExternalStop:
    """Hard-kill a runner started elsewhere (another terminal, double-clicked run.cmd).

    Kills each runner process together with its launching cmd.exe chain (only cmd.exe whose
    command line runs run.cmd / run-helper.cmd) and their descendants, like `taskkill /T /F`.
    Always a hard kill: the GitHub session lingers ~3 minutes (spike §5).
    Never kills this process or its ancestors; runner processes among them count as survivors.
    No runner processes -> ExternalStop([], []). Raises ProcessError if the scan fails.
    """
    import psutil

    try:
        procs = winproc.find_runner_processes(runner_dir)
    except (psutil.Error, OSError) as e:
        raise ProcessError(f"无法检查 {runner_dir} 下的 runner 进程：{e}") from e
    if not procs:
        return ExternalStop([], [])
    protected = _own_lineage()
    runner_pids = {p.pid for p in procs}
    roots: dict[int, psutil.Process] = {}
    for proc in procs:
        if _parent_pid(proc) in runner_pids:
            continue  # e.g. Worker under Listener: covered by the Listener's tree
        root = _launcher_root(proc)
        roots.setdefault(root.pid, root)
    shells = {pid for pid in roots if pid not in runner_pids and pid not in protected}

    is_gone: dict[int, bool] = {}  # latest observation per pid, in kill order

    def observe(gone: list[int], alive: list[int]) -> None:
        is_gone.update(dict.fromkeys(gone, True))
        is_gone.update(dict.fromkeys(alive, False))

    for pid, root in roots.items():
        if pid in protected:
            log.warning("not killing %s: it is this process or one of its ancestors", pid)
            continue
        if is_gone.get(pid):
            continue
        try:
            observe(*winproc.kill_tree_report(pid))
        except (psutil.Error, OSError) as e:
            log.warning("kill_tree(%s) failed: %s", pid, e)
            observe([], [pid] if _still_running(root) else [])

    # The rescan is the truth for runner processes: kill what the trees missed (or what was
    # started meanwhile); runner processes it no longer finds are gone.
    try:
        leftovers = winproc.find_runner_processes(runner_dir)
    except (psutil.Error, OSError) as e:
        log.warning("rescanning %s failed: %s", runner_dir, e)
        leftovers = [p for p in procs if _still_running(p)]
    leftover_pids = {p.pid for p in leftovers}
    observe(sorted(runner_pids - leftover_pids), sorted(leftover_pids))
    runner_pids |= leftover_pids
    targets = [p for p in leftovers if p.pid not in protected]
    if targets:
        observe(*_kill_checked(targets))
    result = ExternalStop(
        [pid for pid, gone in is_gone.items() if gone],
        sorted(
            pid
            for pid, gone in is_gone.items()
            if not gone and (pid in runner_pids or pid in shells)
        ),
    )
    log.info("stop_external(%s): %s", runner_dir, result)
    return result


def _kill_checked(procs: list[psutil.Process]) -> tuple[list[int], list[int]]:
    """winproc.kill_processes_report that never hides a survivor behind an error.

    psutil's wait raises AccessDenied on Windows for a process we may not open (e.g. one
    started elevated); such a process was not killed either, so check what is still running.
    """
    import psutil

    try:
        return winproc.kill_processes_report(procs)
    except (psutil.Error, OSError) as e:
        log.warning("killing %s failed: %s", [p.pid for p in procs], e)
    alive = sorted(p.pid for p in procs if _still_running(p))
    return sorted(p.pid for p in procs if p.pid not in alive), alive


def _still_running(proc: psutil.Process) -> bool:
    import psutil

    try:
        return proc.is_running()
    except psutil.Error:  # cannot tell: never report it as gone
        return True


def _parent_pid(proc: psutil.Process) -> int | None:
    import psutil

    try:
        return proc.ppid()
    except psutil.Error:
        return None


def _is_run_script_shell(proc: psutil.Process) -> bool:
    import psutil

    try:
        if proc.name().lower() != "cmd.exe":
            return False
        return bool(_RUN_SCRIPT_RE.search(" ".join(proc.cmdline())))
    except psutil.Error:  # gone, or AccessDenied: never kill what we cannot identify
        return False


def _launcher_root(proc: psutil.Process) -> psutil.Process:
    """Topmost ancestor in the unbroken cmd.exe(run.cmd / run-helper.cmd) chain, else proc."""
    import psutil

    root = proc
    with contextlib.suppress(psutil.Error):
        parent = proc.parent()
        while parent is not None and _is_run_script_shell(parent):
            root = parent
            parent = parent.parent()
    return root


def _own_lineage() -> set[int]:
    import psutil

    pids = {os.getpid()}
    with contextlib.suppress(psutil.Error):
        pids.update(p.pid for p in psutil.Process().parents())
    return pids
