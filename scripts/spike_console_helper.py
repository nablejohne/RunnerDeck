"""M0 spike: console-owning helper for stopping run.cmd from a process without a console.

A tray app (pythonw) has no console, so it cannot deliver CTRL_BREAK to a child started with
CREATE_NO_WINDOW (confirmed in round 1). This helper is started with CREATE_NO_WINDOW and so owns
a hidden console; run.cmd inherits that console and our stdout. Writing "stop" to our stdin makes
us send CTRL_BREAK inside the shared console. Exit code is run.cmd's.

Usage: python spike_console_helper.py <runner_dir>
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading


def main() -> int:
    runner_dir = sys.argv[1]
    child = subprocess.Popen(
        ["cmd.exe", "/c", "run.cmd"],
        cwd=runner_dir,
        stdin=subprocess.DEVNULL,  # answers the "Terminate batch job (Y/N)?" prompt with EOF
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,  # no CREATE_NO_WINDOW: share our console
    )

    def watch_stdin() -> None:
        for line in sys.stdin:
            if line.strip() == "stop":
                os.kill(child.pid, signal.CTRL_BREAK_EVENT)

    threading.Thread(target=watch_stdin, daemon=True).start()
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
