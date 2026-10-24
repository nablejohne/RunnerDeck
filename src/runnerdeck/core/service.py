"""Windows services of service-mode runners (docs/m4-design.md §2.2, spike-notes §12).

The only module that imports win32service / win32security. Queries need no admin rights;
start / stop work without them once RunnerDeck granted the user control at install time
(grant_control, run in the elevated helper). Everything is behind ServiceOps so tests and
non-Windows platforms use stand-ins.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from runnerdeck.core.errors import ElevationRequiredError, InvalidInputError, ServiceError

log = logging.getLogger(__name__)

SERVICE_FILE = ".service"  # written by `config.cmd --runasservice`: the service name, one line
# actions.runner.{owner}-{repo}.{name}; also what the elevated helper accepts.
SERVICE_NAME_RE = re.compile(r"^actions\.runner\.[A-Za-z0-9._-]{1,200}$")

# Win32 error codes the SCM returns.
_ACCESS_DENIED = 5
_SERVICE_ALREADY_RUNNING = 1056
_SERVICE_DOES_NOT_EXIST = 1060
_SERVICE_NOT_ACTIVE = 1062
_SERVICE_MARKED_FOR_DELETE = 1072

# Rights granted to the installing user: query config (CC), query status (LC), start (RP),
# stop (WP), interrogate (LO), read control (RC) -> "(A;;CCLCRPWPLORC;;;<sid>)".
_SERVICE_QUERY_CONFIG = 0x0001
_SERVICE_QUERY_STATUS = 0x0004
_SERVICE_START = 0x0010
_SERVICE_STOP = 0x0020
_SERVICE_INTERROGATE = 0x0080
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_DELETE = 0x00010000
USER_CONTROL_RIGHTS = (
    _SERVICE_QUERY_CONFIG
    | _SERVICE_QUERY_STATUS
    | _SERVICE_START
    | _SERVICE_STOP
    | _SERVICE_INTERROGATE
    | _READ_CONTROL
)


class ServiceState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    START_PENDING = "start_pending"
    STOP_PENDING = "stop_pending"
    PAUSED = "paused"  # also continue / pause pending; runners never pause
    MISSING = "missing"  # no such service
    UNKNOWN = "unknown"  # could not be queried (access denied, not Windows)


_SCM_STATES = {
    1: ServiceState.STOPPED,
    2: ServiceState.START_PENDING,
    3: ServiceState.STOP_PENDING,
    4: ServiceState.RUNNING,
    5: ServiceState.PAUSED,
    6: ServiceState.PAUSED,
    7: ServiceState.PAUSED,
}


@dataclass(frozen=True)
class ServiceInfo:
    """exit_code: Win32ExitCode; 1 when the Listener quit on its own (e.g. it cannot list a
    parent directory, spike-notes §12). detail: why the state is UNKNOWN."""

    name: str
    state: ServiceState
    pid: int | None = None
    exit_code: int = 0
    detail: str | None = None


class ServiceOps(Protocol):
    def query(self, name: str) -> ServiceInfo: ...
    def start(self, name: str) -> None: ...
    def stop(self, name: str) -> None: ...
    def grant_control(self, name: str, sid: str) -> None: ...
    def delete(self, name: str) -> None: ...


def check_service_name(name: str) -> str:
    if not SERVICE_NAME_RE.fullmatch(name):
        raise InvalidInputError(f"服务名不合法：{name!r}")
    return name


def read_service_name(runner_dir: Path) -> str | None:
    """The service name from `<runner_dir>/.service`; None if missing, unreadable or invalid."""
    path = runner_dir / SERVICE_FILE
    try:
        name = path.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        log.warning("cannot read %s: %s", path, e)
        return None
    if not SERVICE_NAME_RE.fullmatch(name):
        log.warning("unexpected service name in %s: %r", path, name[:100])
        return None
    return name


def wait_for(
    ops: ServiceOps,
    name: str,
    states: Iterable[ServiceState],
    *,
    timeout_s: float,
    interval_s: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ServiceInfo:
    """Poll until the service is in one of `states` (or MISSING); the last info on timeout."""
    wanted = set(states) | {ServiceState.MISSING}
    deadline = clock() + timeout_s
    info = ops.query(name)
    while info.state not in wanted and clock() < deadline:
        sleep(interval_s)
        info = ops.query(name)
    return info


class UnsupportedServiceOps:
    """Not Windows: every query is UNKNOWN, every change is refused."""

    _REASON = "只有 Windows 支持服务模式"

    def query(self, name: str) -> ServiceInfo:
        return ServiceInfo(name, ServiceState.UNKNOWN, detail=self._REASON)

    def _refuse(self, *_args: object) -> None:
        raise InvalidInputError(self._REASON)

    start = stop = grant_control = delete = _refuse


class WindowsServiceOps:
    """ServiceOps over the service control manager (pywin32)."""

    def __init__(self) -> None:
        import win32service  # Windows only

        self._ws = win32service

    def _open(self, name: str, access: int) -> Any:
        check_service_name(name)
        ws = self._ws
        scm = ws.OpenSCManager(None, None, ws.SC_MANAGER_CONNECT)
        try:
            return ws.OpenService(scm, name, access)
        finally:
            ws.CloseServiceHandle(scm)

    def query(self, name: str) -> ServiceInfo:
        try:
            handle = self._open(name, _SERVICE_QUERY_STATUS)
        except self._ws.error as e:
            if e.winerror == _SERVICE_DOES_NOT_EXIST:
                return ServiceInfo(name, ServiceState.MISSING)
            return ServiceInfo(name, ServiceState.UNKNOWN, detail=_describe(e))
        try:
            status = self._ws.QueryServiceStatusEx(handle)
        except self._ws.error as e:
            return ServiceInfo(name, ServiceState.UNKNOWN, detail=_describe(e))
        finally:
            self._ws.CloseServiceHandle(handle)
        return ServiceInfo(
            name,
            _SCM_STATES.get(status["CurrentState"], ServiceState.UNKNOWN),
            pid=status["ProcessId"] or None,
            exit_code=status["Win32ExitCode"],
        )

    def start(self, name: str) -> None:
        self._control(name, _SERVICE_START, "启动", lambda h: self._ws.StartService(h, None))

    def stop(self, name: str) -> None:
        stop = self._ws.SERVICE_CONTROL_STOP
        self._control(name, _SERVICE_STOP, "停止", lambda h: self._ws.ControlService(h, stop))

    def _control(self, name: str, access: int, verb: str, action: Callable[[Any], Any]) -> None:
        try:
            handle = self._open(name, access)
        except self._ws.error as e:
            raise _service_error(e, name, verb) from e
        try:
            action(handle)
        except self._ws.error as e:
            if e.winerror in (_SERVICE_ALREADY_RUNNING, _SERVICE_NOT_ACTIVE):
                return  # already where we wanted it
            raise _service_error(e, name, verb) from e
        finally:
            self._ws.CloseServiceHandle(handle)
        log.info("service %s: %s requested", name, verb)

    def grant_control(self, name: str, sid: str) -> None:
        import win32security

        ws = self._ws
        try:
            handle = self._open(name, _READ_CONTROL | _WRITE_DAC)
            try:
                info = win32security.DACL_SECURITY_INFORMATION
                sd = ws.QueryServiceObjectSecurity(handle, info)
                dacl = sd.GetSecurityDescriptorDacl()
                trustee = win32security.ConvertStringSidToSid(sid)
                dacl.AddAccessAllowedAce(win32security.ACL_REVISION, USER_CONTROL_RIGHTS, trustee)
                sd.SetSecurityDescriptorDacl(True, dacl, False)
                ws.SetServiceObjectSecurity(handle, info, sd)
            finally:
                ws.CloseServiceHandle(handle)
        except (ws.error, win32security.error) as e:
            raise _service_error(e, name, "授权") from e
        log.info("service %s: control granted to %s", name, sid)

    def delete(self, name: str) -> None:
        ws = self._ws
        try:
            handle = self._open(name, _DELETE | _SERVICE_STOP | _SERVICE_QUERY_STATUS)
        except ws.error as e:
            if e.winerror == _SERVICE_DOES_NOT_EXIST:
                return
            raise _service_error(e, name, "删除") from e
        try:
            try:
                ws.ControlService(handle, ws.SERVICE_CONTROL_STOP)
            except ws.error as e:
                if e.winerror != _SERVICE_NOT_ACTIVE:
                    log.warning("stopping %s before delete failed: %s", name, _describe(e))
            try:
                ws.DeleteService(handle)
            except ws.error as e:
                if e.winerror != _SERVICE_MARKED_FOR_DELETE:
                    raise _service_error(e, name, "删除") from e
        finally:
            ws.CloseServiceHandle(handle)
        log.info("service %s deleted", name)


def _describe(e: Any) -> str:
    return f"{getattr(e, 'strerror', e)}（{getattr(e, 'winerror', '?')}）"


def _service_error(e: Any, name: str, verb: str) -> ServiceError:
    code = getattr(e, "winerror", None)
    if code == _ACCESS_DENIED:
        return ElevationRequiredError(f"{verb}服务 {name} 需要管理员权限")
    if code == _SERVICE_DOES_NOT_EXIST:
        return ServiceError(f"服务 {name} 不存在（可能已被删除）")
    return ServiceError(f"{verb}服务 {name} 失败：{_describe(e)}")


def default_ops() -> ServiceOps:
    return WindowsServiceOps() if sys.platform == "win32" else UnsupportedServiceOps()


def current_user_sid() -> str | None:
    """The SID of this process's user (string form); None off Windows."""
    if sys.platform != "win32":
        return None
    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try:
        user, _attrs = win32security.GetTokenInformation(token, win32security.TokenUser)
    finally:
        win32api.CloseHandle(token)
    return win32security.ConvertSidToStringSid(user)
