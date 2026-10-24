"""One RunnerDeck per user session (docs/m3-design.md §6.1).

The first instance takes a QLockFile under paths.local_dir() and listens on a QLocalServer.
A second instance (the user double-clicks the exe again) fails to take the lock, connects to
that server and sends one line ("show"), and the first instance raises its window.

The lock decides who is primary: on Windows a local server name is a machine-wide named
pipe, and two processes can both listen on the same pipe name, so `listen()` alone proves
nothing. The name is derived from the data directory and the user name, so users logged in
at the same time (fast user switching, RDP) and different RUNNERDECK_HOME directories never
collide.
"""

from __future__ import annotations

import getpass
import hashlib
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt5.QtCore import QLockFile, QObject, pyqtSignal
from PyQt5.QtNetwork import QLocalServer, QLocalSocket

from runnerdeck.core import paths

SHOW_MESSAGE = "show"
LOCK_FILE_NAME = "runnerdeck.lock"
SERVER_PREFIX = "RunnerDeck-"
NAME_HASH_CHARS = 16
CONNECT_ATTEMPT_MS = 200  # one connect attempt; send_to_primary retries until its timeout
RETRY_INTERVAL_S = 0.1
DISCONNECT_WAIT_MS = 500
MAX_MESSAGE_BYTES = 4096  # messages are single short words; anything longer is dropped
ASFW_ANY = -1  # AllowSetForegroundWindow: (DWORD)-1 = any process may take the foreground
# Held while the GUI runs; packaging/RunnerDeck.iss names it in AppMutex, so setup and the
# uninstaller ask the user to quit RunnerDeck first (m4-design §9).
APP_MUTEX_NAME = "RunnerDeckAppMutex"
_SYNCHRONIZE = 0x00100000

log = logging.getLogger(__name__)


def _user_name() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no USERNAME and no pwd module: fall back to nothing
        return os.environ.get("USERNAME", "")


def server_name(base: Path | None = None) -> str:
    """ "RunnerDeck-" + sha256(normcase(base) + user name)[:16]; base defaults to local_dir()."""
    base = paths.local_dir() if base is None else base
    key = f"{os.path.normcase(os.path.abspath(base))}\0{_user_name()}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return SERVER_PREFIX + digest[:NAME_HASH_CHARS]


def _allow_set_foreground_window() -> bool:
    """Windows: let the primary take the foreground (we were started by the user, it was not).

    Without this, the primary's activateWindow() only flashes its taskbar button.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - cosmetic: activation still works, just less eagerly
        log.debug("AllowSetForegroundWindow failed", exc_info=True)
        return False


def _remaining_ms(deadline: float) -> int:
    return max(0, int((deadline - time.monotonic()) * 1000))


class _Connection(QObject):
    """Server side of one client: collects bytes, delivers each complete line."""

    def __init__(
        self, socket: QLocalSocket, deliver: Callable[[str], None], parent: QObject
    ) -> None:
        super().__init__(parent)
        self._socket = socket
        self._deliver = deliver
        self._buffer = bytearray()
        self._done = False
        socket.setParent(self)
        socket.readyRead.connect(self._read)
        socket.disconnected.connect(self._finish)
        self._read()  # the client may have written (and even left) before we got here
        if socket.state() == QLocalSocket.UnconnectedState:
            self._finish()

    def _read(self) -> None:
        if self._done:
            return
        self._buffer += bytes(self._socket.readAll())
        while b"\n" in self._buffer:
            line, _, rest = bytes(self._buffer).partition(b"\n")
            self._buffer = bytearray(rest)
            self._emit(line)
        if len(self._buffer) > MAX_MESSAGE_BYTES:
            log.warning("single instance: dropping an oversized message")
            self._buffer.clear()
            self._socket.abort()

    def _finish(self) -> None:
        if self._done:
            return
        self._read()
        self._done = True
        if self._buffer:
            self._emit(bytes(self._buffer))
            self._buffer.clear()
        self.deleteLater()

    def _emit(self, raw: bytes) -> None:
        if len(raw) > MAX_MESSAGE_BYTES:
            log.warning("single instance: dropping an oversized message")
            return
        text = raw.decode("utf-8", errors="replace").strip()
        if text:
            self._deliver(text)


class SingleInstance(QObject):
    """QLockFile + QLocalServer. acquire() once at startup; release() on exit.

    lock_dir defaults to paths.local_dir(); name defaults to server_name(lock_dir), so a test
    that points RUNNERDECK_HOME (or lock_dir) at a temporary directory gets its own server.
    """

    message_received = pyqtSignal(str)

    def __init__(
        self,
        *,
        lock_dir: Path | None = None,
        name: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._lock_dir = paths.local_dir() if lock_dir is None else lock_dir
        self._name = server_name(self._lock_dir) if name is None else name
        self._lock: QLockFile | None = None
        self._server: QLocalServer | None = None
        self._primary = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def lock_file(self) -> Path:
        return self._lock_dir / LOCK_FILE_NAME

    @property
    def is_primary(self) -> bool:
        return self._primary

    def acquire(self) -> bool:
        """True: we are the primary instance (now listening). False: another one holds the lock.

        A lock that cannot be created at all (data directory not writable) is not a second
        instance: we run anyway, unprotected and without a server, rather than refuse to start.
        """
        if self._primary:
            return True
        try:
            self._lock_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("single instance: cannot create %s: %s", self._lock_dir, e)
        lock = QLockFile(str(self.lock_file))
        # Stale only when the owning process is gone; the default 30 s age rule is meant for
        # short-lived locks, ours is held for the whole run.
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            if lock.error() == QLockFile.LockFailedError:
                log.info("single instance: %s is held by another instance", self.lock_file)
                return False
            log.warning(
                "single instance: cannot lock %s (error %s); running without the single-"
                "instance check",
                self.lock_file,
                int(lock.error()),
            )
            self._primary = True
            return True
        self._lock = lock
        self._primary = True
        self._listen()
        return True

    def _listen(self) -> None:
        # We hold the lock, so any socket file left under this name is from a crashed run.
        QLocalServer.removeServer(self._name)
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.UserAccessOption)
        server.newConnection.connect(self._on_new_connection)
        if not server.listen(self._name):
            log.warning(
                "single instance: cannot listen on %s: %s; a second start will not find this "
                "window",
                self._name,
                server.errorString(),
            )
            server.deleteLater()
            return
        self._server = server
        log.info("single instance: listening on %s", self._name)

    def _on_new_connection(self) -> None:
        server = self._server
        while server is not None and server.hasPendingConnections():
            socket = server.nextPendingConnection()
            if socket is None:
                break
            _Connection(socket, self.message_received.emit, self)

    def send_to_primary(self, message: str = SHOW_MESSAGE, timeout_ms: int = 2000) -> bool:
        """Deliver one message to the primary instance; False if it cannot be reached in time.

        Retries the connection until timeout_ms: the primary may hold the lock but not be
        listening yet. Blocking; called before this (second) instance builds any window.
        """
        payload = message.encode("utf-8") + b"\n"
        _allow_set_foreground_window()
        deadline = time.monotonic() + max(0, timeout_ms) / 1000
        while True:
            socket = QLocalSocket()
            socket.connectToServer(self._name)
            if socket.waitForConnected(max(1, min(_remaining_ms(deadline), CONNECT_ATTEMPT_MS))):
                return self._write(socket, payload, deadline)
            socket.abort()
            remaining = _remaining_ms(deadline)
            if remaining <= 0:
                log.info("single instance: no primary answered on %s", self._name)
                return False
            time.sleep(min(RETRY_INTERVAL_S, remaining / 1000))

    def _write(self, socket: QLocalSocket, payload: bytes, deadline: float) -> bool:
        socket.write(payload)
        written = socket.waitForBytesWritten(max(1, _remaining_ms(deadline)))
        delivered = written or socket.bytesToWrite() == 0
        socket.disconnectFromServer()
        if socket.state() != QLocalSocket.UnconnectedState:
            socket.waitForDisconnected(DISCONNECT_WAIT_MS)
        if not delivered:
            log.warning("single instance: could not write to %s", self._name)
        return delivered

    def release(self) -> None:
        """Stop listening and unlock. Idempotent."""
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None
        self._primary = False


class AppMutex:
    """A named Windows mutex held for the GUI's lifetime (no-op elsewhere).

    Only signals "RunnerDeck is running" to the installer; single-instance activation is the
    lock file + local server above.
    """

    def __init__(self, name: str = APP_MUTEX_NAME) -> None:
        self.name = name
        self._handle: int | None = None

    def acquire(self) -> bool:
        if sys.platform != "win32":
            return False
        kernel32 = _kernel32()
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            log.warning("CreateMutexW(%s) failed: %s", self.name, _last_error())
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            _kernel32().CloseHandle(self._handle)
            self._handle = None


def app_mutex_exists(name: str = APP_MUTEX_NAME) -> bool:
    """Some process holds the named mutex (what Inno Setup's AppMutex check does)."""
    if sys.platform != "win32":
        return False
    kernel32 = _kernel32()
    handle = kernel32.OpenMutexW(_SYNCHRONIZE, False, name)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def _last_error() -> int:
    import ctypes

    return ctypes.get_last_error()  # type: ignore[attr-defined]
