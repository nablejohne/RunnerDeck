"""Stand-in for Runner.Listener.exe, started by the fake run.cmd in tests/core/test_process.py.

Prints lines shaped like the real runner output (tests/fixtures/stdout/, spike-notes §4) and
behaves like the listener on Ctrl+Break: print "Exiting...", exit 0.

Usage: python fake_listener.py <mode> <pid_file>
  idle   announce "Listening for Jobs", then wait for Ctrl+Break
  crash  announce "Listening for Jobs", then exit 3 on its own
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from types import FrameType

CRASH_EXIT_CODE = 3


def emit(line: str) -> None:
    # Bytes, not print(): the pipe's locale encoding (cp1252 on CI) cannot encode the check mark.
    sys.stdout.buffer.write(line.encode("utf-8") + b"\r\n")
    sys.stdout.buffer.flush()


def stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())


def on_break(signum: int, frame: FrameType | None) -> None:
    emit("Exiting...")
    emit('"Runner listener exit with 0 return code, stop the service, no retry needed."')
    sys.exit(0)


def main() -> int:
    mode, pid_file = sys.argv[1], sys.argv[2]
    Path(pid_file).write_text(str(os.getpid()), encoding="ascii")
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, on_break)

    emit("")
    emit("√ Connected to GitHub")
    emit("")
    emit("Current runner version: '2.338.0'")
    emit(f"{stamp()}: Listening for Jobs")

    if mode == "crash":
        time.sleep(0.5)
        emit(f'"Exiting after unknown error code: {CRASH_EXIT_CODE}"')
        return CRASH_EXIT_CODE
    # Short sleeps: Python runs signal handlers between bytecodes, and a long time.sleep()
    # is not interrupted by SIGBREAK on Windows.
    while True:
        time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())
