"""Tests for runnerdeck.core.console_helper (the GUI's Ctrl+Break relay).

The stdin protocol runs against a fake child on every platform; the tests at the bottom start
the real helper around the fake runner from tests/core/fake_runner and only run on Windows.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil
import pytest

from runnerdeck.core import console_helper, winproc
from runnerdeck.core.console_helper import STOP_COMMAND, main, run

FAKE_RUNNER = Path(__file__).resolve().parent / "fake_runner"


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.005)


class BlockingStdin:
    """Byte stream whose readline blocks until a line or EOF arrives from the test."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._eof = False
        self._cond = threading.Condition()

    def feed(self, data: bytes) -> None:
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
                if idx >= 0:
                    line = bytes(self._buf[: idx + 1])
                    del self._buf[: idx + 1]
                    return line
                if self._eof:
                    line = bytes(self._buf)
                    self._buf.clear()
                    return line
                self._cond.wait()


class FakeChild:
    def __init__(self, args: list[str], kwargs: dict[str, Any]) -> None:
        self.args = args
        self.kwargs = kwargs
        self.pid = 777
        self.returncode: int | None = None
        self._exited = threading.Event()

    def finish(self, rc: int) -> None:
        self.returncode = rc
        self._exited.set()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired(self.args, timeout or 0)
        assert self.returncode is not None
        return self.returncode


class Harness:
    def __init__(self) -> None:
        self.child: FakeChild | None = None
        self.breaks: list[int] = []
        self.kills: list[int] = []
        self.stdin = BlockingStdin()
        self.break_error: OSError | None = None

    def popen(self, args: list[str], **kwargs: Any) -> FakeChild:
        self.child = FakeChild(args, kwargs)
        return self.child

    def send_break(self, pid: int) -> None:
        self.breaks.append(pid)
        if self.break_error is not None:
            raise self.break_error
        assert self.child is not None
        threading.Timer(0.02, self.child.finish, args=(0,)).start()

    def kill(self, pid: int) -> None:
        self.kills.append(pid)
        assert self.child is not None
        self.child.finish(1)

    def run_in_thread(self, **kw: Any) -> tuple[threading.Thread, list[int]]:
        result: list[int] = []

        def target() -> None:
            result.append(
                run(
                    "D:\\runners\\demo",
                    stdin=self.stdin,  # type: ignore[arg-type]
                    popen=self.popen,
                    send_break=self.send_break,
                    kill=self.kill,
                    **kw,
                )
            )

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        wait_until(lambda: self.child is not None)
        return thread, result


def test_child_is_run_cmd_in_its_own_group_sharing_the_console() -> None:
    h = Harness()
    thread, result = h.run_in_thread()
    child = h.child
    assert child is not None
    assert child.args[1:] == ["/c", "run.cmd"]
    assert child.args[0].lower().endswith("cmd.exe")
    assert child.kwargs["cwd"] == "D:\\runners\\demo"
    assert child.kwargs["stdin"] == subprocess.DEVNULL
    assert child.kwargs["creationflags"] & winproc.CREATE_NEW_PROCESS_GROUP
    assert not child.kwargs["creationflags"] & winproc.CREATE_NO_WINDOW  # must share our console
    assert "stdout" not in child.kwargs  # inherited: the runner's output reaches the GUI's pipe
    child.finish(0)
    thread.join(5)
    assert result == [0]


def test_stop_command_sends_ctrl_break_and_returns_child_exit_code() -> None:
    h = Harness()
    thread, result = h.run_in_thread()
    h.stdin.feed(b"  stop \r\n")
    thread.join(5)
    assert not thread.is_alive()
    assert h.breaks == [777]
    assert h.kills == []
    assert result == [0]


def test_other_lines_are_ignored() -> None:
    h = Harness()
    thread, result = h.run_in_thread()
    h.stdin.feed(b"hello\nstopper\n")
    time.sleep(0.1)
    assert h.breaks == []
    assert thread.is_alive()
    h.stdin.feed(STOP_COMMAND + b"\n")
    thread.join(5)
    assert result == [0]


def test_stdin_eof_stops_the_runner_gracefully() -> None:
    # The GUI died: the helper must not leave run.cmd behind.
    h = Harness()
    thread, result = h.run_in_thread()
    h.stdin.close_writer()
    thread.join(5)
    assert h.breaks == [777]
    assert h.kills == []
    assert result == [0]


def test_stdin_eof_kills_the_tree_when_ctrl_break_is_ignored() -> None:
    h = Harness()
    h.send_break = lambda pid: h.breaks.append(pid)  # type: ignore[method-assign]
    thread, result = h.run_in_thread(orphan_timeout=0.2)
    h.stdin.close_writer()
    thread.join(5)
    assert h.breaks == [777]
    assert h.kills == [777]
    assert result == [1]


def test_break_failure_does_not_crash_the_helper() -> None:
    h = Harness()
    h.break_error = OSError("no console")
    thread, result = h.run_in_thread()
    h.stdin.feed(b"stop\n")
    time.sleep(0.05)
    assert thread.is_alive()
    assert h.child is not None
    h.child.finish(3)
    thread.join(5)
    assert result == [3]


def test_stop_after_exit_is_harmless() -> None:
    h = Harness()
    thread, result = h.run_in_thread()
    assert h.child is not None
    h.child.finish(0)
    thread.join(5)
    assert result == [0]
    h.stdin.feed(b"stop\n")  # the stdin thread may still be alive; nothing to signal
    time.sleep(0.05)
    assert h.breaks == []


@pytest.mark.parametrize("argv", [[], ["a", "b"], ["--version"], ["-h"]])
def test_main_usage(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(argv) == console_helper.EXIT_USAGE
    assert "usage" in capsys.readouterr().err


def test_main_runs_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(console_helper, "run", lambda d: calls.append(d) or 7)
    monkeypatch.setattr(console_helper.signal, "signal", lambda *a: None)
    assert main(["D:\\r"]) == 7
    assert calls == ["D:\\r"]


def test_winproc_helper_command_from_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winproc.sys, "frozen", False, raising=False)
    # Forward slashes: pathlib only splits on backslashes on Windows.
    monkeypatch.setattr(winproc.sys, "executable", "C:/venv/Scripts/pythonw.exe")
    assert winproc.helper_command() == [
        str(Path("C:/venv/Scripts/python.exe")),
        "-I",
        "-m",
        "runnerdeck.core.console_helper",
    ]
    monkeypatch.setattr(winproc.sys, "executable", "/usr/bin/python3")
    assert winproc.helper_command()[0] == str(Path("/usr/bin/python3"))


def test_winproc_helper_command_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(winproc.sys, "frozen", True, raising=False)
    monkeypatch.setattr(winproc.sys, "executable", str(tmp_path / "RunnerDeck.exe"))
    with pytest.raises(winproc.ProcessError, match="安装不完整"):
        winproc.helper_command()
    helper = tmp_path / winproc.HELPER_EXE_NAME
    helper.write_bytes(b"")
    assert winproc.helper_command() == [str(helper)]


def test_stop_command_constants_agree() -> None:
    assert winproc.HELPER_STOP_COMMAND == STOP_COMMAND


# --- Windows only: the real helper around the fake runner ------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def _python() -> str:
    base = getattr(sys, "_base_executable", "") or ""
    return base if base and Path(base).is_file() else sys.executable


def make_fake_runner_dir(tmp_path: Path, mode: str = "idle") -> tuple[Path, Path]:
    runner_dir = tmp_path / "fake-runner"
    runner_dir.mkdir()
    pid_file = tmp_path / "listener.pid"
    template = (FAKE_RUNNER / "run.cmd.template").read_text(encoding="utf-8")
    script = (
        template.replace("@PYTHON@", _python())
        .replace("@LISTENER@", str(FAKE_RUNNER / "fake_listener.py"))
        .replace("@MODE@", mode)
        .replace("@PIDFILE@", str(pid_file))
    )
    (runner_dir / "run.cmd").write_bytes("\r\n".join(script.splitlines()).encode("ascii") + b"\r\n")
    return runner_dir, pid_file


def listener_pid(pid_file: Path) -> int:
    wait_until(lambda: pid_file.exists() and pid_file.read_text().strip() != "", 20)
    return int(pid_file.read_text())


def start_real_helper(runner_dir: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [*winproc.helper_command(), str(runner_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=winproc.CREATE_NEW_PROCESS_GROUP | winproc.CREATE_NO_WINDOW,
    )


def read_all(proc: subprocess.Popen[bytes]) -> list[str]:
    assert proc.stdout is not None
    return [winproc.decode_line(raw) for raw in proc.stdout.read().splitlines(keepends=True)]


@windows_only
def test_windows_helper_relays_ctrl_break(tmp_path: Path) -> None:
    runner_dir, pid_file = make_fake_runner_dir(tmp_path)
    proc = start_real_helper(runner_dir)
    try:
        child = listener_pid(pid_file)
        assert proc.stdin is not None
        proc.stdin.write(b"stop\n")
        proc.stdin.flush()
        rc = proc.wait(30)
    finally:
        if proc.poll() is None:
            winproc.kill_tree(proc.pid)
    out = read_all(proc)
    assert rc == 0
    assert "Exiting..." in out
    assert '"Exiting runner..."' in out
    assert "\u221a Connected to GitHub" in out  # the runner's own output passes through
    wait_until(lambda: not psutil.pid_exists(child), 10)


@windows_only
def test_windows_helper_stops_runner_when_parent_closes_stdin(tmp_path: Path) -> None:
    runner_dir, pid_file = make_fake_runner_dir(tmp_path)
    proc = start_real_helper(runner_dir)
    try:
        child = listener_pid(pid_file)
        assert proc.stdin is not None
        proc.stdin.close()  # what the OS does when the GUI dies
        rc = proc.wait(30)
    finally:
        if proc.poll() is None:
            winproc.kill_tree(proc.pid)
    assert rc == 0
    assert "Exiting..." in read_all(proc)
    wait_until(lambda: not psutil.pid_exists(child), 10)


@windows_only
def test_windows_helper_exit_code_follows_run_cmd(tmp_path: Path) -> None:
    runner_dir, _ = make_fake_runner_dir(tmp_path, "crash")  # run.cmd itself exits 0 (spike §5)
    proc = start_real_helper(runner_dir)
    try:
        rc = proc.wait(30)
    finally:
        if proc.poll() is None:
            winproc.kill_tree(proc.pid)
    assert rc == 0
    assert '"Exiting after unknown error code: 15"' in read_all(proc)
