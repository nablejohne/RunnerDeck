"""Console-owning helper that runs run.cmd for a process without a console (the GUI).

    runnerdeck-helper <runner_dir>            (frozen: runnerdeck-helper.exe)
    python -I -m runnerdeck.core.console_helper <runner_dir>

Only Ctrl+Break delivered through a console shared with run.cmd stops the runner cleanly
(docs/spike-notes.md §5), and a process without a console (pythonw, a windowed exe) cannot
deliver it. The GUI therefore starts this helper with CREATE_NO_WINDOW, which gives it a hidden
console of its own, and run.cmd inherits that console together with our stdout (so the runner's
output flows to the GUI's pipe unchanged). Measured in spike round 2 (§10): 0.91 s graceful stop.

Protocol on stdin (bytes, one command per line):
- `stop`: send CTRL_BREAK_EVENT to run.cmd's process group, keep waiting for it to exit.
- EOF (the GUI died or closed the pipe): stop the runner the same way, hard-kill the tree if
  it is still there after ORPHAN_STOP_TIMEOUT_S, then exit.
The exit code is run.cmd's. Nothing is printed by the helper itself. The process ends with
os._exit() (see the bottom of the file): a daemon thread blocked in a pipe read crashes the
interpreter's normal teardown on Windows.

Standard library only: the frozen helper exe must not drag Qt in, and nothing here may import
the rest of runnerdeck (this file is also a PyInstaller entry point). The Windows-specific calls
are injectable so the stdin protocol is unit-tested on every platform.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import IO, Any

STOP_COMMAND = b"stop"
EXIT_USAGE = 2
# run.cmd cancels a running job before exiting, ~15 s and more (spike §10); same as
# process.BUSY_STOP_TIMEOUT_S.
ORPHAN_STOP_TIMEOUT_S = 90.0
RUN_SCRIPT = "run.cmd"
CREATE_NEW_PROCESS_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

PopenFn = Callable[..., Any]
SignalFn = Callable[[int], None]
KillFn = Callable[[int], None]


def cmd_exe() -> str:
    """Absolute cmd.exe (same rule as winproc.cmd_exe: never one planted on PATH / in cwd)."""
    if sys.platform != "win32":
        return "cmd.exe"  # tests only
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or "C:\\Windows"
    return os.path.join(system_root, "System32", "cmd.exe")


def send_ctrl_break(pid: int) -> None:
    os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]


def kill_tree(pid: int) -> None:
    """taskkill /T /F: the whole cmd.exe -> run-helper -> Runner.Listener tree (spike §5)."""
    subprocess.run(
        [os.path.join(os.path.dirname(cmd_exe()), "taskkill.exe"), "/PID", str(pid), "/T", "/F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def run(
    runner_dir: str,
    *,
    stdin: IO[bytes] | None = None,
    popen: PopenFn = subprocess.Popen,
    send_break: SignalFn = send_ctrl_break,
    kill: KillFn = kill_tree,
    orphan_timeout: float = ORPHAN_STOP_TIMEOUT_S,
) -> int:
    """Run run.cmd in `runner_dir` until it exits; return its exit code."""
    stream = sys.stdin.buffer if stdin is None else stdin
    child = popen(
        [cmd_exe(), "/c", RUN_SCRIPT],
        cwd=runner_dir,
        # DEVNULL ends cmd.exe's "Terminate batch job (Y/N)?" prompt by itself (spike §8).
        stdin=subprocess.DEVNULL,
        # Own group so CTRL_BREAK can target it; no CREATE_NO_WINDOW: it must share our console.
        creationflags=CREATE_NEW_PROCESS_GROUP,
    )
    orphaned = threading.Event()

    def deliver_break() -> None:
        if child.poll() is not None:
            return
        # Already gone, or no console: the wait below ends when it exits or is killed.
        with contextlib.suppress(OSError):
            send_break(child.pid)

    def watch_stdin() -> None:
        try:
            for line in iter(stream.readline, b""):
                if line.strip() == STOP_COMMAND:
                    deliver_break()
        except (OSError, ValueError):
            pass
        # EOF: whoever started us is gone (or closed the pipe); never leave a runner behind.
        orphaned.set()
        deliver_break()

    threading.Thread(target=watch_stdin, name="helper-stdin", daemon=True).start()

    while True:
        try:
            return int(child.wait(timeout=1.0))
        except subprocess.TimeoutExpired:
            pass
        if orphaned.is_set():
            try:
                return int(child.wait(timeout=orphan_timeout))
            except subprocess.TimeoutExpired:
                kill(child.pid)
                return int(child.wait())


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 1 or args[0].startswith("-"):
        sys.stderr.write(
            f"usage: {os.path.basename(sys.argv[0]) or 'runnerdeck-helper'} <runner_dir>\n"
        )
        return EXIT_USAGE
    # Console events for the group we signal never target us, but a Ctrl+C typed into a
    # visible console (dev) must not kill the helper before run.cmd: ignore both.
    for name in ("SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, signal.SIG_IGN)
    return run(args[0])


if __name__ == "__main__":
    # Real (2026-10-23, Windows): sys.exit() crashed the helper with 0xC0000005 while the
    # daemon thread was still blocked in stdin.readline() during interpreter teardown, and the
    # GUI then saw that code instead of run.cmd's. There is nothing to flush or finalize here,
    # so skip teardown altogether.
    os._exit(main())
