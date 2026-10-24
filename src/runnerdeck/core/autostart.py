"""Launch RunnerDeck when the user logs in: the per-user Run key (HKCU only, no admin rights).

    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run   RunnerDeck = "<exe>" --autostart

Task Manager's "Startup apps" page does not delete that value when the user disables the entry;
it writes a binary flag under Explorer\\StartupApproved\\Run instead (first byte odd = disabled,
e.g. 03 00 00 ...; even = enabled, e.g. 02 00 ...). That key is only read here: re-enabling is
the user's decision, made where they disabled it.

The registry is injected (Registry protocol). Tests use an in-memory implementation and must
never touch the developer's HKCU; WinRegistry imports winreg only on Windows.
"""

from __future__ import annotations

import logging
import sys
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING, Any, Protocol

from runnerdeck.core.errors import RunnerDeckError
from runnerdeck.core.models import AutostartState

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "RunnerDeck"
AUTOSTART_FLAG = "--autostart"
APP_MODULE = "runnerdeck"  # `pythonw -m runnerdeck` when running from source


class AutostartError(RunnerDeckError):
    """The Run key could not be read or written, or the platform has no such thing."""


class Registry(Protocol):
    """Values under HKEY_CURRENT_USER. Implementations raise OSError for registry failures."""

    def get(self, key: str, name: str) -> str | bytes | None:
        """The value `name` under `key`; None if the key or the value does not exist."""
        ...

    def set_string(self, key: str, name: str, value: str) -> None:
        """Write a REG_SZ value, creating `key` if needed."""
        ...

    def delete(self, key: str, name: str) -> None:
        """Delete the value; silently nothing if the key or the value does not exist."""
        ...


def _on_windows() -> bool:
    return sys.platform == "win32"


class WinRegistry(Registry):
    """The real HKCU through winreg. Constructing it off Windows raises AutostartError."""

    def __init__(self) -> None:
        if not _on_windows():
            raise AutostartError("开机自启只支持 Windows")
        import winreg

        self._winreg: Any = winreg

    def get(self, key: str, name: str) -> str | bytes | None:
        winreg = self._winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ) as handle:
                value, _kind = winreg.QueryValueEx(handle, name)
        except FileNotFoundError:
            return None
        if value is None or isinstance(value, str | bytes):
            return value
        return str(value)  # e.g. a REG_DWORD someone put there by hand

    def set_string(self, key: str, name: str, value: str) -> None:
        winreg = self._winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
            winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, value)

    def delete(self, key: str, name: str) -> None:
        winreg = self._winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
                winreg.DeleteValue(handle, name)
        except FileNotFoundError:
            pass


def _quote(path: str) -> str:
    return f'"{path}"'


def launch_command(*, frozen: bool | None = None, executable: str | None = None) -> str:
    """The command line the Run value should hold for this installation.

    Frozen (PyInstaller): `"<dir>\\RunnerDeck.exe" --autostart`, i.e. the running GUI exe.
    From source: `"<dir>\\pythonw.exe" -m runnerdeck --autostart`; python.exe is swapped for
    the pythonw.exe next to it, so logging in does not flash a console window. Paths are always
    quoted. Defaults: `sys.frozen` and `sys.executable`.
    """
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    exe = PureWindowsPath(sys.executable if executable is None else executable)
    if is_frozen:
        return f"{_quote(str(exe))} {AUTOSTART_FLAG}"
    if exe.name.casefold() == "python.exe":
        exe = exe.with_name("pythonw.exe")
    return f"{_quote(str(exe))} -m {APP_MODULE} {AUTOSTART_FLAG}"


def _registry_call(what: str, key: str, call: Callable[[], Any]) -> Any:
    try:
        return call()
    except OSError as e:
        reason = e.strerror or str(e) or type(e).__name__
        raise AutostartError(f"无法{what}注册表 HKCU\\{key}\\{VALUE_NAME}：{reason}") from e


def _approved_disabled(value: str | bytes | None) -> bool:
    """StartupApproved data: the first byte odd means "disabled in Task Manager"."""
    return isinstance(value, bytes) and len(value) > 0 and value[0] % 2 == 1


def _open(registry: Registry | None) -> Registry:
    return registry if registry is not None else WinRegistry()


def state(registry: Registry | None = None) -> AutostartState:
    """What the Run key says now. Off Windows without an injected registry: supported=False.

    Raises AutostartError when the registry cannot be read.
    """
    try:
        reg = _open(registry)
    except AutostartError:
        return AutostartState(supported=False)
    value = _registry_call("读取", RUN_KEY, lambda: reg.get(RUN_KEY, VALUE_NAME))
    approved = _registry_call("读取", APPROVED_KEY, lambda: reg.get(APPROVED_KEY, VALUE_NAME))
    return AutostartState(
        supported=True,
        registered=value is not None,
        command=value if isinstance(value, str) else None,
        expected=launch_command(),
        disabled_by_user=_approved_disabled(approved),
    )


def enable(registry: Registry | None = None) -> AutostartState:
    """Write the Run value for this installation; the resulting state.

    AutostartError off Windows (no registry injected) or when writing fails. A Task Manager
    "disabled" flag stays as it is (see `disabled_by_user`).
    """
    reg = _open(registry)
    command = launch_command()
    _registry_call("写入", RUN_KEY, lambda: reg.set_string(RUN_KEY, VALUE_NAME, command))
    log.info("autostart enabled: %s", command)
    return state(reg)


def disable(registry: Registry | None = None) -> AutostartState:
    """Delete the Run value (nothing if absent); the resulting state."""
    reg = _open(registry)
    _registry_call("删除", RUN_KEY, lambda: reg.delete(RUN_KEY, VALUE_NAME))
    log.info("autostart disabled")
    return state(reg)


def reconcile(wanted: bool, registry: Registry | None = None) -> AutostartState:
    """Make the Run key agree with the setting, e.g. at every GUI start.

    wanted and (not registered, or another command, such as after the user moved the unzipped
    folder) -> rewrite it; not wanted but registered -> delete it; otherwise leave it alone.
    Unsupported platform -> AutostartState(supported=False) without raising. Registry
    failures -> AutostartError.
    """
    try:
        reg = _open(registry)
    except AutostartError:
        return AutostartState(supported=False)
    current = state(reg)
    if wanted and not current.matches:
        log.info(
            "autostart entry %r does not match %r, rewriting", current.command, current.expected
        )
        return enable(reg)
    if not wanted and current.registered:
        return disable(reg)
    return current
