"""Run one whitelisted operation as administrator through UAC (docs/m4-design.md §2.3).

UAC elevates whole processes only, so the caller writes a request file and starts
`runnerdeck-cli elevated <request>` with ShellExecuteExW("runas"); that process (core/elevated.py)
does exactly the one operation and writes a result file next to the request. The request holds
short-lived registration / remove tokens: it lives under %LOCALAPPDATA%\\RunnerDeck\\elevate
(only the user and administrators can read it), the helper deletes it as soon as it has read
it, and the caller deletes both files afterwards in any case. The only module that calls
ShellExecuteExW.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from runnerdeck.core import paths
from runnerdeck.core.errors import ElevationCancelledError, ServiceError

log = logging.getLogger(__name__)

ELEVATED_SUBCOMMAND = "elevated"
REQUEST_VERSION = 1
RESULT_SUFFIX = ".result.json"
DEFAULT_TIMEOUT_S = 300.0  # config.cmd --runasservice takes ~7 s; the UAC prompt waits on the user
ERROR_CANCELLED = 1223  # the user answered "No" in the UAC prompt

# (executable, parameters, timeout_s) -> exit code; raises ElevationCancelledError.
Launcher = Callable[[str, str, float], int]


@dataclass(frozen=True)
class ElevatedResult:
    """What the elevated helper reports.

    registered: a GitHub registration may still exist after the operation (the caller falls
    back to the API DELETE where that matters). agent_id: from `.runner` afterwards.
    service_exists: the Windows service is (still) installed. warnings: Chinese notes.
    """

    ok: bool
    output: str = ""
    error: str | None = None
    service_name: str | None = None
    agent_id: int | None = None
    registered: bool = False
    service_exists: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> ElevatedResult:
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
            raise ValueError("not an elevated result")
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        known["warnings"] = tuple(str(w) for w in known.get("warnings") or ())
        return cls(**known)


def elevate_dir() -> Path:
    return paths.local_dir() / "elevate"


def helper_command() -> tuple[str, list[str]]:
    """How to start the CLI that runs `elevated`: runnerdeck-cli.exe next to the frozen
    executable, else this interpreter (python.exe rather than pythonw.exe) with -m."""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).with_name("runnerdeck-cli.exe")), [ELEVATED_SUBCOMMAND]
    python = Path(sys.executable)
    if python.name.casefold() == "pythonw.exe":
        python = python.with_name("python.exe")
    return str(python), ["-m", "runnerdeck.core.cli", ELEVATED_SUBCOMMAND]


class Elevator:
    """Callable(op, payload) -> ElevatedResult; the launcher and directory are injectable."""

    def __init__(
        self,
        *,
        launcher: Launcher | None = None,
        directory: Path | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._launcher = launcher or default_launcher
        self._directory = directory
        self._timeout_s = timeout_s

    def __call__(self, op: str, payload: Mapping[str, Any]) -> ElevatedResult:
        directory = self._directory or elevate_dir()
        request = directory / f"{uuid.uuid4().hex}.json"
        result_path = request.with_name(request.stem + RESULT_SUFFIX)
        exe, args = helper_command()
        try:
            _write_private(request, json.dumps({"version": REQUEST_VERSION, "op": op, **payload}))
            log.info("elevating %s (%s)", op, request.name)  # never the payload: it has tokens
            code = self._launcher(
                exe, subprocess.list2cmdline([*args, str(request)]), self._timeout_s
            )
            try:
                result = ElevatedResult.from_json(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as e:
                raise ServiceError(
                    f"管理员操作没有返回结果（退出码 {code}），详见 RunnerDeck CLI 日志"
                ) from e
            log.info("elevated %s -> ok=%s exit=%s", op, result.ok, code)
            return result
        finally:
            for path in (request, result_path):
                try:
                    path.unlink(missing_ok=True)
                except OSError as e:
                    log.warning("cannot delete %s: %s", path, e)


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def is_elevated() -> bool:
    """This process already runs with an administrator token (no UAC prompt needed)."""
    if sys.platform != "win32":
        return False
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def run_in_process(_executable: str, parameters: str, _timeout_s: float) -> int:
    """Launcher for an already elevated process (e.g. an administrator terminal or SSH
    session): run the helper here instead of asking UAC for what we already have."""
    from runnerdeck.core import elevated  # elevated imports this module

    return elevated.main(_last_arg(parameters))


def _last_arg(parameters: str) -> str:
    """The request path: the last argument of a subprocess.list2cmdline() string."""
    import shlex

    return shlex.split(parameters, posix=False)[-1].strip('"')


def default_launcher(executable: str, parameters: str, timeout_s: float) -> int:
    if is_elevated():
        return run_in_process(executable, parameters, timeout_s)
    return shell_execute_runas(executable, parameters, timeout_s)


def shell_execute_runas(executable: str, parameters: str, timeout_s: float) -> int:
    """Start `executable parameters` elevated (UAC prompt), hidden; wait; its exit code.

    "No" in the prompt -> ElevationCancelledError. Not Windows, a failed start or a timeout ->
    ServiceError (a timed-out helper is left running: we may not be allowed to end it).
    """
    if sys.platform != "win32":
        raise ServiceError("只有 Windows 支持管理员提权")
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    see_mask_nocloseprocess, see_mask_noasync, sw_hide = 0x40, 0x100, 0
    wait_object_0, infinite_cap_ms = 0, 0x7FFFFFFF
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = see_mask_nocloseprocess | see_mask_noasync
    info.lpVerb = "runas"
    info.lpFile = executable
    info.lpParameters = parameters
    info.nShow = sw_hide
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()  # type: ignore[attr-defined]
        if error == ERROR_CANCELLED:
            raise ElevationCancelledError("已取消：这个操作需要管理员授权")
        raise ServiceError(f"无法以管理员身份启动 {Path(executable).name}（错误 {error}）")
    if not info.hProcess:
        raise ServiceError("以管理员身份启动后拿不到进程句柄")
    try:
        wait_ms = min(int(timeout_s * 1000), infinite_cap_ms)
        if kernel32.WaitForSingleObject(info.hProcess, wait_ms) != wait_object_0:
            raise ServiceError(f"管理员操作 {int(timeout_s)} 秒内没有完成")
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)
