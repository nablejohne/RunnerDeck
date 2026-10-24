"""core/service.py (docs/m4-design.md §2.2)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from runnerdeck.core import service
from runnerdeck.core.errors import ElevationRequiredError, InvalidInputError, ServiceError
from runnerdeck.core.service import ServiceInfo, ServiceState

NAME = "actions.runner.octo-demo.box-demo"


def test_check_service_name() -> None:
    assert service.check_service_name(NAME) == NAME
    for bad in ("", "EventLog", "actions.runner.", "actions.runner.a b", "actions.runner.a\\b"):
        with pytest.raises(InvalidInputError):
            service.check_service_name(bad)


def test_read_service_name(tmp_path: Path) -> None:
    assert service.read_service_name(tmp_path) is None
    (tmp_path / ".service").write_bytes(b"\xef\xbb\xbf" + NAME.encode() + b"\r\n")
    assert service.read_service_name(tmp_path) == NAME
    (tmp_path / ".service").write_text("not a runner service", encoding="utf-8")
    assert service.read_service_name(tmp_path) is None
    (tmp_path / ".service").write_bytes(b"\xff\xfe\x00")
    assert service.read_service_name(tmp_path) is None


class Scripted:
    def __init__(self, *states: ServiceState) -> None:
        self.states = list(states)

    def query(self, name: str) -> ServiceInfo:
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return ServiceInfo(name, state)


def test_wait_for_reaches_a_state() -> None:
    ops = Scripted(ServiceState.START_PENDING, ServiceState.START_PENDING, ServiceState.RUNNING)
    slept: list[float] = []
    info = service.wait_for(
        ops, NAME, (ServiceState.RUNNING,), timeout_s=10, sleep=slept.append, clock=lambda: 0.0
    )  # type: ignore[arg-type]
    assert info.state is ServiceState.RUNNING and len(slept) == 2


def test_wait_for_stops_on_missing_and_on_timeout() -> None:
    info = service.wait_for(
        Scripted(ServiceState.MISSING), NAME, (ServiceState.RUNNING,), timeout_s=10,
        sleep=lambda _s: None,
    )  # type: ignore[arg-type]  # fmt: skip
    assert info.state is ServiceState.MISSING
    ticks = iter(range(100))
    info = service.wait_for(
        Scripted(ServiceState.STOP_PENDING), NAME, (ServiceState.STOPPED,), timeout_s=3,
        sleep=lambda _s: None, clock=lambda: float(next(ticks)),
    )  # type: ignore[arg-type]  # fmt: skip
    assert info.state is ServiceState.STOP_PENDING


def test_unsupported_ops() -> None:
    ops = service.UnsupportedServiceOps()
    assert ops.query(NAME).state is ServiceState.UNKNOWN
    for method in (ops.start, ops.stop, ops.delete):
        with pytest.raises(InvalidInputError, match="Windows"):
            method(NAME)
    with pytest.raises(InvalidInputError):
        ops.grant_control(NAME, "S-1-5-18")


@pytest.mark.parametrize(
    ("code", "kind", "text"),
    [
        (5, ElevationRequiredError, "管理员"),
        (1060, ServiceError, "不存在"),
        (2, ServiceError, "失败"),
    ],
)
def test_service_error_mapping(code: int, kind: type[Exception], text: str) -> None:
    err = service._service_error(SimpleNamespace(winerror=code, strerror="x"), NAME, "启动")
    assert type(err) is kind and text in str(err)


@pytest.mark.skipif(sys.platform != "win32", reason="service control manager")
def test_windows_query_real_services() -> None:
    ops = service.WindowsServiceOps()
    assert ops.query("actions.runner.runnerdeck-test-does-not-exist").state is ServiceState.MISSING
    sid = service.current_user_sid()
    assert sid is not None and sid.startswith("S-1-5-")
