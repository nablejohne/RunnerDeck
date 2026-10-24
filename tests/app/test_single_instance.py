"""Tests for runnerdeck.app.single_instance (docs/m3-design.md §6.1, §6.4).

Two SingleInstance objects in one process stand in for two processes where only the lock is
involved: QLockFile refuses a second lock on the same file even within a process. Messages are
sent from a real second process (as in production): on Windows a named-pipe client in the same
thread cannot finish its write while the server's event loop is blocked by it. Everything lives
under a temporary RUNNERDECK_HOME, so the server names never collide with a real RunnerDeck.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from runnerdeck.app import single_instance
from runnerdeck.app.single_instance import SHOW_MESSAGE, SingleInstance, server_name
from runnerdeck.core import paths

SIGNAL_MS = 15000  # includes starting a Python + Qt subprocess on a CI runner
SENDER = textwrap.dedent(
    """
    import sys
    from PyQt5.QtCore import QCoreApplication
    from PyQt5.QtNetwork import QLocalSocket
    from runnerdeck.app.single_instance import SingleInstance

    app = QCoreApplication([])
    name, message, raw = sys.argv[1], sys.argv[2], sys.argv[3] == "raw"
    if raw:  # bytes as given, through a bare socket
        socket = QLocalSocket()
        socket.connectToServer(name)
        ok = socket.waitForConnected(5000)
        socket.write(message.encode("utf-8").decode("unicode_escape").encode("utf-8"))
        ok = ok and (socket.waitForBytesWritten(5000) or socket.bytesToWrite() == 0)
        socket.disconnectFromServer()
        if socket.state() != QLocalSocket.UnconnectedState:
            socket.waitForDisconnected(5000)
    else:
        ok = SingleInstance(name=name).send_to_primary(message)
    sys.exit(0 if ok else 1)
    """
)


def send_from_another_process(
    primary: SingleInstance, message: str = SHOW_MESSAGE, *, raw: bool = False
) -> subprocess.Popen[bytes]:
    """Start a second process that sends `message` to `primary` (the caller waits for it)."""
    mode = "raw" if raw else "send"
    return subprocess.Popen([sys.executable, "-c", SENDER, primary.name, message, mode])


def finished_ok(proc: subprocess.Popen[bytes]) -> bool:
    return proc.wait(timeout=SIGNAL_MS / 1000) == 0


def finished_ok_while_pumping(qtbot: QtBot, proc: subprocess.Popen[bytes]) -> bool:
    """Wait for a sender whose message produces no signal, keeping the primary's loop alive."""
    qtbot.waitUntil(lambda: proc.poll() is not None, timeout=SIGNAL_MS)
    return proc.returncode == 0


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def instances(qapp: object) -> Iterator[list[SingleInstance]]:
    """Every SingleInstance a test makes is released afterwards, even when the test fails."""
    made: list[SingleInstance] = []
    yield made
    for instance in made:
        instance.release()


def make(instances: list[SingleInstance], **kwargs: object) -> SingleInstance:
    instance = SingleInstance(**kwargs)  # type: ignore[arg-type]
    instances.append(instance)
    return instance


# --- server_name ---------------------------------------------------------------------------


def test_server_name_shape_and_default_base(home: Path) -> None:
    name = server_name()
    assert name.startswith("RunnerDeck-")
    suffix = name.removeprefix("RunnerDeck-")
    assert len(suffix) == 16
    int(suffix, 16)  # hex
    assert name == server_name(paths.local_dir())


def test_server_name_changes_with_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "a"))
    first = server_name()
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "b"))
    second = server_name()
    assert first != second
    assert server_name(tmp_path / "a") == first


def test_server_name_changes_with_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(single_instance.getpass, "getuser", lambda: "alice")
    alice = server_name(tmp_path)
    monkeypatch.setattr(single_instance.getpass, "getuser", lambda: "bob")
    assert server_name(tmp_path) != alice


def test_server_name_survives_missing_user_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_user() -> str:
        raise OSError("no user name")

    monkeypatch.setattr(single_instance.getpass, "getuser", no_user)
    monkeypatch.setenv("USERNAME", "carol")
    assert server_name(tmp_path).startswith("RunnerDeck-")


@pytest.mark.skipif(sys.platform != "win32", reason="paths are case-insensitive on Windows only")
def test_server_name_ignores_path_case_on_windows(tmp_path: Path) -> None:
    assert server_name(Path(str(tmp_path).upper())) == server_name(Path(str(tmp_path).lower()))


def test_instance_defaults_follow_home(home: Path, instances: list[SingleInstance]) -> None:
    instance = make(instances)
    assert instance.name == server_name(home)
    assert instance.lock_file == home / "runnerdeck.lock"


# --- two instances in one process -------------------------------------------------------------


def test_second_instance_activates_the_primary(
    qtbot: QtBot, home: Path, instances: list[SingleInstance]
) -> None:
    primary = make(instances)
    assert primary.acquire()
    assert primary.is_primary
    assert (home / "runnerdeck.lock").exists()  # acquire creates the data directory

    second = make(instances)
    assert not second.acquire()
    assert not second.is_primary

    with qtbot.waitSignal(primary.message_received, timeout=SIGNAL_MS) as blocker:
        sender = send_from_another_process(primary)
    assert finished_ok(sender)
    assert blocker.args == [SHOW_MESSAGE]


def test_custom_message_and_several_clients(
    qtbot: QtBot, home: Path, instances: list[SingleInstance]
) -> None:
    primary = make(instances)
    assert primary.acquire()
    received: list[str] = []
    primary.message_received.connect(received.append)

    senders = [send_from_another_process(primary, m) for m in ("hello", SHOW_MESSAGE)]
    qtbot.waitUntil(lambda: len(received) == 2, timeout=SIGNAL_MS)
    assert all(finished_ok(proc) for proc in senders)
    assert sorted(received) == sorted(["hello", SHOW_MESSAGE])


def test_several_lines_in_one_connection(
    qtbot: QtBot, home: Path, instances: list[SingleInstance]
) -> None:
    primary = make(instances)
    assert primary.acquire()
    received: list[str] = []
    primary.message_received.connect(received.append)

    # blank lines dropped, last line without newline
    sender = send_from_another_process(primary, "one\\n\\n  two  \\nthree", raw=True)
    qtbot.waitUntil(lambda: len(received) == 3, timeout=SIGNAL_MS)
    assert finished_ok(sender)
    assert received == ["one", "two", "three"]


def test_oversized_message_is_dropped(
    qtbot: QtBot, home: Path, instances: list[SingleInstance]
) -> None:
    primary = make(instances)
    assert primary.acquire()
    received: list[str] = []
    primary.message_received.connect(received.append)

    big = send_from_another_process(primary, "x" * (single_instance.MAX_MESSAGE_BYTES + 10))
    assert finished_ok_while_pumping(qtbot, big)
    after = send_from_another_process(primary, "after")
    qtbot.waitUntil(lambda: "after" in received, timeout=SIGNAL_MS)
    assert finished_ok(after)
    assert received == ["after"]


def test_send_without_primary_times_out(home: Path, instances: list[SingleInstance]) -> None:
    lonely = make(instances)
    started = time.monotonic()
    assert not lonely.send_to_primary(timeout_ms=300)
    assert time.monotonic() - started < 5


def test_release_lets_the_next_instance_take_over(
    qtbot: QtBot, home: Path, instances: list[SingleInstance]
) -> None:
    first = make(instances)
    assert first.acquire()
    assert first.acquire()  # idempotent while held
    first.release()
    first.release()  # idempotent
    assert not first.is_primary

    second = make(instances)
    assert second.name == first.name  # same home, same server name: reused after release
    assert second.acquire()
    with qtbot.waitSignal(second.message_received, timeout=SIGNAL_MS):
        sender = send_from_another_process(second)
    assert finished_ok(sender)


def test_different_homes_do_not_interfere(tmp_path: Path, instances: list[SingleInstance]) -> None:
    a = make(instances, lock_dir=tmp_path / "a")
    b = make(instances, lock_dir=tmp_path / "b")
    assert a.acquire()
    assert b.acquire()
    assert a.name != b.name


def test_lock_left_by_a_dead_process_is_taken_over(
    tmp_path: Path, instances: list[SingleInstance]
) -> None:
    lock_dir = tmp_path / "crashed"
    lock_dir.mkdir()
    lock_path = lock_dir / single_instance.LOCK_FILE_NAME
    # A process that takes the lock and dies without unlocking (os._exit skips destructors).
    script = textwrap.dedent(
        f"""
        import os
        from PyQt5.QtCore import QLockFile
        lock = QLockFile({str(lock_path)!r})
        assert lock.tryLock(0)
        os._exit(0)
        """
    )
    subprocess.run([sys.executable, "-c", script], check=True, timeout=60)
    assert lock_path.exists()

    assert make(instances, lock_dir=lock_dir).acquire()


def test_unwritable_lock_dir_runs_unprotected(
    tmp_path: Path, instances: list[SingleInstance]
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    instance = make(instances, lock_dir=blocker / "sub")
    assert instance.acquire()  # degraded: no lock, no server, but the app still starts
    assert instance.is_primary
    assert not make(instances, lock_dir=blocker / "sub").send_to_primary(timeout_ms=200)


# --- AllowSetForegroundWindow ---------------------------------------------------------------


def test_send_allows_the_primary_to_take_the_foreground(
    home: Path, instances: list[SingleInstance], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        single_instance, "_allow_set_foreground_window", lambda: calls.append(1) or True
    )
    make(instances).send_to_primary(timeout_ms=50)
    assert calls == [1]


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows behaviour")
def test_allow_set_foreground_window_is_a_no_op_elsewhere() -> None:
    assert single_instance._allow_set_foreground_window() is False


@pytest.mark.skipif(sys.platform != "win32", reason="user32.AllowSetForegroundWindow")
def test_allow_set_foreground_window_calls_user32() -> None:
    # The return value depends on whether this process may set the foreground (often not on
    # CI); the call itself must not raise.
    assert isinstance(single_instance._allow_set_foreground_window(), bool)
