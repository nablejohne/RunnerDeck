"""Tests for runnerdeck.core.process.

Most tests drive RunnerProcess with a fake Popen whose stdout replays real run.cmd captures
(tests/fixtures/stdout/), with winproc's Windows-only helpers monkeypatched. The tests at the
bottom run a real cmd.exe + fake listener and only run on Windows.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil
import pytest

from runnerdeck.core import process as process_mod
from runnerdeck.core import winproc
from runnerdeck.core.errors import ProcessError
from runnerdeck.core.models import LocalState
from runnerdeck.core.process import (
    EventKind,
    ExternalStop,
    OutputStateMachine,
    RunnerEvent,
    RunnerProcess,
    StopMethod,
    StopResult,
    parse_line,
    running_processes,
    stop_external,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "stdout"
FAKE_RUNNER = Path(__file__).resolve().parent / "fake_runner"
S = LocalState


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.005)


# --- parse_line ------------------------------------------------------------------------------

SESSION_CONFLICT_LINE = (
    '"Runner listener exit with Session Conflict error, stop the service, no retry needed."'
)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("\u221a Connected to GitHub", RunnerEvent(EventKind.CONNECTED)),
        ("\xa1\xcc Connected to GitHub  ", RunnerEvent(EventKind.CONNECTED)),  # GBK as latin-1
        ("A session for this runner already exists.", RunnerEvent(EventKind.CONFLICT)),
        ("2026-10-22 08:19:06Z: Runner reconnected.", RunnerEvent(EventKind.RECONNECTED)),
        ("Current runner version: '2.338.0'", RunnerEvent(EventKind.VERSION, "2.338.0")),
        ("2026-10-22 08:09:45Z: Listening for Jobs", RunnerEvent(EventKind.IDLE)),
        ("2026-10-22 08:09:53Z: Running job: spike", RunnerEvent(EventKind.BUSY, "spike")),
        ("Running job:   build windows  ", RunnerEvent(EventKind.BUSY, "build windows")),
        (
            "2026-10-22 08:10:02Z: Job spike completed with result: Failed",
            RunnerEvent(EventKind.COMPLETED, "spike", "Failed"),
        ),
        (
            "Job build (x64) completed with result: Succeeded",
            RunnerEvent(EventKind.COMPLETED, "build (x64)", "Succeeded"),
        ),
        ("Exiting...", RunnerEvent(EventKind.STOPPING)),
        ("Exiting...  ", RunnerEvent(EventKind.STOPPING)),
        (
            '"Runner listener exit with 0 return code, stop the service, no retry needed."',
            RunnerEvent(EventKind.EXITED, "0"),
        ),
        ('"Exiting after unknown error code: 15"', RunnerEvent(EventKind.EXITED, "15")),
        ("Exiting after unknown error code: -1", RunnerEvent(EventKind.EXITED, "-1")),
        # run-helper.cmd.template lines without a number: mapped to the code they stand for
        (
            '"Runner listener exit with terminated error, stop the service, no retry needed."',
            RunnerEvent(EventKind.EXITED, "1"),
        ),
        (
            '"Runner listener exit with retryable error, re-launch runner in 5 seconds."',
            RunnerEvent(EventKind.EXITED, "2"),
        ),
        (
            '"Runner listener exit because of updating, re-launch runner in 5 seconds"',
            RunnerEvent(EventKind.EXITED, "3"),
        ),
        (
            '"Runner listener exit because of updating, re-launch ephemeral runner in 5 seconds"',
            RunnerEvent(EventKind.EXITED, "4"),
        ),
        (
            SESSION_CONFLICT_LINE,
            RunnerEvent(EventKind.EXITED, "5"),
        ),
    ],
)
def test_parse_line_recognises_spike_patterns(line: str, expected: RunnerEvent) -> None:
    assert parse_line(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "",
        "已复制         1 个文件。",
        "终止批处理操作吗(Y/N)? ",
        "2026-10-22 08:16:53Z: Runner connect error: Error: Conflict. Retrying until reconnected.",
        '"Exiting runner..."',
        '"Restarting runner..."',
        "Exiting after a while",
        "  Exiting...",  # anchored: only the listener's own line counts
        "Connected to GitHub, now doing something else",
    ],
)
def test_parse_line_ignores_other_lines(line: str) -> None:
    assert parse_line(line) is None


def test_parse_line_matches_spike_classifier_on_all_fixtures() -> None:
    spike_common = pytest.importorskip("spike_common")
    for path in sorted(FIXTURES.glob("*.txt")):
        for line in path.read_text(encoding="utf-8").splitlines():
            ours = parse_line(line)
            theirs = spike_common.classify_line(line)
            if theirs is None:
                assert ours is None, line
                continue
            assert ours is not None, line
            assert ours.kind == theirs.kind, line
            if ours.kind == EventKind.COMPLETED:
                assert f"{ours.detail}: {ours.result}" == theirs.detail
            else:
                assert ours.detail == theirs.detail


# --- OutputStateMachine --------------------------------------------------------------------


def replay(lines: list[str]) -> tuple[OutputStateMachine, list[tuple[LocalState, str | None]]]:
    machine = OutputStateMachine()
    seen: list[tuple[LocalState, str | None]] = []
    for line in lines:
        event = parse_line(line)
        if event is not None and machine.feed(event):
            snapshot = (machine.state, machine.current_job)
            if not seen or seen[-1] != snapshot:
                seen.append(snapshot)
    return machine, seen


def test_machine_session_conflict_reconnect() -> None:
    machine, seen = replay(fixture_lines("session_conflict_reconnect.txt"))
    assert seen == [(S.RECONNECTING, None), (S.STARTING, None), (S.IDLE, None)]
    assert machine.runner_version == "2.338.0"
    assert machine.last_job is None


def test_machine_job_then_ctrl_break() -> None:
    machine, seen = replay(fixture_lines("job_then_ctrl_break.txt"))
    assert seen == [(S.IDLE, None), (S.BUSY, "spike"), (S.IDLE, None), (S.STOPPING, None)]
    assert machine.last_job == ("spike", "Failed")
    assert machine.runner_version == "2.338.0"


def test_machine_conflict_then_kill_listener_exit_does_not_change_state() -> None:
    machine, seen = replay(fixture_lines("conflict_then_kill_listener.txt"))
    assert seen == [(S.RECONNECTING, None), (S.STARTING, None), (S.IDLE, None)]
    assert machine.state == S.IDLE  # "Exiting after unknown error code: 15" is left to the exit


def test_machine_gbk_raw_capture_decodes_per_line() -> None:
    raw = fixture_bytes("job_then_ctrl_break.cp936.raw")
    lines = [winproc.decode_line(chunk, "gbk") for chunk in raw.splitlines(keepends=True)]
    assert lines[0] == "已复制         1 个文件。"
    assert "终止批处理操作吗(Y/N)? " in lines
    machine, seen = replay(lines)
    assert seen == [(S.IDLE, None), (S.BUSY, "spike"), (S.IDLE, None), (S.STOPPING, None)]
    assert machine.last_job == ("spike", "Failed")


def test_machine_feed_return_values() -> None:
    m = OutputStateMachine()
    assert m.state == S.STARTING
    assert m.feed(RunnerEvent(EventKind.CONNECTED)) is False
    assert m.feed(RunnerEvent(EventKind.VERSION, "2.338.0")) is False
    assert m.runner_version == "2.338.0"
    assert m.feed(RunnerEvent(EventKind.CONFLICT)) is True
    assert m.feed(RunnerEvent(EventKind.CONNECTED)) is False
    assert m.state == S.RECONNECTING  # connected during conflict retries is not "online"
    assert m.feed(RunnerEvent(EventKind.CONFLICT)) is False
    assert m.feed(RunnerEvent(EventKind.RECONNECTED)) is True
    assert m.feed(RunnerEvent(EventKind.IDLE)) is True
    assert m.feed(RunnerEvent(EventKind.IDLE)) is False
    assert m.feed(RunnerEvent(EventKind.BUSY, "a")) is True
    assert m.feed(RunnerEvent(EventKind.BUSY, "b")) is True  # job changed, state did not
    assert (m.state, m.current_job) == (S.BUSY, "b")
    assert m.feed(RunnerEvent(EventKind.COMPLETED, "b", "Succeeded")) is True
    assert (m.state, m.current_job, m.last_job) == (S.IDLE, None, ("b", "Succeeded"))
    # same completion again changes nothing; a different one only changes last_job
    assert m.feed(RunnerEvent(EventKind.COMPLETED, "b", "Succeeded")) is False
    assert m.feed(RunnerEvent(EventKind.COMPLETED, "c", "Failed")) is True
    assert m.feed(RunnerEvent(EventKind.EXITED, "0")) is False
    assert m.feed(RunnerEvent(EventKind.STOPPING)) is True
    assert m.feed(RunnerEvent(EventKind.EXITED, "0")) is False
    assert m.state == S.STOPPING


def test_machine_idle_clears_job_after_missed_completion() -> None:
    m = OutputStateMachine(S.IDLE)
    m.feed(RunnerEvent(EventKind.BUSY, "x"))
    m.feed(RunnerEvent(EventKind.IDLE))
    assert (m.state, m.current_job, m.last_job) == (S.IDLE, None, None)


# --- fakes for RunnerProcess -----------------------------------------------------------------


class FakePipe:
    """Blocking byte pipe: readline() waits for a full line, the size limit, or EOF."""

    def __init__(self, data: bytes = b"") -> None:
        self._buf = bytearray(data)
        self._eof = False
        self._cond = threading.Condition()
        self.closed = False

    def write(self, data: bytes) -> None:
        with self._cond:
            self._buf += data
            self._cond.notify_all()

    def close_writer(self) -> None:
        with self._cond:
            self._eof = True
            self._cond.notify_all()

    def readline(self, size: int = -1) -> bytes:
        with self._cond:
            while True:
                idx = self._buf.find(b"\n")
                end = idx + 1 if idx >= 0 else None
                if size is not None and size > 0 and (end is None or end > size):
                    end = size if len(self._buf) >= size else None
                if end is None and self._eof:
                    end = len(self._buf)
                if end is not None:
                    chunk = bytes(self._buf[:end])
                    del self._buf[:end]
                    return chunk
                self._cond.wait()

    def close(self) -> None:
        self.closed = True


class FakeProc:
    def __init__(self, args: list[str], kwargs: dict[str, Any], pid: int, data: bytes) -> None:
        self.args = args
        self.kwargs = kwargs
        self.pid = pid
        self.stdout = FakePipe(data)
        self.returncode: int | None = None
        self._exited = threading.Event()

    def finish(self, rc: int, tail: bytes = b"", *, close_pipe: bool = True) -> None:
        self.stdout.write(tail)
        self.returncode = rc
        self._exited.set()
        if close_pipe:
            self.stdout.close_writer()

    def wait(self, timeout: float | None = None) -> int:
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired(self.args, timeout or 0)
        assert self.returncode is not None
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode


class FakePopen:
    """Popen replacement. `exit_code` set -> the process ends right after printing `data`."""

    def __init__(self, data: bytes = b"", *, exit_code: int | None = None) -> None:
        self.data = data
        self.exit_code = exit_code
        self.procs: list[FakeProc] = []
        self.error: Exception | None = None

    def __call__(self, args: list[str], **kwargs: Any) -> FakeProc:
        if self.error is not None:
            raise self.error
        proc = FakeProc(args, kwargs, pid=40000 + len(self.procs), data=self.data)
        self.procs.append(proc)
        if self.exit_code is not None:
            proc.finish(self.exit_code)
        return proc

    @property
    def last(self) -> FakeProc:
        return self.procs[-1]


class FakeWin:
    """Monkeypatched winproc: console presence, Ctrl+Break effect, kill effect, process scan."""

    def __init__(self, popen: FakePopen) -> None:
        self.popen = popen
        self.console = False
        self.ctrl_break_tail: bytes | None = (
            b"Exiting...\r\n\xd6\xd5\xd6\xb9\xc5\xfa\xb4\xa6\xc0\xed\xb2\xd9\xd7\xf7\xc2\xf0(Y/N)? "
            b'\r\n"Runner listener exit with 0 return code, stop the service, no retry needed."'
            b'\r\n"Exiting runner..."\r\n'
        )  # None: Ctrl+Break is ignored
        self.ctrl_break_error: ProcessError | None = None
        self.kill_works = True
        self.unkillable: set[int] = set()  # runner_procs pids that kill_processes cannot end
        self.runner_procs: list[Any] = []
        self.calls: list[tuple[str, Any]] = []

    def has_console(self) -> bool:
        return self.console

    def send_ctrl_break(self, pid: int) -> None:
        self.calls.append(("ctrl_break", pid))
        if self.ctrl_break_error is not None:
            raise self.ctrl_break_error
        if self.ctrl_break_tail is not None:
            proc = self._proc(pid)
            threading.Timer(0.02, proc.finish, args=(0, self.ctrl_break_tail)).start()

    def kill_tree_report(self, pid: int, *, timeout: float = 5.0) -> tuple[list[int], list[int]]:
        self.calls.append(("kill_tree", pid))
        proc = next((p for p in self.popen.procs if p.pid == pid), None)
        if proc is None:  # not one of ours (stop_external tests)
            return [pid], []
        if not self.kill_works:
            return [], [pid]
        proc.finish(1)
        return [pid + 1, pid], []

    def kill_processes_report(
        self, procs: list[Any], *, timeout: float = 5.0
    ) -> tuple[list[int], list[int]]:
        self.calls.append(("kill_processes", [p.pid for p in procs]))
        gone = [p for p in procs if p.pid not in self.unkillable]
        for p in gone:
            p.alive = False
        self.runner_procs = [p for p in self.runner_procs if p not in gone]
        return [p.pid for p in gone], [p.pid for p in procs if p not in gone]

    def find_runner_processes(self, runner_dir: Path) -> list[Any]:
        self.calls.append(("find", runner_dir))
        return list(self.runner_procs)

    def _proc(self, pid: int) -> FakeProc:
        return next(p for p in self.popen.procs if p.pid == pid)

    def called(self, name: str) -> list[Any]:
        return [arg for call, arg in self.calls if call == name]


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self._lock = threading.Lock()

    def on_line(self, line: str) -> None:
        with self._lock:
            self.events.append(("line", line))

    def on_state(self, state: LocalState, job: str | None) -> None:
        with self._lock:
            self.events.append(("state", state, job))

    def on_job_finished(self, job: str, result: str) -> None:
        with self._lock:
            self.events.append(("job", job, result))

    @property
    def states(self) -> list[tuple[LocalState, str | None]]:
        with self._lock:
            return [(e[1], e[2]) for e in self.events if e[0] == "state"]

    @property
    def jobs(self) -> list[tuple[str, str]]:
        with self._lock:
            return [(e[1], e[2]) for e in self.events if e[0] == "job"]

    @property
    def lines(self) -> list[str]:
        with self._lock:
            return [e[1] for e in self.events if e[0] == "line"]

    def callbacks(self) -> dict[str, Any]:
        return {
            "on_line": self.on_line,
            "on_state": self.on_state,
            "on_job_finished": self.on_job_finished,
        }


@pytest.fixture
def runner_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runner"
    d.mkdir()
    (d / "run.cmd").write_text("@echo off\r\n", encoding="ascii")
    return d


@pytest.fixture
def popen() -> FakePopen:
    return FakePopen()


@pytest.fixture
def win(monkeypatch: pytest.MonkeyPatch, popen: FakePopen) -> FakeWin:
    fake = FakeWin(popen)
    for name in (
        "has_console",
        "send_ctrl_break",
        "kill_tree_report",
        "kill_processes_report",
        "find_runner_processes",
    ):
        monkeypatch.setattr(winproc, name, getattr(fake, name))
    monkeypatch.setattr(process_mod, "READER_JOIN_TIMEOUT_S", 2.0)
    return fake


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


def make(runner_dir: Path, popen: FakePopen, rec: Recorder, **kw: Any) -> RunnerProcess:
    return RunnerProcess(runner_dir, popen=popen, **rec.callbacks(), **kw)


IDLE_OUTPUT = (
    b"\r\n\xe2\x88\x9a Connected to GitHub\r\n\r\nCurrent runner version: '2.338.0'\r\n"
    b"2026-10-22 08:09:45Z: Listening for Jobs\r\n"
)


class FakePsProc:
    """Just enough of psutil.Process for stop_external / leftover cleanup."""

    def __init__(
        self,
        pid: int,
        name: str,
        cmdline: list[str] | None = None,
        parent: FakePsProc | None = None,
        *,
        deny: bool = False,
    ) -> None:
        self.pid = pid
        self._name = name
        self._cmdline = cmdline or [name]
        self._parent = parent
        self._deny = deny
        self.alive = True

    def name(self) -> str:
        return self._name

    def cmdline(self) -> list[str]:
        if self._deny:
            raise psutil.AccessDenied(self.pid)
        return self._cmdline

    def parent(self) -> FakePsProc | None:
        return self._parent

    def ppid(self) -> int:
        return self._parent.pid if self._parent else 0

    def is_running(self) -> bool:
        return self.alive


# --- RunnerProcess: replaying captured output -------------------------------------------------


def test_start_launches_run_cmd_with_required_flags(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    proc = make(runner_dir, popen, rec, env={"PSExecutionPolicyPreference": "RemoteSigned"})
    proc.start()
    try:
        fake = popen.last
        assert fake.args == [winproc.cmd_exe(), "/c", "run.cmd"]
        kw = fake.kwargs
        assert kw["cwd"] == str(runner_dir)
        assert kw["stdin"] is subprocess.DEVNULL
        assert kw["stdout"] is subprocess.PIPE
        assert kw["stderr"] is subprocess.STDOUT
        assert kw["creationflags"] == winproc.CREATE_NEW_PROCESS_GROUP
        assert kw["creationflags"] & winproc.CREATE_NO_WINDOW == 0
        assert kw["env"]["PSExecutionPolicyPreference"] == "RemoteSigned"
        assert kw["env"]["PATH"] == os.environ["PATH"]
        assert proc.is_running()
        assert proc.state == S.STARTING
        assert proc.pid == fake.pid
        assert proc.started_at is not None
        assert rec.states[0] == (S.STARTING, None)
    finally:
        proc.stop()


def test_start_uses_absolute_cmd_exe_on_windows(
    runner_dir: Path,
    popen: FakePopen,
    win: FakeWin,
    rec: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bare "cmd.exe" is looked up in the cwd (the runner dir) and PATH first.
    monkeypatch.setattr(winproc, "IS_WINDOWS", True)
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec)
    proc.start()
    proc.wait(5)
    assert popen.last.args == [os.path.join("C:\\Windows", "System32", "cmd.exe"), "/c", "run.cmd"]


SECRETS = {
    "RUNNERDECK_PAT": "github_pat_" + "A" * 30,
    "RUNNERDECK_TEST_PAT": "github_pat_" + "B" * 30,
    "GH_TOKEN": "ghp_" + "C" * 36,
    "GITHUB_TOKEN": "ghp_" + "D" * 36,
}


@pytest.mark.parametrize(
    "overrides",
    [None, {"https_proxy": "http://127.0.0.1:7890", "http_proxy": "http://127.0.0.1:7890"}],
)
def test_child_environment_never_carries_the_pat(
    runner_dir: Path,
    popen: FakePopen,
    win: FakeWin,
    rec: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str] | None,
) -> None:
    # Workflow jobs inherit run.cmd's environment: a PAT there ends up in job logs.
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec, env=overrides)
    proc.start()
    proc.wait(5)
    env = popen.last.kwargs["env"]
    assert isinstance(env, dict)  # never None: that would inherit everything
    assert env["PATH"] == os.environ["PATH"]
    assert not set(SECRETS) & set(env)
    assert not set(SECRETS.values()) & set(env.values())
    for key, value in (overrides or {}).items():
        assert env[key] == value


def test_child_environment_is_case_insensitive_on_windows(
    runner_dir: Path,
    popen: FakePopen,
    win: FakeWin,
    rec: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(winproc, "IS_WINDOWS", True)
    monkeypatch.setenv("runnerdeck_pat", SECRETS["RUNNERDECK_PAT"])
    monkeypatch.setenv("HTTPS_PROXY", "http://old:1")
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec, env={"https_proxy": "http://127.0.0.1:7890"})
    proc.start()
    proc.wait(5)
    env = popen.last.kwargs["env"]
    assert "runnerdeck_pat" not in env
    assert env["https_proxy"] == "http://127.0.0.1:7890"
    assert "HTTPS_PROXY" not in env  # two spellings in one Windows env block is undefined


@pytest.mark.parametrize(
    ("fixture", "states", "jobs"),
    [
        (
            "job_then_ctrl_break.txt",
            [
                (S.STARTING, None),
                (S.IDLE, None),
                (S.BUSY, "spike"),
                (S.IDLE, None),
                (S.STOPPING, None),
                (S.STOPPED, None),
            ],
            [("spike", "Failed")],
        ),
        (
            "session_conflict_reconnect.txt",
            [
                (S.STARTING, None),
                (S.RECONNECTING, None),
                (S.STARTING, None),
                (S.IDLE, None),
                (S.STOPPED, None),
            ],
            [],
        ),
    ],
)
def test_replay_fixture_through_process(
    runner_dir: Path,
    popen: FakePopen,
    win: FakeWin,
    rec: Recorder,
    fixture: str,
    states: list[tuple[LocalState, str | None]],
    jobs: list[tuple[str, str]],
) -> None:
    popen.data = fixture_bytes(fixture)
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec)
    proc.start()
    assert proc.wait(5) == 0
    assert rec.states == states
    assert rec.jobs == jobs
    assert proc.state == S.STOPPED
    assert proc.exit_code == 0
    assert proc.last_error is None
    assert proc.runner_version == "2.338.0"
    assert not proc.is_running()
    assert proc.pid is None
    assert proc.stopped_at is not None
    assert popen.last.stdout.closed
    expected_lines = [line for line in fixture_lines(fixture) if line.strip()]
    assert proc.output() == expected_lines
    assert rec.lines == expected_lines


def test_callback_order_line_then_job_then_state(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    popen.data = fixture_bytes("job_then_ctrl_break.txt")
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec)
    proc.start()
    proc.wait(5)
    completed = next(
        i for i, e in enumerate(rec.events) if e[0] == "line" and "completed with" in e[1]
    )
    assert rec.events[completed + 1] == ("job", "spike", "Failed")
    assert rec.events[completed + 2] == ("state", S.IDLE, None)
    assert proc.last_job == ("spike", "Failed")


def test_gbk_raw_fixture_through_reader(
    runner_dir: Path,
    popen: FakePopen,
    win: FakeWin,
    rec: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(winproc, "console_codepage_codec", lambda: "gbk")
    popen.data = fixture_bytes("job_then_ctrl_break.cp936.raw")
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec)
    proc.start()
    proc.wait(5)
    out = proc.output()
    assert out[0] == "已复制         1 个文件。"
    assert "√ Connected to GitHub" in out  # GBK check mark decoded via the fallback
    assert "终止批处理操作吗(Y/N)? " in out
    assert [s for s, _ in rec.states] == [S.STARTING, S.IDLE, S.BUSY, S.IDLE, S.STOPPING, S.STOPPED]
    assert rec.jobs == [("spike", "Failed")]


def test_gbk_raw_fixture_without_gbk_fallback_still_tracks_state(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    # Non-Windows fallback is latin-1: mojibake for cmd.exe lines, runner lines unaffected.
    popen.data = fixture_bytes("job_then_ctrl_break.cp936.raw")
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec)
    proc.start()
    proc.wait(5)
    assert [s for s, _ in rec.states][-3:] == [S.IDLE, S.STOPPING, S.STOPPED]


def test_ring_buffer_keeps_last_lines_and_returns_copy(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    popen.data = fixture_bytes("job_then_ctrl_break.txt")
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec, max_lines=3)
    proc.start()
    proc.wait(5)
    out = proc.output()
    assert out == [
        "终止批处理操作吗(Y/N)? ",
        '"Runner listener exit with 0 return code, stop the service, no retry needed."',
        '"Exiting runner..."',
    ]
    out.clear()
    assert len(proc.output()) == 3
    assert len(rec.lines) == 10  # callbacks still see every non-blank line


def test_output_is_kept_across_restarts(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    popen.data = b"first\r\n"
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec)
    proc.start()
    proc.wait(5)
    popen.data = b"second\r\n"
    proc.start()
    proc.wait(5)
    assert proc.output() == ["first", "second"]


def test_max_lines_must_be_positive(runner_dir: Path) -> None:
    with pytest.raises(ValueError):
        RunnerProcess(runner_dir, max_lines=0)


def test_long_line_is_split_not_buffered_forever(
    runner_dir: Path,
    popen: FakePopen,
    win: FakeWin,
    rec: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_mod, "MAX_LINE_BYTES", 8)
    popen.data = b"0123456789abcdef\r\n"
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec)
    proc.start()
    proc.wait(5)
    assert proc.output() == ["01234567", "89abcdef"]


def test_output_lines_are_redacted(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    fake_pat = "ghp_" + "A" * 36
    popen.data = f"config --token NOTAREALTOKEN123\r\nusing {fake_pat}\r\n".encode()
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec)
    proc.start()
    proc.wait(5)
    joined = "\n".join(proc.output() + rec.lines)
    assert "NOTAREALTOKEN123" not in joined
    assert fake_pat not in joined
    assert "--token ***" in proc.output()[0]


def test_listener_crash_is_error_although_run_cmd_exits_0(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    # Spike §5 kill_listener: the Listener died with 15, run.cmd still returned rc=0.
    popen.data = fixture_bytes("conflict_then_kill_listener.txt")
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec)
    proc.start()
    assert proc.wait(5) == 0
    assert proc.state == S.ERROR
    assert proc.exit_code == 0
    assert proc.listener_exit_code == 15
    assert proc.last_error == ('runner 意外退出（Listener 退出码 15）："Exiting runner..."')
    assert rec.states[-1] == (S.ERROR, None)


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ('"Runner listener exit with terminated error, stop the service, no retry needed."', 1),
        (
            SESSION_CONFLICT_LINE,
            5,
        ),
    ],
)
def test_listener_error_exit_lines_are_errors_with_rc_0(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder, line: str, code: int
) -> None:
    # e.g. the registration was deleted on GitHub: run-helper ends with exit /b 0.
    popen.data = IDLE_OUTPUT + line.encode() + b'\r\n"Exiting runner..."\r\n'
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec)
    proc.start()
    assert proc.wait(5) == 0
    assert proc.state == S.ERROR
    assert proc.listener_exit_code == code
    assert proc.last_error is not None
    assert f"Listener 退出码 {code}" in proc.last_error


def test_clean_listener_exit_with_rc_0_is_stopped(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    # Nobody called stop(), e.g. Ctrl+Break from the console the runner shares.
    popen.data = fixture_bytes("job_then_ctrl_break.txt")
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec)
    proc.start()
    proc.wait(5)
    assert proc.listener_exit_code == 0
    assert proc.state == S.STOPPED
    assert proc.last_error is None


RETRY = b'"Runner listener exit with retryable error, re-launch runner in 5 seconds."\r\n'
RESTART = b'"Restarting runner..."\r\n'
CLEAN_EXIT = (
    b'"Runner listener exit with 0 return code, stop the service, no retry needed."\r\n'
    b'"Exiting runner..."\r\n'
)


@pytest.mark.parametrize(
    ("data", "state", "listener"),
    [
        # relaunched Listener later exits cleanly: its line replaces the restart code
        (IDLE_OUTPUT + RETRY + RESTART + IDLE_OUTPUT + CLEAN_EXIT, S.STOPPED, 0),
        # relaunched Listener later crashes
        (
            IDLE_OUTPUT
            + RETRY
            + RESTART
            + IDLE_OUTPUT
            + b'"Exiting after unknown error code: 15"\r\n"Exiting runner..."\r\n',
            S.ERROR,
            15,
        ),
        # run.cmd ended right after a restart code, no Listener running any more
        (IDLE_OUTPUT + RETRY + b'"Exiting runner..."\r\n', S.ERROR, 2),
    ],
)
def test_restart_codes_do_not_end_the_run(
    runner_dir: Path,
    popen: FakePopen,
    win: FakeWin,
    rec: Recorder,
    data: bytes,
    state: LocalState,
    listener: int,
) -> None:
    popen.data = data
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec)
    proc.start()
    assert proc.wait(5) == 0
    assert proc.state == state
    assert proc.listener_exit_code == listener
    assert (proc.last_error is None) == (state == S.STOPPED)


def test_listener_restart_keeps_running(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    proc = started(runner_dir, popen, rec, IDLE_OUTPUT + RETRY + RESTART + IDLE_OUTPUT)
    wait_until(lambda: proc.output()[-1].endswith("Listening for Jobs"))
    assert proc.is_running()
    assert proc.state == S.IDLE
    proc.stop()
    assert proc.state == S.STOPPED


def test_stop_requested_hides_listener_error_code(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    win.console = True
    win.ctrl_break_tail = b'"Exiting after unknown error code: 15"\r\n"Exiting runner..."\r\n'
    proc = started(runner_dir, popen, rec, IDLE_OUTPUT)
    result = proc.stop(timeout=5)
    assert result.graceful
    assert proc.listener_exit_code == 15
    assert proc.state == S.STOPPED
    assert proc.last_error is None


def test_cmd_exe_failure_and_listener_code_both_reported(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    popen.data = b'"Exiting after unknown error code: 15"\r\n'
    popen.exit_code = 1
    proc = make(runner_dir, popen, rec)
    proc.start()
    proc.wait(5)
    assert proc.last_error == (
        'runner 意外退出（退出码 1，Listener 退出码 15）："Exiting after unknown error code: 15"'
    )


def test_error_message_for_ntstatus_exit_code_and_no_output(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    popen.exit_code = 0xC000013A
    proc = make(runner_dir, popen, rec)
    proc.start()
    proc.wait(5)
    assert proc.last_error == "runner 意外退出（退出码 3221225786 (0xC000013A)）"


def test_restart_resets_run_state(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    # cmd.exe itself failing (e.g. killed from Task Manager) is an error too.
    popen.data = b'"Exiting after unknown error code: 15"\r\n'
    popen.exit_code = 2
    proc = make(runner_dir, popen, rec)
    proc.start()
    proc.wait(5)
    assert proc.state == S.ERROR
    assert proc.listener_exit_code == 15
    popen.exit_code = None
    popen.data = b"2026-10-22 08:09:45Z: Listening for Jobs\r\n"
    proc.start()
    wait_until(lambda: proc.state == S.IDLE)
    assert proc.last_error is None
    assert proc.exit_code is None
    assert proc.listener_exit_code is None
    assert proc.stopped_at is None
    proc.stop()


def test_callback_exceptions_do_not_break_tracking(
    runner_dir: Path, popen: FakePopen, win: FakeWin, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*args: Any) -> None:
        raise RuntimeError("callback bug")

    popen.data = fixture_bytes("job_then_ctrl_break.txt")
    popen.exit_code = 0
    proc = RunnerProcess(runner_dir, popen=popen, on_line=boom, on_state=boom, on_job_finished=boom)
    proc.start()
    proc.wait(5)
    assert proc.state == S.STOPPED
    assert proc.last_job == ("spike", "Failed")
    assert "callback failed" in caplog.text


def test_no_callbacks_is_fine(runner_dir: Path, popen: FakePopen, win: FakeWin) -> None:
    popen.data = fixture_bytes("job_then_ctrl_break.txt")
    popen.exit_code = 0
    proc = RunnerProcess(runner_dir, popen=popen)
    proc.start()
    proc.wait(5)
    assert proc.state == S.STOPPED
    assert len(proc.output()) == 10


def test_reader_survives_broken_pipe(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec)

    class BrokenPipe:
        def readline(self, size: int = -1) -> bytes:
            raise OSError("pipe broken")

        def close(self) -> None:
            raise OSError("already closed")

    original = popen.__call__

    def broken_popen(args: list[str], **kwargs: Any) -> FakeProc:
        fake = original(args, **kwargs)
        fake.stdout = BrokenPipe()  # type: ignore[assignment]
        return fake

    proc._popen = broken_popen
    proc.start()
    assert proc.wait(5) == 0
    assert proc.state == S.STOPPED


# --- start() refusals ----------------------------------------------------------------------


def test_start_refuses_when_already_running(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    proc = make(runner_dir, popen, rec)
    proc.start()
    try:
        with pytest.raises(ProcessError, match="已在运行"):
            proc.start()
        assert len(popen.procs) == 1
    finally:
        proc.stop()


def test_start_refuses_when_external_runner_processes_exist(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    win.runner_procs = [FakePsProc(777, "Runner.Listener.exe")]
    proc = make(runner_dir, popen, rec)
    with pytest.raises(ProcessError, match="777"):
        proc.start()
    assert popen.procs == []
    assert proc.state == S.STOPPED
    assert not proc.is_running()
    assert rec.events == []


def test_start_reports_process_scan_failure(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder, monkeypatch: Any
) -> None:
    def denied(runner_dir: Path) -> list[Any]:
        raise psutil.AccessDenied(1)

    monkeypatch.setattr(winproc, "find_runner_processes", denied)
    with pytest.raises(ProcessError, match="无法检查"):
        make(runner_dir, popen, rec).start()


def test_start_requires_run_cmd(tmp_path: Path, popen: FakePopen, win: FakeWin) -> None:
    proc = RunnerProcess(tmp_path, popen=popen)
    with pytest.raises(ProcessError, match="run.cmd"):
        proc.start()
    assert popen.procs == []


def test_start_wraps_popen_failure(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    popen.error = FileNotFoundError(2, "cmd.exe not found")
    proc = make(runner_dir, popen, rec)
    with pytest.raises(ProcessError, match="无法启动") as exc_info:
        proc.start()
    assert isinstance(exc_info.value.__cause__, FileNotFoundError)
    assert proc.state == S.STOPPED
    assert not proc.is_running()


def test_start_cleans_up_when_reader_thread_cannot_start(
    runner_dir: Path,
    popen: FakePopen,
    win: FakeWin,
    rec: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoThread(threading.Thread):
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(process_mod.threading, "Thread", NoThread)
    proc = make(runner_dir, popen, rec)
    with pytest.raises(ProcessError, match="读取线程"):
        proc.start()
    assert win.called("kill_tree") == [popen.last.pid]
    assert proc.state == S.ERROR
    assert not proc.is_running()


# --- stop() ----------------------------------------------------------------------------------


def started(
    runner_dir: Path, popen: FakePopen, rec: Recorder, data: bytes = b"", **kw: Any
) -> RunnerProcess:
    popen.data = data
    proc = make(runner_dir, popen, rec, **kw)
    proc.start()
    return proc


def test_stop_graceful_ctrl_break(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    win.console = True
    proc = started(runner_dir, popen, rec, IDLE_OUTPUT)
    wait_until(lambda: proc.state == S.IDLE)
    result = proc.stop(timeout=5)
    assert result == StopResult(True, StopMethod.CTRL_BREAK, 0, [])
    assert win.called("ctrl_break") == [popen.last.pid]
    assert win.called("kill_tree") == []
    assert proc.state == S.STOPPED
    assert not proc.last_stop_forced
    assert [s for s, _ in rec.states] == [S.STARTING, S.IDLE, S.STOPPING, S.STOPPED]
    assert proc.output()[-1] == '"Exiting runner..."'


def test_stop_graceful_cleans_leftover_runner_processes(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    win.console = True
    proc = started(runner_dir, popen, rec, IDLE_OUTPUT)
    worker = FakePsProc(888, "Runner.Worker.exe")
    win.runner_procs = [worker]
    result = proc.stop(timeout=5)
    assert result.graceful is True
    assert result.killed_pids == [888]


def test_kill_noted_during_graceful_wait_is_reported_as_forced(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    # The CLI's 2nd Ctrl+C: stop() is blocked waiting for Ctrl+Break while another thread
    # calls note_forced_kill() and kills the tree. The exit must not count as graceful.
    win.console = True
    win.ctrl_break_tail = None  # Ctrl+Break is ignored, so only the kill ends the wait
    proc = started(runner_dir, popen, rec, IDLE_OUTPUT)
    wait_until(lambda: proc.state == S.IDLE)
    pid = popen.last.pid

    def force() -> None:
        proc.note_forced_kill()
        win.kill_tree_report(pid)

    threading.Timer(0.1, force).start()
    result = proc.stop(timeout=10)
    assert result.graceful is False
    assert result.method == StopMethod.KILL
    assert proc.last_stop_forced
    assert proc.state == S.STOPPED
    assert proc.stop() == result  # the recorded stop is the forced one


def test_note_forced_kill_when_not_running_is_a_noop(
    runner_dir: Path, popen: FakePopen, rec: Recorder
) -> None:
    proc = make(runner_dir, popen, rec)
    proc.note_forced_kill()
    assert not proc.last_stop_forced


def test_stop_graceful_timeout_falls_back_to_kill(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    win.console = True
    win.ctrl_break_tail = None  # Ctrl+Break never arrives (e.g. CREATE_NO_WINDOW child)
    proc = started(runner_dir, popen, rec, IDLE_OUTPUT)
    t0 = time.monotonic()
    result = proc.stop(timeout=0.2)
    assert time.monotonic() - t0 >= 0.15  # waited for Ctrl+Break (Windows clock is coarse)
    pid = popen.last.pid
    assert result == StopResult(False, StopMethod.KILL, 1, [pid + 1, pid])
    assert win.called("ctrl_break") == [pid]
    assert win.called("kill_tree") == [pid]
    assert proc.state == S.STOPPED  # exit code 1 after our kill is not an error
    assert proc.last_error is None
    assert proc.last_stop_forced


def test_stop_without_console_kills_and_cleans_leftovers(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    win.console = False
    proc = started(runner_dir, popen, rec, IDLE_OUTPUT)
    wait_until(lambda: proc.state == S.IDLE)
    orphan = FakePsProc(999, "Runner.Worker.exe")
    win.runner_procs = [orphan]
    result = proc.stop()
    pid = popen.last.pid
    assert result.graceful is False
    assert result.method == StopMethod.KILL
    assert result.killed_pids == [pid + 1, pid, 999]
    assert win.called("ctrl_break") == []
    assert win.called("kill_processes") == [[999]]
    assert proc.state == S.STOPPED
    assert rec.states[-2:] == [(S.STOPPING, None), (S.STOPPED, None)]


def test_stop_when_ctrl_break_fails_kills(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    win.console = True
    win.ctrl_break_error = ProcessError("无法发送")
    proc = started(runner_dir, popen, rec)
    result = proc.stop()
    assert result.graceful is False
    assert win.called("kill_tree") == [popen.last.pid]


def test_stop_survives_kill_helper_errors(
    runner_dir: Path,
    popen: FakePopen,
    win: FakeWin,
    rec: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = started(runner_dir, popen, rec)
    fake = popen.last

    def kill_tree_fails(pid: int, *, timeout: float = 5.0) -> tuple[list[int], list[int]]:
        fake.finish(1)  # the process still dies (e.g. killed by someone else)
        raise psutil.AccessDenied(pid)

    def scan_fails(runner_dir: Path) -> list[Any]:
        raise psutil.AccessDenied(0)

    monkeypatch.setattr(winproc, "kill_tree_report", kill_tree_fails)
    monkeypatch.setattr(winproc, "find_runner_processes", scan_fails)
    result = proc.stop()
    assert result.graceful is False
    assert result.killed_pids == []
    assert proc.state == S.STOPPED


def test_stop_raises_if_process_survives_kill(
    runner_dir: Path,
    popen: FakePopen,
    win: FakeWin,
    rec: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_mod, "KILL_WAIT_S", 0.05)
    win.kill_works = False
    proc = started(runner_dir, popen, rec)
    with pytest.raises(ProcessError, match=f"无法结束 runner 进程（PID {popen.last.pid}）"):
        proc.stop()
    assert proc.is_running()
    assert proc.state == S.STOPPING
    assert proc.last_error is not None
    # a later stop() can still finish the job
    win.kill_works = True
    result = proc.stop()
    assert result.graceful is False
    assert proc.state == S.STOPPED


@pytest.mark.parametrize("console", [False, True])
def test_stop_raises_if_runner_processes_survive(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder, console: bool
) -> None:
    # run.cmd is gone (killed, or ended by Ctrl+Break) but a Runner.* process cannot be killed.
    win.console = console
    proc = started(runner_dir, popen, rec, IDLE_OUTPUT)
    worker = FakePsProc(999, "Runner.Worker.exe")
    win.runner_procs = [worker]
    win.unkillable = {999}
    with pytest.raises(ProcessError, match=r"无法结束 runner 进程（PID 999）"):
        proc.stop(timeout=5)
    assert win.called("kill_processes") == [[999]]
    assert not proc.is_running()  # the run itself is over
    assert proc.state == S.ERROR
    assert proc.last_error == "无法结束 runner 进程（PID 999）"
    assert rec.states[-1] == (S.ERROR, None)


def test_stop_reports_survivor_when_kill_wait_is_denied(
    runner_dir: Path,
    popen: FakePopen,
    win: FakeWin,
    rec: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # psutil's wait raises AccessDenied on Windows for a process we may not open.
    proc = started(runner_dir, popen, rec)
    win.runner_procs = [FakePsProc(998, "Runner.Listener.exe")]

    def denied(procs: list[Any], *, timeout: float = 5.0) -> tuple[list[int], list[int]]:
        raise psutil.AccessDenied(998)

    monkeypatch.setattr(winproc, "kill_processes_report", denied)
    with pytest.raises(ProcessError, match="998"):
        proc.stop()
    assert proc.state == S.ERROR


def test_stop_kills_leftovers_that_survived_the_tree_kill(
    runner_dir: Path,
    popen: FakePopen,
    win: FakeWin,
    rec: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = started(runner_dir, popen, rec)
    fake = popen.last
    listener = FakePsProc(997, "Runner.Listener.exe")
    win.runner_procs = [listener]

    def tree_leaves_listener(pid: int, *, timeout: float = 5.0) -> tuple[list[int], list[int]]:
        fake.finish(1)
        return [pid], [997]

    monkeypatch.setattr(winproc, "kill_tree_report", tree_leaves_listener)
    result = proc.stop()
    assert result.killed_pids == [fake.pid, 997]
    assert proc.state == S.STOPPED


def test_stop_when_not_running(runner_dir: Path, popen: FakePopen, win: FakeWin) -> None:
    proc = RunnerProcess(runner_dir, popen=popen)
    assert proc.stop() == StopResult(True, StopMethod.CTRL_BREAK, None, [])
    assert proc.wait() is None
    assert win.calls == []


def test_stop_after_unexpected_exit_is_noop(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    popen.data = fixture_bytes("conflict_then_kill_listener.txt")
    popen.exit_code = 0
    proc = make(runner_dir, popen, rec)
    proc.start()
    proc.wait(5)
    assert proc.stop() == StopResult(True, StopMethod.CTRL_BREAK, None, [])
    assert proc.state == S.ERROR  # stop() of a dead process does not hide the error


def test_stop_is_idempotent(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    proc = started(runner_dir, popen, rec)
    first = proc.stop()
    second = proc.stop()
    assert second is first
    assert len(win.called("kill_tree")) == 1
    assert rec.states.count((S.STOPPED, None)) == 1


def test_concurrent_stops_stop_once(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    win.console = True
    proc = started(runner_dir, popen, rec, IDLE_OUTPUT)
    results: list[StopResult] = []
    threads = [threading.Thread(target=lambda: results.append(proc.stop(5))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(results) == 4
    assert all(r == results[0] for r in results)
    assert results[0].graceful
    assert len(win.called("ctrl_break")) == 1
    assert proc.state == S.STOPPED


def test_stop_during_starting_keeps_stopping_until_exit(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    # Ctrl+Break lands while the runner is still connecting; the listener prints
    # "Listening for Jobs" before it notices. That must not flip the state back to IDLE.
    win.console = True
    win.ctrl_break_tail = b"2026-10-22 08:09:45Z: Listening for Jobs\r\nExiting...\r\n"
    proc = started(runner_dir, popen, rec, "已复制         1 个文件。\r\n".encode("gbk"))
    assert proc.state == S.STARTING
    result = proc.stop(timeout=5)
    assert result.graceful
    assert [s for s, _ in rec.states] == [S.STARTING, S.STOPPING, S.STOPPED]


def test_stop_while_reader_is_mid_line_in_callback(
    runner_dir: Path, popen: FakePopen, win: FakeWin
) -> None:
    """stop() from another thread must not block on a slow callback or on a partial line."""
    win.console = True
    entered = threading.Event()
    release = threading.Event()
    seen: list[Any] = []

    def slow_on_line(line: str) -> None:
        seen.append(("line", line))
        if "Listening" in line:
            entered.set()
            release.wait(5)

    def on_state(state: LocalState, job: str | None) -> None:
        seen.append(("state", state, job))

    popen.data = b"2026-10-22 08:09:45Z: Listening for Jobs\r\n2026-10-22 08:09:53Z: Runn"
    proc = RunnerProcess(runner_dir, popen=popen, on_line=slow_on_line, on_state=on_state)
    proc.start()
    assert entered.wait(5)
    # The reader thread is stuck inside on_line; state reads must not block.
    assert proc.state == S.IDLE
    win.ctrl_break_tail = b"ing job: late\r\nExiting...\r\n"
    threading.Timer(0.2, release.set).start()
    result = proc.stop(timeout=5)
    assert result.graceful
    assert proc.state == S.STOPPED
    states = [e[1:] for e in seen if e[0] == "state"]
    # A job announced after Ctrl+Break updates current_job but the state stays STOPPING.
    assert states == [
        (S.STARTING, None),
        (S.IDLE, None),
        (S.STOPPING, None),
        (S.STOPPING, "late"),
        (S.STOPPED, None),
    ]
    assert proc.current_job is None
    # partial line completed by the tail and delivered in order
    assert ("line", "2026-10-22 08:09:53Z: Running job: late") in seen


def test_stop_finalizes_when_pipe_stays_open(
    runner_dir: Path,
    popen: FakePopen,
    win: FakeWin,
    rec: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An orphaned grandchild can keep the stdout pipe open after cmd.exe is gone.
    monkeypatch.setattr(process_mod, "READER_JOIN_TIMEOUT_S", 0.1)
    proc = started(runner_dir, popen, rec, IDLE_OUTPUT)
    fake = popen.last

    def kill_tree_pipe_stays_open(pid: int, *, timeout: float = 5.0) -> tuple[list[int], list[int]]:
        fake.finish(1, close_pipe=False)
        return [pid], []

    monkeypatch.setattr(winproc, "kill_tree_report", kill_tree_pipe_stays_open)
    result = proc.stop()
    assert result.graceful is False
    assert proc.state == S.STOPPED
    assert not proc.is_running()
    # late output of the finished run is buffered but cannot change the state any more
    fake.stdout.write(b"2026-10-22 08:09:53Z: Running job: ghost\r\n")
    fake.stdout.close_writer()
    wait_until(lambda: "Running job: ghost" in proc.output()[-1])
    assert proc.state == S.STOPPED
    assert proc.current_job is None


def test_late_output_from_previous_run_is_dropped(
    runner_dir: Path,
    popen: FakePopen,
    win: FakeWin,
    rec: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_mod, "READER_JOIN_TIMEOUT_S", 0.1)
    proc = started(runner_dir, popen, rec)
    old = popen.last
    monkeypatch.setattr(
        winproc,
        "kill_tree_report",
        lambda pid, timeout=5.0: (old.finish(1, close_pipe=False), ([pid], []))[1],
    )
    proc.stop()
    old_reader = proc._reader
    assert old_reader is not None and old_reader.is_alive()  # still blocked on the open pipe
    monkeypatch.setattr(winproc, "kill_tree_report", win.kill_tree_report)
    proc.start()
    old.stdout.write(b"stale line from old run\r\n")
    old.stdout.close_writer()
    old_reader.join(5)
    assert not old_reader.is_alive()
    assert "stale line from old run" not in proc.output()
    assert proc.state == S.STARTING
    proc.stop()


def test_callbacks_must_not_control_the_process(
    runner_dir: Path, popen: FakePopen, win: FakeWin, caplog: pytest.LogCaptureFixture
) -> None:
    errors: list[BaseException] = []
    reads: list[Any] = []
    holder: dict[str, RunnerProcess] = {}

    def on_state(state: LocalState, job: str | None) -> None:
        for call in (holder["p"].stop, holder["p"].wait, holder["p"].start):
            try:
                call()
            except RuntimeError as e:
                errors.append(e)
        # reads are allowed inside callbacks (the state may already be newer than `state`)
        p = holder["p"]
        reads.append((p.state, p.current_job, p.output(), p.is_running()))

    popen.exit_code = 0
    proc = RunnerProcess(runner_dir, popen=popen, on_state=on_state)
    holder["p"] = proc
    proc.start()
    proc.wait(5)
    assert len(errors) == 6  # 3 calls x (STARTING, STOPPED)
    assert all("callbacks" in str(e) for e in errors)
    assert len(reads) == 2
    assert proc.state == S.STOPPED
    assert len(popen.procs) == 1


def test_wait_timeout_returns_none(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    proc = started(runner_dir, popen, rec)
    assert proc.wait(0.05) is None
    assert proc.is_running()
    popen.last.finish(0)
    assert proc.wait(5) == 0
    assert proc.wait() == 0  # already finished: returns the stored exit code


def test_properties_track_state_since(
    runner_dir: Path, popen: FakePopen, win: FakeWin, rec: Recorder
) -> None:
    proc = RunnerProcess(runner_dir, popen=popen)
    created = proc.state_since
    assert proc.state == S.STOPPED
    assert proc.runner_dir == runner_dir
    proc.start()
    assert proc.state_since >= created
    proc.stop()
    assert proc.stopped_at is not None
    assert proc.state_since >= proc.started_at  # type: ignore[operator]


# --- running_processes / stop_external --------------------------------------------------------


def test_running_processes_delegates(win: FakeWin, runner_dir: Path) -> None:
    listener = FakePsProc(10, "Runner.Listener.exe")
    win.runner_procs = [listener]
    assert running_processes(runner_dir) == [listener]


def test_stop_external_nothing_running(win: FakeWin, runner_dir: Path) -> None:
    result = stop_external(runner_dir)
    assert result == ExternalStop([], [])
    assert result.ok
    assert win.called("kill_tree") == []


def test_stop_external_scan_failure_is_an_error(
    win: FakeWin, runner_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(runner_dir: Path) -> list[Any]:
        raise psutil.AccessDenied(0)

    monkeypatch.setattr(winproc, "find_runner_processes", denied)
    with pytest.raises(ProcessError, match="无法检查"):
        stop_external(runner_dir)


def _external_tree(
    run_cmdline: list[str], helper_cmdline: list[str]
) -> tuple[FakePsProc, FakePsProc, FakePsProc, FakePsProc, FakePsProc]:
    explorer = FakePsProc(100, "explorer.exe")
    run = FakePsProc(101, "cmd.exe", run_cmdline, explorer)
    helper = FakePsProc(102, "cmd.exe", helper_cmdline, run)
    listener = FakePsProc(103, "Runner.Listener.exe", parent=helper)
    worker = FakePsProc(104, "Runner.Worker.exe", parent=listener)
    return explorer, run, helper, listener, worker


@pytest.fixture
def no_lineage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_mod, "_own_lineage", lambda: {os.getpid()})


def test_stop_external_kills_launching_cmd_chain(
    win: FakeWin, runner_dir: Path, no_lineage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run, helper, listener, worker = _external_tree(
        ["C:\\WINDOWS\\system32\\cmd.exe", "/c", "D:\\runners\\x\\run.cmd"],
        ["C:\\WINDOWS\\system32\\cmd.exe", "/c", '"D:\\runners\\x\\run-helper.cmd"'],
    )
    win.runner_procs = [listener, worker]
    trees = {101: [102, 103, 104, 101]}

    def kill_tree(pid: int, *, timeout: float = 5.0) -> tuple[list[int], list[int]]:
        win.calls.append(("kill_tree", pid))
        win.runner_procs = []
        return trees[pid], []

    monkeypatch.setattr(winproc, "kill_tree_report", kill_tree)
    assert stop_external(runner_dir) == ExternalStop([102, 103, 104, 101], [])
    assert win.called("kill_tree") == [101]  # the worker is covered by the listener's tree
    assert win.called("kill_processes") == []  # nothing left over


def test_stop_external_reports_surviving_cmd_shell(
    win: FakeWin, runner_dir: Path, no_lineage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run, helper, listener, worker = _external_tree(
        ["cmd.exe", "/c", "run.cmd"], ["cmd.exe", "/c", "run-helper.cmd"]
    )
    win.runner_procs = [listener, worker]

    def kill_tree(pid: int, *, timeout: float = 5.0) -> tuple[list[int], list[int]]:
        win.calls.append(("kill_tree", pid))
        win.runner_procs = []
        return [102, 103, 104], [101, 555]  # 555: some job process, not ours to report

    monkeypatch.setattr(winproc, "kill_tree_report", kill_tree)
    result = stop_external(runner_dir)
    assert result == ExternalStop([102, 103, 104], [101])
    assert not result.ok


def test_stop_external_does_not_kill_unrelated_shell(
    win: FakeWin, runner_dir: Path, no_lineage: None
) -> None:
    # run.cmd typed into an interactive cmd window: that window is not ours to close.
    _, _, helper, listener, _ = _external_tree(
        ["C:\\WINDOWS\\system32\\cmd.exe"],
        ["cmd.exe", "/c", "run-helper.cmd"],
    )
    win.runner_procs = [listener]
    stop_external(runner_dir)
    assert win.called("kill_tree") == [helper.pid]


@pytest.mark.parametrize(
    "cmdline",
    [
        ["cmd.exe", "/c", "dry-run.cmd"],
        ["cmd.exe", "/c", "myrun.cmd"],
        ["cmd.exe", "/c", "run.cmd.bak"],
        ["cmd.exe", "/k"],
    ],
)
def test_stop_external_parent_cmdline_must_reference_run_scripts(
    win: FakeWin, runner_dir: Path, no_lineage: None, cmdline: list[str]
) -> None:
    shell = FakePsProc(200, "cmd.exe", cmdline)
    listener = FakePsProc(201, "Runner.Listener.exe", parent=shell)
    win.runner_procs = [listener]
    stop_external(runner_dir)
    assert win.called("kill_tree") == [201]


def test_stop_external_skips_parent_it_cannot_inspect(
    win: FakeWin, runner_dir: Path, no_lineage: None
) -> None:
    shell = FakePsProc(300, "cmd.exe", ["cmd.exe", "/c", "run.cmd"], deny=True)
    listener = FakePsProc(301, "Runner.Listener.exe", parent=shell)
    win.runner_procs = [listener]
    stop_external(runner_dir)
    assert win.called("kill_tree") == [301]


def test_stop_external_requires_cmd_exe_parent(
    win: FakeWin, runner_dir: Path, no_lineage: None
) -> None:
    service = FakePsProc(400, "RunnerService.exe", ["RunnerService.exe", "run.cmd"])
    listener = FakePsProc(401, "Runner.Listener.exe", parent=service)
    win.runner_procs = [listener]
    stop_external(runner_dir)
    assert win.called("kill_tree") == [401]


def test_stop_external_never_kills_own_lineage(
    win: FakeWin, runner_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = FakePsProc(500, "cmd.exe", ["cmd.exe", "/c", "run.cmd"])
    listener = FakePsProc(501, "Runner.Listener.exe", parent=shell)
    win.runner_procs = [listener]
    monkeypatch.setattr(process_mod, "_own_lineage", lambda: {500})
    assert stop_external(runner_dir) == ExternalStop([501], [])
    assert win.called("kill_tree") == []
    assert win.called("kill_processes") == [[501]]


def test_stop_external_protected_runner_process_is_a_survivor(
    win: FakeWin, runner_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # e.g. `runnerdeck-cli stop` run by a job on that very runner: never kill our own ancestors,
    # but do not claim the runner was stopped either.
    listener = FakePsProc(510, "Runner.Listener.exe")
    worker = FakePsProc(511, "Runner.Worker.exe", parent=listener)
    win.runner_procs = [listener, worker]
    monkeypatch.setattr(process_mod, "_own_lineage", lambda: {510, 511})
    result = stop_external(runner_dir)
    assert result == ExternalStop([], [510, 511])
    assert win.called("kill_tree") == []
    assert win.called("kill_processes") == []


def _deny_kill_tree(win: FakeWin, monkeypatch: pytest.MonkeyPatch) -> None:
    def kill_tree_denied(pid: int, *, timeout: float = 5.0) -> tuple[list[int], list[int]]:
        win.calls.append(("kill_tree", pid))
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(winproc, "kill_tree_report", kill_tree_denied)


def test_stop_external_cleans_leftovers_after_tree_kill_error(
    win: FakeWin, runner_dir: Path, no_lineage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    listener = FakePsProc(601, "Runner.Listener.exe")
    win.runner_procs = [listener]
    _deny_kill_tree(win, monkeypatch)
    assert stop_external(runner_dir) == ExternalStop([601], [])
    assert win.called("kill_processes") == [[601]]


def test_stop_external_reports_survivors_when_every_kill_is_denied(
    win: FakeWin, runner_dir: Path, no_lineage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # e.g. the runner was started from an elevated terminal: it is still online.
    listener = FakePsProc(601, "Runner.Listener.exe")
    win.runner_procs = [listener]
    win.unkillable = {601}
    _deny_kill_tree(win, monkeypatch)
    result = stop_external(runner_dir)
    assert result == ExternalStop([], [601])
    assert not result.ok


def test_stop_external_partial_kill_reports_the_rest(
    win: FakeWin, runner_dir: Path, no_lineage: None
) -> None:
    a = FakePsProc(611, "Runner.Listener.exe")
    b = FakePsProc(612, "Runner.Listener.exe")
    win.runner_procs = [a, b]
    win.unkillable = {612}
    # kill_tree of a pid that is not one of FakePopen's "works" but the rescan still sees both
    result = stop_external(runner_dir)
    assert result.survivors == [612]
    assert 611 in result.killed
    assert 612 not in result.killed


def test_stop_external_wait_denied_is_a_survivor(
    win: FakeWin, runner_dir: Path, no_lineage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On Windows psutil's wait raises AccessDenied for a process we may not open.
    listener = FakePsProc(621, "Runner.Listener.exe")
    win.runner_procs = [listener]
    _deny_kill_tree(win, monkeypatch)

    def wait_denied(procs: list[Any], *, timeout: float = 5.0) -> tuple[list[int], list[int]]:
        raise psutil.AccessDenied(621)

    monkeypatch.setattr(winproc, "kill_processes_report", wait_denied)
    assert stop_external(runner_dir) == ExternalStop([], [621])


def test_stop_external_rescan_failure_checks_the_first_scan(
    win: FakeWin, runner_dir: Path, no_lineage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    listener = FakePsProc(631, "Runner.Listener.exe")
    gone = FakePsProc(632, "Runner.Listener.exe")
    calls = {"n": 0}

    def scan_then_fail(runner_dir: Path) -> list[Any]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise psutil.AccessDenied(0)
        return [listener, gone]

    def kill_tree(pid: int, *, timeout: float = 5.0) -> tuple[list[int], list[int]]:
        win.calls.append(("kill_tree", pid))
        if pid == 632:
            gone.alive = False
            return [632], []
        return [], [631]

    monkeypatch.setattr(winproc, "find_runner_processes", scan_then_fail)
    monkeypatch.setattr(winproc, "kill_tree_report", kill_tree)
    win.unkillable = {631}
    # cannot rescan: what survives is judged from the processes found the first time
    assert stop_external(runner_dir) == ExternalStop([632], [631])
    assert win.called("kill_processes") == [[631]]


def test_own_lineage_contains_this_process() -> None:
    lineage = process_mod._own_lineage()
    assert os.getpid() in lineage
    assert os.getppid() in lineage


# --- Windows only: real cmd.exe + fake listener ----------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def _python_for_fake_runner() -> str:
    # Prefer the base interpreter: a venv's python.exe is a launcher that adds a process level.
    base = getattr(sys, "_base_executable", "") or ""
    return base if base and Path(base).is_file() else sys.executable


def make_fake_runner_dir(tmp_path: Path, mode: str) -> tuple[Path, Path]:
    runner_dir = tmp_path / "fake-runner"
    runner_dir.mkdir()
    pid_file = tmp_path / "listener.pid"
    template = (FAKE_RUNNER / "run.cmd.template").read_text(encoding="utf-8")
    script = (
        template.replace("@PYTHON@", _python_for_fake_runner())
        .replace("@LISTENER@", str(FAKE_RUNNER / "fake_listener.py"))
        .replace("@MODE@", mode)
        .replace("@PIDFILE@", str(pid_file))
    )
    (runner_dir / "run.cmd").write_bytes("\r\n".join(script.splitlines()).encode("ascii") + b"\r\n")
    return runner_dir, pid_file


def listener_pid(pid_file: Path) -> int:
    wait_until(lambda: pid_file.exists() and pid_file.read_text().strip() != "", 20)
    return int(pid_file.read_text())


@pytest.fixture
def real_processes() -> Any:
    procs: list[RunnerProcess] = []
    yield procs
    for proc in procs:
        if proc.is_running():
            pid = proc.pid
            if pid is not None:
                winproc.kill_tree(pid)
            proc.wait(10)


@windows_only
def test_windows_start_reaches_idle_then_forced_stop(tmp_path: Path, real_processes: Any) -> None:
    runner_dir, pid_file = make_fake_runner_dir(tmp_path, "idle")
    rec = Recorder()
    proc = RunnerProcess(runner_dir, **rec.callbacks())
    real_processes.append(proc)
    proc.start()
    wait_until(lambda: proc.state == S.IDLE, 30)
    assert proc.runner_version == "2.338.0"
    assert "\u221a Connected to GitHub" in proc.output()
    child = listener_pid(pid_file)
    assert psutil.pid_exists(child)

    cmd_pid = proc.pid
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(winproc, "has_console", lambda: False)
        result = proc.stop()
    assert result.graceful is False
    assert result.method == StopMethod.KILL
    assert cmd_pid in result.killed_pids
    assert child in result.killed_pids
    assert proc.state == S.STOPPED
    wait_until(lambda: not psutil.pid_exists(child), 10)  # no orphaned listener
    assert [s for s, _ in rec.states][:2] == [S.STARTING, S.IDLE]
    assert rec.states[-1] == (S.STOPPED, None)


@windows_only
def test_windows_graceful_ctrl_break(tmp_path: Path, real_processes: Any) -> None:
    if not winproc.has_console():
        pytest.skip("test process has no console: Ctrl+Break cannot reach the child")
    runner_dir, pid_file = make_fake_runner_dir(tmp_path, "idle")
    proc = RunnerProcess(runner_dir)
    real_processes.append(proc)
    proc.start()
    wait_until(lambda: proc.state == S.IDLE, 30)
    child = listener_pid(pid_file)
    result = proc.stop(timeout=15)
    assert result.graceful is True
    assert result.method == StopMethod.CTRL_BREAK
    assert result.exit_code == 0
    assert proc.state == S.STOPPED
    out = proc.output()
    assert "Exiting..." in out
    assert '"Runner listener exit with 0 return code, stop the service, no retry needed."' in out
    assert out[-1] == '"Exiting runner..."'
    assert proc.listener_exit_code == 0
    wait_until(lambda: not psutil.pid_exists(child), 10)


@windows_only
def test_windows_unexpected_exit_is_error(tmp_path: Path, real_processes: Any) -> None:
    # Like the real run.cmd, the fake one returns 0 after the Listener died with 15 (spike §5).
    runner_dir, _ = make_fake_runner_dir(tmp_path, "crash")
    proc = RunnerProcess(runner_dir)
    real_processes.append(proc)
    proc.start()
    assert proc.wait(30) == 0
    assert proc.state == S.ERROR
    assert proc.listener_exit_code == 15
    assert proc.last_error == 'runner 意外退出（Listener 退出码 15）："Exiting runner..."'
    assert proc.output()[-2:] == ['"Exiting after unknown error code: 15"', '"Exiting runner..."']
    assert proc.stop().exit_code is None  # nothing left to stop


@windows_only
def test_windows_terminated_error_is_error(tmp_path: Path, real_processes: Any) -> None:
    # Listener code 1, e.g. its registration was deleted on GitHub.
    runner_dir, _ = make_fake_runner_dir(tmp_path, "terminated")
    proc = RunnerProcess(runner_dir)
    real_processes.append(proc)
    proc.start()
    assert proc.wait(30) == 0
    assert proc.state == S.ERROR
    assert proc.listener_exit_code == 1
    assert proc.last_error is not None
    assert "Listener 退出码 1" in proc.last_error


@windows_only
def test_windows_retryable_exit_relaunches_listener(tmp_path: Path, real_processes: Any) -> None:
    runner_dir, pid_file = make_fake_runner_dir(tmp_path, "retry")
    proc = RunnerProcess(runner_dir)
    real_processes.append(proc)
    proc.start()
    wait_until(lambda: '"Restarting runner..."' in proc.output(), 30)
    wait_until(lambda: proc.output()[-1].endswith("Listening for Jobs"), 30)
    assert proc.is_running()
    assert proc.state == S.IDLE
    child = listener_pid(pid_file)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(winproc, "has_console", lambda: False)
        proc.stop()
    assert proc.state == S.STOPPED
    assert proc.last_error is None
    wait_until(lambda: not psutil.pid_exists(child), 10)


@windows_only
def test_windows_start_twice_refused(tmp_path: Path, real_processes: Any) -> None:
    runner_dir, _ = make_fake_runner_dir(tmp_path, "idle")
    proc = RunnerProcess(runner_dir)
    real_processes.append(proc)
    proc.start()
    with pytest.raises(ProcessError):
        proc.start()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(winproc, "has_console", lambda: False)
        proc.stop()
    assert not proc.is_running()
