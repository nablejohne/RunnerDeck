"""Windows process / console helpers shared by installer.py and process.py.

Findings from the M0 spike (docs/spike-notes.md §4–§5) encoded here:
- cmd.exe's own lines are in the console (OEM) code page, runner lines are UTF-8/ASCII.
- CTRL_BREAK only reaches a child that shares our console (CREATE_NO_WINDOW children ignore it).
- A runner is identified by Runner.*.exe processes whose executable lives under its directory.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from runnerdeck.core.errors import ProcessError

if TYPE_CHECKING:
    import psutil

IS_WINDOWS = sys.platform == "win32"
CREATE_NEW_PROCESS_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

RUNNER_PROCESS_NAMES = frozenset(
    {"runner.listener.exe", "runner.worker.exe", "runner.pluginhost.exe"}
)


def console_codepage_codec() -> str:
    """Codec for cmd.exe output written to a pipe: the OEM code page (GBK on zh-CN)."""
    return "oem" if IS_WINDOWS else "latin-1"


def decode_line(raw: bytes, fallback: str | None = None) -> str:
    """Decode one line of config.cmd / run.cmd output: UTF-8 first, console code page second."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode(fallback or console_codepage_codec(), errors="replace")
    return text.rstrip("\r\n")


def has_console() -> bool:
    """True if this process is attached to a console (so CTRL_BREAK can reach children).

    A tray app started with pythonw has none; the CLI in a terminal has one.
    """
    if not IS_WINDOWS:
        return False
    import ctypes

    return bool(ctypes.windll.kernel32.GetConsoleCP())  # type: ignore[attr-defined]


def send_ctrl_break(pid: int) -> None:
    """Send CTRL_BREAK_EVENT to the process group `pid` (created with CREATE_NEW_PROCESS_GROUP).

    Silently ineffective if the group does not share our console (spike §5).
    """
    if not IS_WINDOWS:
        raise ProcessError("CTRL_BREAK is only available on Windows")
    try:
        os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
    except OSError as e:
        raise ProcessError(f"无法向进程组 {pid} 发送 Ctrl+Break：{e}") from e


def is_under(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """Case-insensitive containment check that respects path boundaries.

    D:\\runners\\foo does not contain D:\\runners\\foobar\\x.exe.
    """
    p = os.path.normcase(os.path.abspath(path))
    r = os.path.normcase(os.path.abspath(root)).rstrip("\\/")
    return p == r or p.startswith(r + os.sep)


def find_runner_processes(runner_dir: Path) -> list[psutil.Process]:
    """Runner.Listener / Worker / PluginHost processes whose executable is under runner_dir."""
    import psutil

    found = []
    for proc in psutil.process_iter(["name", "exe"]):
        name = (proc.info.get("name") or "").lower()
        exe = proc.info.get("exe") or ""
        if name in RUNNER_PROCESS_NAMES and exe and is_under(exe, runner_dir):
            found.append(proc)
    return found


def kill_tree(pid: int, *, timeout: float = 5.0) -> list[int]:
    """Kill `pid` and all its descendants (TerminateProcess). Returns the pids killed."""
    import psutil

    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []
    procs = [root]
    with contextlib.suppress(psutil.Error):
        procs = [*root.children(recursive=True), root]
    return kill_processes(procs, timeout=timeout)


def kill_processes(procs: list[psutil.Process], *, timeout: float = 5.0) -> list[int]:
    import psutil

    for proc in procs:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    psutil.wait_procs(procs, timeout=timeout)
    return [p.pid for p in procs]
