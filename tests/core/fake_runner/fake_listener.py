"""Stand-in for Runner.Listener.exe, started by the fake run.cmd in tests/core/.

Prints lines shaped like the real runner output (tests/fixtures/stdout/, spike-notes §4) and
behaves like the listener on Ctrl+Break: print "Exiting...", exit 0. The lines about the exit
code ("Runner listener exit with 0 return code", "Exiting after unknown error code: 15") come
from run-helper, i.e. from the fake run.cmd, as in the real layout.

Usage: python fake_listener.py <mode> <pid_file>
  idle        announce "Listening for Jobs", then wait for Ctrl+Break
  crash       announce "Listening for Jobs", then exit 15 on its own (a code run-helper does
              not know, like the psutil kill in spike §5)
  terminated  announce "Listening for Jobs", then exit 1 (run-helper's "terminated error",
              e.g. the registration was deleted on GitHub)
  retry       first launch: exit 2 (retryable, run.cmd relaunches); later launches: idle
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from types import FrameType

CRASH_EXIT_CODE = 15
TERMINATED_EXIT_CODE = 1
RETRYABLE_EXIT_CODE = 2


def emit(line: str) -> None:
    # Bytes, not print(): the pipe's locale encoding (cp1252 on CI) cannot encode the check mark.
    sys.stdout.buffer.write(line.encode("utf-8") + b"\r\n")
    sys.stdout.buffer.flush()


def stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())


def on_break(signum: int, frame: FrameType | None) -> None:
    emit("Exiting...")
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

    if mode == "retry":
        marker = Path(pid_file + ".retried")
        if not marker.exists():
            marker.write_text("1", encoding="ascii")
            time.sleep(0.2)
            return RETRYABLE_EXIT_CODE
    elif mode in ("crash", "terminated"):
        time.sleep(0.5)
        return CRASH_EXIT_CODE if mode == "crash" else TERMINATED_EXIT_CODE
    # Short sleeps: Python runs signal handlers between bytecodes, and a long time.sleep()
    # is not interrupted by SIGBREAK on Windows.
    while True:
        time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())
