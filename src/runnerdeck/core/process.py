"""Run a runner directory's run.cmd as a child process and track its state from stdout.

Grounded in docs/spike-notes.md:
- §4: the stdout keywords below are the only local state signal. "Connected to GitHub" is also
  printed on every retry while a stale session blocks us, so it does not mean online.
- §5: only CTRL_BREAK delivered through a console shared with run.cmd stops the listener cleanly
  (it deletes its session, a restart works at once). Every other way is a hard kill and leaves
  the GitHub session behind for ~3 minutes.
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

RUN_COMMAND = ("cmd.exe", "/c", "run.cmd")
DEFAULT_STOP_TIMEOUT_S = 15.0  # spike §8: Ctrl+Break normally ends run.cmd in ~0.4 s
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


@dataclass(frozen=True)
class RunnerEvent:
    """One recognised run.cmd output line.

    detail: runner version (VERSION), job name (BUSY, COMPLETED) or exit code (EXITED).
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
)


def parse_line(line: str) -> RunnerEvent | None:
    """Classify one decoded output line; None for everything else (banners, cmd.exe prompts)."""
    for pattern, kind in _EVENT_PATTERNS:
        m = pattern.search(line)
        if m:
            groups = m.groupdict()
            return RunnerEvent(kind, groups.get("detail"), groups.get("result"))
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
                # EXITED is handled by RunnerProcess from the real exit code.
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


def _merge_env(overrides: Mapping[str, str] | None) -> dict[str, str] | None:
    if not overrides:
        return None
    merged = dict(os.environ)
    if winproc.IS_WINDOWS:
        # Windows env names are case-insensitive; two spellings in one block is undefined.
        upper = {key.upper() for key in overrides}
        merged = {k: v for k, v in merged.items() if k.upper() not in upper}
    merged.update(overrides)
    return merged


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
        self._last_error: str | None = None
        self._last_line: str | None = None
        self._started_at: datetime | None = None
        self._stopped_at: datetime | None = None
        self._last_stop_forced = False
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
        """Exit code of the most recent run; None while running or before the first run."""
        with self._lock:
            return self._exit_code

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
        """True if the most recent run was ended by a hard kill from stop() (session lingers)."""
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
            run_cmd = self._runner_dir / RUN_COMMAND[-1]
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
                    list(RUN_COMMAND),
                    cwd=str(self._runner_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    # Own process group so CTRL_BREAK can target it; deliberately NOT
                    # CREATE_NO_WINDOW, which makes CTRL_BREAK silently ineffective (spike §5).
                    creationflags=winproc.CREATE_NEW_PROCESS_GROUP,
                    env=_merge_env(self._env),
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
                self._last_error = None
                self._last_line = None
                self._started_at = _now()
                self._stopped_at = None
                self._last_stop_forced = False
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
                self._kill(proc.pid)
                self._finalize(gen, None, error=f"无法启动输出读取线程：{e}")
                raise ProcessError(f"无法启动输出读取线程：{e}") from e
        self._emit()

    def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT_S) -> StopResult:
        """Stop the runner: Ctrl+Break first, hard kill if that fails. Final state STOPPED.

        Blocks up to `timeout` (+ kill/drain time). Idempotent: when not running, returns the
        result of the stop that ended the latest run, or graceful=True / exit_code=None.
        Safe to call from any thread except from inside this object's callbacks.
        Raises ProcessError only if the process survives the kill (state stays STOPPING).
        """
        self._forbid_in_callback("stop")
        with self._control_lock:
            with self._lock:
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

            if graceful:
                killed = self._kill_leftovers()
            else:
                with self._lock:
                    self._last_stop_forced = True
                killed = self._kill(pid)
                if not self._wait_proc(proc, KILL_WAIT_S):
                    message = f"无法结束 runner 进程（PID {pid}）"
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
            if error is None and (self._stop_requested or rc == 0):
                state = LocalState.STOPPED
            else:
                if error is None:
                    code = "未知" if rc is None else _format_exit_code(rc)
                    error = f"runner 意外退出（退出码 {code}）"
                    if self._last_line:
                        error += f"：{self._last_line[:200]}"
                self._last_error = error
                state = LocalState.ERROR
            self._publish_locked(state, None)
        log.info("%s exited with %s -> %s", self._runner_dir, rc, state)
        self._emit()
        return True

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

    def _kill(self, pid: int) -> list[int]:
        import psutil

        killed: list[int] = []
        try:
            killed.extend(winproc.kill_tree(pid))
        except (psutil.Error, OSError) as e:
            log.warning("kill_tree(%s) failed: %s", pid, e)
        killed.extend(self._kill_leftovers())
        return list(dict.fromkeys(killed))

    def _kill_leftovers(self) -> list[int]:
        """Kill Runner.* processes still under the dir (e.g. a Worker that outlived its parent)."""
        import psutil

        try:
            leftovers = winproc.find_runner_processes(self._runner_dir)
            if not leftovers:
                return []
            log.warning(
                "killing leftover runner processes in %s: %s",
                self._runner_dir,
                [p.pid for p in leftovers],
            )
            return winproc.kill_processes(leftovers)
        except (psutil.Error, OSError) as e:
            log.warning("cleaning up leftovers in %s failed: %s", self._runner_dir, e)
            return []


# --- processes not owned by a RunnerProcess -----------------------------------------------

# cmd.exe running the runner's own scripts: `cmd /c run.cmd`, `cmd /c "D:\r\x\run-helper.cmd"`.
_RUN_SCRIPT_RE = re.compile(r"""(?:^|[\\/"'\s])run(?:-helper)?\.cmd(?=$|["'\s])""", re.IGNORECASE)


def running_processes(runner_dir: Path) -> list[psutil.Process]:
    """Runner.Listener / Worker / PluginHost processes whose executable is under runner_dir."""
    return winproc.find_runner_processes(runner_dir)


def stop_external(runner_dir: Path) -> list[int]:
    """Hard-kill a runner started elsewhere (another terminal, double-clicked run.cmd).

    Kills each runner process together with its launching cmd.exe chain (only cmd.exe whose
    command line runs run.cmd / run-helper.cmd) and their descendants, like `taskkill /T /F`.
    Always a hard kill: the GitHub session lingers ~3 minutes (spike §5). Returns killed PIDs.
    """
    import psutil

    procs = winproc.find_runner_processes(runner_dir)
    if not procs:
        return []
    protected = _own_lineage()
    runner_pids = {p.pid for p in procs}
    roots: dict[int, psutil.Process] = {}
    for proc in procs:
        if _parent_pid(proc) in runner_pids:
            continue  # e.g. Worker under Listener: covered by the Listener's tree
        root = _launcher_root(proc)
        roots.setdefault(root.pid, root)

    killed: list[int] = []
    for pid in roots:
        if pid in protected:
            log.warning("not killing %s: it is this process or one of its ancestors", pid)
            continue
        if pid in killed:
            continue
        try:
            killed.extend(winproc.kill_tree(pid))
        except (psutil.Error, OSError) as e:
            log.warning("kill_tree(%s) failed: %s", pid, e)
    try:
        leftovers = [p for p in winproc.find_runner_processes(runner_dir) if p.pid not in protected]
        if leftovers:
            killed.extend(winproc.kill_processes(leftovers))
    except (psutil.Error, OSError) as e:
        log.warning("cleaning up leftovers in %s failed: %s", runner_dir, e)
    result = list(dict.fromkeys(killed))
    log.info("stop_external(%s) killed %s", runner_dir, result)
    return result


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
