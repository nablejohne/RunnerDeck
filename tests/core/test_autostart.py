"""Tests for runnerdeck.core.autostart.

Everything runs against an in-memory Registry (and a fake `winreg` module for WinRegistry):
no test ever reads or writes the real HKCU, on Windows either.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

from runnerdeck.core import autostart
from runnerdeck.core.autostart import (
    APPROVED_KEY,
    AUTOSTART_FLAG,
    RUN_KEY,
    VALUE_NAME,
    AutostartError,
    WinRegistry,
    launch_command,
)
from runnerdeck.core.errors import RunnerDeckError
from runnerdeck.core.models import AutostartState

FROZEN_EXE = r"D:\Apps\RunnerDeck-0.2.0\RunnerDeck.exe"
VENV_PYTHON = r"D:\src\RunnerDeck\.venv\Scripts\python.exe"
EXPECTED_DEV = r'"D:\src\RunnerDeck\.venv\Scripts\pythonw.exe" -m runnerdeck --autostart'
ENABLED = bytes([2]) + bytes(11)
DISABLED = bytes([3]) + bytes(11)


class MemoryRegistry:
    """In-memory HKCU: {key: {name: value}}. `fail` makes the named operation raise OSError."""

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, str | bytes]] = {}
        self.fail: dict[str, OSError] = {}
        self.calls: list[tuple[str, str, str]] = []

    def _check(self, op: str, key: str, name: str) -> None:
        self.calls.append((op, key, name))
        if op in self.fail:
            raise self.fail[op]

    def get(self, key: str, name: str) -> str | bytes | None:
        self._check("get", key, name)
        return self.keys.get(key, {}).get(name)

    def set_string(self, key: str, name: str, value: str) -> None:
        self._check("set", key, name)
        self.keys.setdefault(key, {})[name] = value

    def delete(self, key: str, name: str) -> None:
        self._check("delete", key, name)
        self.keys.get(key, {}).pop(name, None)

    def run_value(self) -> str | bytes | None:
        return self.keys.get(RUN_KEY, {}).get(VALUE_NAME)


@pytest.fixture
def reg() -> MemoryRegistry:
    return MemoryRegistry()


@pytest.fixture
def dev_python(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pretend we run from source with a Windows venv interpreter."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", VENV_PYTHON)
    return EXPECTED_DEV


# --- launch_command ---------------------------------------------------------------------------


def test_launch_command_frozen() -> None:
    assert launch_command(frozen=True, executable=FROZEN_EXE) == f'"{FROZEN_EXE}" --autostart'


@pytest.mark.parametrize(
    ("executable", "expected"),
    [
        (VENV_PYTHON, EXPECTED_DEV),
        (r"C:\Python311\PYTHON.EXE", r'"C:\Python311\pythonw.exe" -m runnerdeck --autostart'),
        (r"C:\Python311\pythonw.exe", r'"C:\Python311\pythonw.exe" -m runnerdeck --autostart'),
        (
            r"C:\Program Files\Py\python3.11.exe",
            r'"C:\Program Files\Py\python3.11.exe" -m runnerdeck --autostart',
        ),
    ],
)
def test_launch_command_from_source(executable: str, expected: str) -> None:
    assert launch_command(frozen=False, executable=executable) == expected


def test_launch_command_defaults_follow_sys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "executable", FROZEN_EXE)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert launch_command() == f'"{FROZEN_EXE}" {AUTOSTART_FLAG}'
    monkeypatch.setattr(sys, "frozen", False)
    monkeypatch.setattr(sys, "executable", VENV_PYTHON)
    assert launch_command() == EXPECTED_DEV


# --- state / enable / disable / reconcile -----------------------------------------------------


def test_state_not_registered(reg: MemoryRegistry, dev_python: str) -> None:
    assert autostart.state(reg) == AutostartState(
        supported=True, registered=False, command=None, expected=dev_python
    )


def test_enable_writes_the_run_value(reg: MemoryRegistry, dev_python: str) -> None:
    result = autostart.enable(reg)
    assert reg.run_value() == dev_python
    assert result.registered and result.matches and not result.disabled_by_user
    assert result.command == dev_python
    assert ("set", RUN_KEY, VALUE_NAME) in reg.calls


def test_disable_deletes_and_is_idempotent(reg: MemoryRegistry, dev_python: str) -> None:
    autostart.enable(reg)
    assert not autostart.disable(reg).registered
    assert reg.run_value() is None
    assert not autostart.disable(reg).registered  # already gone: silent


def test_matches_ignores_case(reg: MemoryRegistry, dev_python: str) -> None:
    reg.keys[RUN_KEY] = {VALUE_NAME: dev_python.upper()}
    assert autostart.state(reg).matches


def test_state_reads_task_manager_flag(reg: MemoryRegistry, dev_python: str) -> None:
    autostart.enable(reg)
    reg.keys[APPROVED_KEY] = {VALUE_NAME: DISABLED}
    disabled = autostart.state(reg)
    assert disabled.disabled_by_user and disabled.registered and disabled.matches
    reg.keys[APPROVED_KEY][VALUE_NAME] = ENABLED
    assert not autostart.state(reg).disabled_by_user
    reg.keys[APPROVED_KEY][VALUE_NAME] = bytes([7]) + bytes(11)  # any odd first byte
    assert autostart.state(reg).disabled_by_user


@pytest.mark.parametrize("value", [b"", "03", None])
def test_unusual_approved_values_are_not_disabled(
    reg: MemoryRegistry, dev_python: str, value: Any
) -> None:
    if value is not None:
        reg.keys[APPROVED_KEY] = {VALUE_NAME: value}
    assert not autostart.state(reg).disabled_by_user


def test_enable_never_touches_the_approved_key(reg: MemoryRegistry, dev_python: str) -> None:
    reg.keys[APPROVED_KEY] = {VALUE_NAME: DISABLED}
    result = autostart.enable(reg)
    assert reg.keys[APPROVED_KEY][VALUE_NAME] == DISABLED
    assert result.disabled_by_user  # the user re-enables it in Task Manager
    assert not [c for c in reg.calls if c[0] != "get" and c[1] == APPROVED_KEY]


def test_binary_run_value_counts_as_registered_but_not_matching(
    reg: MemoryRegistry, dev_python: str
) -> None:
    reg.keys[RUN_KEY] = {VALUE_NAME: b"\x00\x01"}
    result = autostart.state(reg)
    assert result.registered and result.command is None and not result.matches


@pytest.mark.parametrize(
    ("wanted", "before", "after", "writes"),
    [
        (True, None, EXPECTED_DEV, [("set", RUN_KEY, VALUE_NAME)]),
        (
            True,
            r'"E:\old\RunnerDeck.exe" --autostart',
            EXPECTED_DEV,
            [("set", RUN_KEY, VALUE_NAME)],
        ),
        (True, EXPECTED_DEV, EXPECTED_DEV, []),
        (False, EXPECTED_DEV, None, [("delete", RUN_KEY, VALUE_NAME)]),
        (False, r'"E:\old\RunnerDeck.exe" --autostart', None, [("delete", RUN_KEY, VALUE_NAME)]),
        (False, None, None, []),
    ],
)
def test_reconcile(
    reg: MemoryRegistry,
    dev_python: str,
    wanted: bool,
    before: str | None,
    after: str | None,
    writes: list[tuple[str, str, str]],
) -> None:
    if before is not None:
        reg.keys[RUN_KEY] = {VALUE_NAME: before}
    result = autostart.reconcile(wanted, reg)
    assert reg.run_value() == after
    assert result.registered is (after is not None)
    assert [c for c in reg.calls if c[0] != "get"] == writes


@pytest.mark.parametrize(
    ("op", "call", "verb"),
    [
        ("get", lambda r: autostart.state(r), "读取"),
        ("set", lambda r: autostart.enable(r), "写入"),
        ("delete", lambda r: autostart.disable(r), "删除"),
        ("set", lambda r: autostart.reconcile(True, r), "写入"),
    ],
)
def test_registry_errors_become_autostart_errors(
    reg: MemoryRegistry, dev_python: str, op: str, call: Any, verb: str
) -> None:
    reg.fail[op] = PermissionError(5, "拒绝访问")
    with pytest.raises(AutostartError) as info:
        call(reg)
    assert isinstance(info.value, RunnerDeckError)
    assert f"无法{verb}注册表 HKCU\\{RUN_KEY}\\{VALUE_NAME}：拒绝访问" in str(info.value)


# --- unsupported platform / WinRegistry ----------------------------------------------------------


@pytest.fixture
def not_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "_on_windows", lambda: False)


def test_win_registry_refuses_other_platforms(not_windows: None) -> None:
    with pytest.raises(AutostartError, match="开机自启只支持 Windows"):
        WinRegistry()


def test_unsupported_platform(not_windows: None) -> None:
    assert autostart.state() == AutostartState(supported=False)
    assert autostart.reconcile(True) == AutostartState(supported=False)
    assert autostart.reconcile(False) == AutostartState(supported=False)
    with pytest.raises(AutostartError, match="只支持 Windows"):
        autostart.enable()
    with pytest.raises(AutostartError, match="只支持 Windows"):
        autostart.disable()


class FakeKey:
    def __init__(self, store: FakeWinreg, path: str, create: bool) -> None:
        if path not in store.keys:
            if not create:
                raise FileNotFoundError(2, "系统找不到指定的文件。")
            store.keys[path] = {}
        self.values = store.keys[path]

    def __enter__(self) -> FakeKey:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class FakeWinreg(types.ModuleType):
    """The subset of winreg WinRegistry uses, backed by a dict (HKCU only)."""

    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1
    REG_BINARY = 3
    REG_DWORD = 4

    def __init__(self) -> None:
        super().__init__("winreg")
        self.keys: dict[str, dict[str, tuple[Any, int]]] = {}
        self.opened: list[tuple[str, int]] = []

    def OpenKey(self, root: object, path: str, reserved: int, access: int) -> FakeKey:
        assert root is self.HKEY_CURRENT_USER
        self.opened.append((path, access))
        return FakeKey(self, path, create=False)

    def CreateKeyEx(self, root: object, path: str, reserved: int, access: int) -> FakeKey:
        assert root is self.HKEY_CURRENT_USER
        self.opened.append((path, access))
        return FakeKey(self, path, create=True)

    def QueryValueEx(self, key: FakeKey, name: str) -> tuple[Any, int]:
        try:
            return key.values[name]
        except KeyError:
            raise FileNotFoundError(2, "系统找不到指定的文件。") from None

    def SetValueEx(self, key: FakeKey, name: str, reserved: int, kind: int, value: Any) -> None:
        key.values[name] = (value, kind)

    def DeleteValue(self, key: FakeKey, name: str) -> None:
        if name not in key.values:
            raise FileNotFoundError(2, "系统找不到指定的文件。")
        del key.values[name]


@pytest.fixture
def fake_winreg(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWinreg]:
    fake = FakeWinreg()
    monkeypatch.setattr(autostart, "_on_windows", lambda: True)
    monkeypatch.setitem(sys.modules, "winreg", fake)
    yield fake


def test_win_registry_round_trip(fake_winreg: FakeWinreg) -> None:
    reg = WinRegistry()
    assert reg.get(RUN_KEY, VALUE_NAME) is None  # key missing
    reg.delete(RUN_KEY, VALUE_NAME)  # key missing: silent
    reg.set_string(RUN_KEY, VALUE_NAME, '"C:\\x\\RunnerDeck.exe" --autostart')
    assert fake_winreg.keys[RUN_KEY][VALUE_NAME] == (
        '"C:\\x\\RunnerDeck.exe" --autostart',
        FakeWinreg.REG_SZ,
    )
    assert reg.get(RUN_KEY, VALUE_NAME) == '"C:\\x\\RunnerDeck.exe" --autostart'
    assert reg.get(RUN_KEY, "Other") is None  # value missing
    reg.delete(RUN_KEY, "Other")  # value missing: silent
    reg.delete(RUN_KEY, VALUE_NAME)
    assert reg.get(RUN_KEY, VALUE_NAME) is None
    assert (RUN_KEY, FakeWinreg.KEY_READ) in fake_winreg.opened
    assert (RUN_KEY, FakeWinreg.KEY_SET_VALUE) in fake_winreg.opened


def test_win_registry_value_types(fake_winreg: FakeWinreg) -> None:
    fake_winreg.keys[APPROVED_KEY] = {
        VALUE_NAME: (DISABLED, FakeWinreg.REG_BINARY),
        "Dword": (1, FakeWinreg.REG_DWORD),
        "Empty": (None, FakeWinreg.REG_BINARY),
    }
    reg = WinRegistry()
    assert reg.get(APPROVED_KEY, VALUE_NAME) == DISABLED
    assert reg.get(APPROVED_KEY, "Dword") == "1"
    assert reg.get(APPROVED_KEY, "Empty") is None


def test_default_registry_is_win_registry(
    fake_winreg: FakeWinreg, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", FROZEN_EXE)
    expected = f'"{FROZEN_EXE}" --autostart'
    assert autostart.state() == AutostartState(supported=True, expected=expected)
    assert autostart.reconcile(True).matches
    assert fake_winreg.keys[RUN_KEY][VALUE_NAME] == (expected, FakeWinreg.REG_SZ)
    fake_winreg.keys[APPROVED_KEY] = {VALUE_NAME: (DISABLED, FakeWinreg.REG_BINARY)}
    assert autostart.state().disabled_by_user
    assert not autostart.disable().registered
    assert autostart.enable().registered


def test_win_registry_os_errors_become_autostart_errors(
    fake_winreg: FakeWinreg, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*args: Any) -> None:
        raise PermissionError(5, "拒绝访问")

    monkeypatch.setattr(fake_winreg, "CreateKeyEx", denied)
    with pytest.raises(AutostartError, match="无法写入注册表.*拒绝访问"):
        autostart.enable()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_win_registry_constructs_without_touching_the_registry() -> None:
    WinRegistry()  # only imports winreg; no key is opened
