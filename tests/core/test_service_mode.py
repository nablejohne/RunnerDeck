"""Service-mode runners: elevate / elevated / manager (docs/m4-design.md §2).

The UAC prompt is the only thing faked at the boundary: the launcher runs core/elevated.main
in-process with fake services and the fake config.cmd from test_manager.
"""

from __future__ import annotations

import itertools
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_manager import (  # tests/core is on sys.path (rootdir-relative imports)
    AGENT_ID,
    DEFAULT_NAME,
    OWNER,
    REPO,
    Env,
    FakeConfigCmd,
    _key,
    _no_inherited_proxy,  # noqa: F401 - autouse fixture
    _propagate_logs,  # noqa: F401 - autouse fixture
    env,  # noqa: F401 - fixture
    make_runner_dir,
    set_proxy,
)

from runnerdeck.core import elevate, elevated
from runnerdeck.core import manager as manager_mod
from runnerdeck.core.elevate import RESULT_SUFFIX, ElevatedResult, Elevator
from runnerdeck.core.errors import (
    ElevationCancelledError,
    ElevationRequiredError,
    InvalidInputError,
    ProcessError,
    ServiceError,
)
from runnerdeck.core.manager import RemoteRemoval, RunnerManager, ServiceInstallError
from runnerdeck.core.models import LocalState, RunnerMode
from runnerdeck.core.service import ServiceInfo, ServiceState

SID = "S-1-5-21-1-2-3-1001"
REAL_UNDER_PROFILE = manager_mod._under_profile
DENIED_LINE = (
    "[2026-10-24 06:04:42Z ERR  Terminal] WRITE ERROR: An error occurred: Permission to read "
    "the directory contents is required for 'D:\\runners\\demo' and each directory up the "
    "hierarchy."
)


def service_name(name: str = DEFAULT_NAME) -> str:
    return f"actions.runner.{OWNER}-{REPO}.{name}"


class FakeServices:
    def __init__(self) -> None:
        self.services: dict[str, ServiceInfo] = {}
        self.denied: set[str] = set()
        self.granted: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []

    def install(self, name: str, *, exit_code: int = 0) -> None:
        state = ServiceState.STOPPED if exit_code else ServiceState.RUNNING
        self.services[name] = ServiceInfo(name, state, pid=None, exit_code=exit_code)

    def query(self, name: str) -> ServiceInfo:
        return self.services.get(name, ServiceInfo(name, ServiceState.MISSING))

    def _set(self, name: str, state: ServiceState, verb: str) -> None:
        self.calls.append((verb, name))
        if name in self.denied:
            raise ElevationRequiredError(f"{verb}服务 {name} 需要管理员权限")
        if name not in self.services:
            raise ServiceError(f"服务 {name} 不存在")
        self.services[name] = ServiceInfo(name, state)

    def start(self, name: str) -> None:
        self._set(name, ServiceState.RUNNING, "start")

    def stop(self, name: str) -> None:
        self._set(name, ServiceState.STOPPED, "stop")

    def grant_control(self, name: str, sid: str) -> None:
        self.granted.append((name, sid))
        self.denied.discard(name)

    def delete(self, name: str) -> None:
        self.calls.append(("delete", name))
        self.services.pop(name, None)


class ServiceConfigCmd(FakeConfigCmd):
    """config.cmd that also installs / removes the service for --runasservice."""

    def __init__(self, base: FakeConfigCmd, services: FakeServices) -> None:
        self.__dict__.update(base.__dict__)
        self.services = services
        self.service_exit = False  # the Listener quits right after the install
        self.service_fail = False  # config.cmd itself fails with --runasservice

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        cwd = Path(kwargs["cwd"])
        if "remove" in args and (cwd / ".service").is_file():
            self.services.delete((cwd / ".service").read_text(encoding="utf-8").strip())
            (cwd / ".service").unlink()
        if "--runasservice" in args and self.service_fail:
            self.calls.append((list(args), kwargs))
            return subprocess.CompletedProcess(args, 1, b"Needs Administrator privileges\r\n")
        result = super().__call__(args, **kwargs)
        if "--runasservice" in args and result.returncode == 0:
            name = service_name(args[args.index("--name") + 1])
            (cwd / ".service").write_text(name, encoding="utf-8")
            self.services.install(name, exit_code=1 if self.service_exit else 0)
            if self.service_exit:
                diag_dir = cwd / "_diag"
                diag_dir.mkdir(exist_ok=True)
                (diag_dir / "Runner_1.log").write_text(DENIED_LINE + "\n", encoding="utf-8")
        return result


class Launcher:
    """Stands in for the UAC prompt: runs the helper in-process (or 'No')."""

    def __init__(self, directory: Path, services: FakeServices, run: Any) -> None:
        self.directory = directory
        self.services = services
        self.run = run
        self.cancel = False
        self.ops: list[str] = []
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, executable: str, parameters: str, timeout_s: float) -> int:
        assert parameters.split()[-1].endswith(".json")
        if self.cancel:
            raise ElevationCancelledError("已取消：这个操作需要管理员授权")
        requests = [p for p in self.directory.glob("*.json") if RESULT_SUFFIX not in p.name]
        assert len(requests) == 1, requests
        data = json.loads(requests[0].read_text(encoding="utf-8"))
        self.ops.append(data["op"])
        self.payloads.append(data)
        ticks = itertools.count()
        return elevated.main(
            str(requests[0]),
            services=self.services,
            run=self.run,
            sleep=lambda _s: None,
            clock=lambda: float(next(ticks)),
        )


class SvcEnv:
    def __init__(self, env: Env) -> None:  # noqa: F811 - not the fixture
        self.env = env
        self.services = FakeServices()
        self.cmd = ServiceConfigCmd(env.cmd, self.services)
        self.launcher = Launcher(env.tmp / "elevate", self.services, self.cmd)
        self.manager = RunnerManager(
            env.store,
            env.factory,
            cache=env.cache,  # type: ignore[arg-type]
            hostname="BUILDBOX",
            installer_run=self.cmd,
            find_processes=env.finder,
            clock=lambda: env.manager._clock(),
            stop_marks=env.marks,
            scan_processes=env.scanner,
            stop_external=env.stop_external,
            services=self.services,
            elevator=Elevator(launcher=self.launcher, directory=env.tmp / "elevate"),
            user_sid=lambda: SID,
        )

    def add(self, **kw: Any) -> Any:
        return self.manager.add_runner(
            self.manager.plan_add(OWNER, REPO, mode=RunnerMode.SERVICE, **kw)
        )

    def remote_ids(self) -> list[int]:
        return [r.id for r in self.env.client.runners.get(_key(OWNER, REPO), [])]

    def leftovers(self) -> list[Path]:
        return list((self.env.tmp / "elevate").glob("*"))


@pytest.fixture
def svc(env: Env, monkeypatch: pytest.MonkeyPatch) -> SvcEnv:  # noqa: F811 - imported fixture
    # pytest's tmp_path is under the profile on Windows (AppData\\Local\\Temp), which service
    # mode refuses; test_plan_service_under_the_profile_is_refused checks the real rule.
    monkeypatch.setattr(manager_mod, "_under_profile", lambda _path: False)
    return SvcEnv(env)


# --- add ---------------------------------------------------------------------------------


def test_add_service_runner(svc: SvcEnv) -> None:
    set_proxy(svc.env, "http://proxy:8080")
    seen_env: list[str] = []
    svc.cmd.on_configure = lambda: seen_env.append(
        (svc.env.root / REPO / ".env").read_text(encoding="utf-8")
    )
    record = svc.add(runner_env={"PSExecutionPolicyPreference": "RemoteSigned"})
    assert record.mode is RunnerMode.SERVICE
    assert record.service_name == service_name()
    assert record.github_runner_id == AGENT_ID and not record.auto_start
    assert svc.services.query(service_name()).state is ServiceState.RUNNING
    assert svc.services.granted == [(service_name(), SID)]
    # .env (policy + proxy) was in place before config.cmd ran: the service starts at once
    assert seen_env and "https_proxy=http://proxy:8080" in seen_env[0]
    args, kwargs = svc.cmd.calls[-1]
    assert "--runasservice" in args
    assert kwargs["env"]["https_proxy"] == "http://proxy:8080"
    assert svc.launcher.ops == ["install_service"]
    assert svc.leftovers() == []  # request and result files are gone


def test_add_service_runner_that_quits_is_rolled_back(svc: SvcEnv) -> None:
    svc.cmd.service_exit = True
    with pytest.raises(ServiceInstallError, match="Permission to read") as info:
        svc.add()
    assert not info.value.result.registered and not info.value.result.service_exists
    assert svc.remote_ids() == []  # the helper ran config.cmd remove itself
    assert svc.services.services == {}
    assert not (svc.env.root / REPO).exists() and svc.env.config().runners == []


def test_add_service_runner_config_failure(svc: SvcEnv) -> None:
    svc.cmd.service_fail = True
    with pytest.raises(ServiceInstallError, match="安装服务失败"):
        svc.add()
    assert svc.remote_ids() == [] and not (svc.env.root / REPO).exists()


def test_add_service_runner_uac_cancelled(svc: SvcEnv) -> None:
    svc.launcher.cancel = True
    with pytest.raises(ElevationCancelledError):
        svc.add()
    assert svc.remote_ids() == [] and not (svc.env.root / REPO).exists()
    assert svc.env.config().runners == [] and svc.leftovers() == []


def test_plan_service_under_the_profile_is_refused(
    svc: SvcEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_mod, "_under_profile", REAL_UNDER_PROFILE)
    monkeypatch.setenv("USERPROFILE", str(svc.env.tmp))
    with pytest.raises(InvalidInputError, match="NETWORK SERVICE"):
        svc.manager.plan_add(OWNER, REPO, mode=RunnerMode.SERVICE)
    assert svc.manager.plan_add(OWNER, REPO).mode is RunnerMode.PROCESS  # process mode is fine


# --- start / stop / grant ----------------------------------------------------------------


def test_start_stop_with_granted_rights(svc: SvcEnv) -> None:
    record = svc.add()
    info = svc.manager.stop_service(record.id)
    assert info.state is ServiceState.STOPPED
    mark = svc.env.marks.get(Path(record.path), now=svc.env.manager._clock())
    assert mark is not None and not mark.forced
    assert svc.manager.start_service(record.id).state is ServiceState.RUNNING
    assert svc.launcher.ops == ["install_service"]  # no UAC for start / stop


def test_start_stop_without_rights_elevates_once_each(svc: SvcEnv) -> None:
    record = svc.add()
    svc.services.denied.add(service_name())
    with pytest.raises(ElevationRequiredError):
        svc.manager.stop_service(record.id, elevate=False)
    svc.services.denied.add(service_name())
    # the fake helper's services are not denied: ElevationRequiredError only hits us
    helper_services = FakeServices()
    helper_services.services = svc.services.services
    svc.launcher.services = helper_services
    assert svc.manager.stop_service(record.id).state is ServiceState.STOPPED
    assert svc.launcher.ops[-1] == "control_service"
    assert svc.launcher.payloads[-1]["action"] == "stop"


def test_start_service_that_quits_reports_the_listener_error(svc: SvcEnv) -> None:
    record = svc.add()
    svc.manager.stop_service(record.id)
    (Path(record.path) / "_diag").mkdir(exist_ok=True)
    (Path(record.path) / "_diag" / "Runner_9.log").write_text(DENIED_LINE + "\n", encoding="utf-8")
    original = svc.services.start

    def start_then_quit(name: str) -> None:
        original(name)
        svc.services.services[name] = ServiceInfo(name, ServiceState.STOPPED, exit_code=1)

    svc.services.start = start_then_quit  # type: ignore[method-assign]
    with pytest.raises(ServiceError, match="启动后退出.*Permission to read"):
        svc.manager.start_service(record.id)


def test_grant_service_control(svc: SvcEnv) -> None:
    record = svc.add()
    svc.services.granted.clear()
    svc.manager.grant_service_control(record.id)
    assert svc.services.granted == [(service_name(), SID)]
    assert svc.launcher.ops[-1] == "grant_control"


def test_service_calls_on_a_process_record_are_refused(svc: SvcEnv) -> None:
    record = svc.env.seed(path=str(make_runner_dir(svc.env.root / "p")))
    with pytest.raises(InvalidInputError, match="不是服务模式"):
        svc.manager.start_service(record.id)


def test_process_for_refuses_a_service_record(svc: SvcEnv) -> None:
    record = svc.add()
    with pytest.raises(InvalidInputError, match="Windows 服务"):
        svc.manager.process_for(record)


# --- status ------------------------------------------------------------------------------


def _local(svc: SvcEnv, record_id: str) -> Any:
    (row,) = [r for r in svc.manager.statuses(remote=False) if r.record.id == record_id]
    return row.local


def test_service_snapshot(svc: SvcEnv) -> None:
    record = svc.add()
    local = _local(svc, record.id)
    assert (local.state, local.running, local.external) == (LocalState.IDLE, True, False)
    svc.manager.stop_service(record.id)
    assert _local(svc, record.id).state is LocalState.STOPPED
    name = service_name()
    svc.services.services[name] = ServiceInfo(name, ServiceState.STOPPED, exit_code=1)
    (Path(record.path) / "_diag").mkdir(exist_ok=True)
    (Path(record.path) / "_diag" / "Runner_9.log").write_text(DENIED_LINE + "\n", encoding="utf-8")
    local = _local(svc, record.id)
    assert local.state is LocalState.ERROR and "Permission to read" in local.last_error
    svc.services.services.pop(name)
    local = _local(svc, record.id)
    assert local.state is LocalState.ERROR and "不存在" in local.last_error


# --- remove ------------------------------------------------------------------------------


def test_remove_service_runner(svc: SvcEnv) -> None:
    record = svc.add()
    result = svc.manager.remove_runner(record.id)
    assert result.remote is RemoteRemoval.UNCONFIGURED
    assert svc.services.services == {} and svc.remote_ids() == []
    assert not Path(record.path).exists() and svc.env.config().runners == []
    assert svc.launcher.ops[-1] == "remove_service"
    assert svc.leftovers() == []


def test_remove_service_runner_falls_back_to_the_api(svc: SvcEnv) -> None:
    record = svc.add()
    svc.cmd.remove_mode = "fail"
    result = svc.manager.remove_runner(record.id)
    assert result.remote is RemoteRemoval.API_DELETED
    assert svc.services.services == {}  # the helper still deleted the service
    assert any("config.cmd remove 失败" in w for w in result.warnings)


def test_remove_service_runner_uac_cancelled_changes_nothing(svc: SvcEnv) -> None:
    record = svc.add()
    svc.launcher.cancel = True
    with pytest.raises(ElevationCancelledError):
        svc.manager.remove_runner(record.id)
    assert Path(record.path).is_dir() and svc.env.config().runners == [record]
    assert svc.remote_ids() == [AGENT_ID]


# --- convert / import --------------------------------------------------------------------


def test_convert_to_service(svc: SvcEnv) -> None:
    runner_dir = make_runner_dir(svc.env.root / "conv", agent_id=7)
    svc.env.client.runners.setdefault(_key(OWNER, REPO), []).append(
        __import__("test_manager").remote(7, DEFAULT_NAME)
    )
    record = svc.env.seed(path=str(runner_dir), github_runner_id=7, auto_start=True)
    updated = svc.manager.convert_to_service(record.id)
    assert updated.mode is RunnerMode.SERVICE and updated.service_name == service_name()
    assert updated.github_runner_id != 7 and not updated.auto_start
    assert svc.remote_ids() == [updated.github_runner_id]
    assert svc.services.granted == [(service_name(), SID)]


def test_convert_to_service_failure_registers_the_process_runner_again(svc: SvcEnv) -> None:
    runner_dir = make_runner_dir(svc.env.root / "conv", agent_id=7)
    svc.env.client.runners.setdefault(_key(OWNER, REPO), []).append(
        __import__("test_manager").remote(7, DEFAULT_NAME)
    )
    record = svc.env.seed(path=str(runner_dir), github_runner_id=7)
    svc.cmd.service_exit = True
    with pytest.raises(ServiceInstallError, match="转为服务失败") as info:
        svc.manager.convert_to_service(record.id)
    assert info.value.result.registered
    (kept,) = svc.env.config().runners
    assert kept.mode is RunnerMode.PROCESS and kept.github_runner_id not in (None, 7)
    assert svc.remote_ids() == [kept.github_runner_id] and svc.services.services == {}


def test_convert_to_service_refuses_a_running_or_service_runner(svc: SvcEnv) -> None:
    record = svc.add()
    with pytest.raises(InvalidInputError, match="已经是服务模式"):
        svc.manager.convert_to_service(record.id)
    other = svc.env.seed(name="p2", path=str(make_runner_dir(svc.env.root / "p2", agent_id=9)))
    svc.env.running[other.path] = [SimpleNamespace(pid=123)]
    with pytest.raises(ProcessError, match="正在运行"):
        svc.manager.convert_to_service(other.id)


def test_import_service_dir(svc: SvcEnv) -> None:
    runner_dir = make_runner_dir(svc.env.root / "imp")
    (runner_dir / ".service").write_text(service_name(), encoding="utf-8")
    svc.env.client.runners.setdefault(_key(OWNER, REPO), []).append(
        __import__("test_manager").remote(AGENT_ID, DEFAULT_NAME)
    )
    record = svc.manager.import_runner(runner_dir, auto_start=True)
    assert record.mode is RunnerMode.SERVICE and record.service_name == service_name()
    assert not record.auto_start


# --- the elevated helper on its own ------------------------------------------------------


def run_helper(
    tmp_path: Path, request: Any, services: FakeServices | None = None
) -> tuple[int, Any]:
    path = tmp_path / "req.json"
    path.write_text(json.dumps(request) if not isinstance(request, str) else request, "utf-8")
    code = elevated.main(str(path), services=services or FakeServices(), run=lambda *a, **k: None)
    assert not path.exists()  # deleted right after reading (it may hold tokens)
    result = tmp_path / ("req" + RESULT_SUFFIX)
    return code, ElevatedResult.from_json(result.read_text("utf-8")) if result.exists() else None


@pytest.mark.parametrize(
    ("request_", "message"),
    [
        ("not json", "内部错误"),
        ({"version": 99, "op": "grant_control"}, "格式不对"),
        ({"version": 1, "op": "format_disk"}, "不支持的管理员操作"),
        (
            {"version": 1, "op": "grant_control", "service_name": "EventLog", "grant_sid": SID},
            "服务名",
        ),
        (
            {"version": 1, "op": "grant_control", "service_name": service_name(), "grant_sid": "x"},
            "SID",
        ),
        ({"version": 1, "op": "install_service", "runner_dir": "relative"}, "绝对路径"),
        (
            {
                "version": 1,
                "op": "control_service",
                "service_name": service_name(),
                "action": "pause",
            },
            "不支持",
        ),
    ],
)
def test_helper_rejects_bad_requests(tmp_path: Path, request_: Any, message: str) -> None:
    code, result = run_helper(tmp_path, request_)
    assert code == 1 and result is not None and not result.ok
    assert message in (result.error or "")


def test_helper_control_and_grant(tmp_path: Path) -> None:
    services = FakeServices()
    services.install(service_name())
    request = {
        "version": 1,
        "op": "control_service",
        "service_name": service_name(),
        "action": "stop",
    }
    code, result = run_helper(tmp_path, request, services)
    assert code == 0 and result.ok
    assert services.query(service_name()).state is ServiceState.STOPPED
    request = {
        "version": 1,
        "op": "grant_control",
        "service_name": service_name(),
        "grant_sid": SID,
    }
    code, result = run_helper(tmp_path, request, services)
    assert code == 0 and services.granted == [(service_name(), SID)]


def test_helper_remove_with_a_lost_dir_only_deletes_the_service(tmp_path: Path) -> None:
    services = FakeServices()
    services.install(service_name())
    request = {
        "version": 1, "op": "remove_service", "service_name": service_name(),
        "runner_dir": str(tmp_path / "gone"),
    }  # fmt: skip
    code, result = run_helper(tmp_path, request, services)
    assert code == 0 and result.ok and result.registered and not result.service_exists


# --- elevate (the unelevated side) -------------------------------------------------------


def test_elevated_result_round_trip() -> None:
    result = ElevatedResult(ok=True, service_name="s", agent_id=3, warnings=("w",))
    assert ElevatedResult.from_json(result.to_json()) == result
    with pytest.raises(ValueError):
        ElevatedResult.from_json("[]")


def test_elevator_without_result_file(tmp_path: Path) -> None:
    elevator = Elevator(launcher=lambda exe, params, t: 1, directory=tmp_path)
    with pytest.raises(ServiceError, match="没有返回结果"):
        elevator("grant_control", {})
    assert list(tmp_path.iterdir()) == []


def test_helper_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(elevate.sys, "executable", str(tmp_path / "pythonw.exe"))
    exe, args = elevate.helper_command()
    assert Path(exe).name == "python.exe" and args == ["-m", "runnerdeck.core.cli", "elevated"]
    monkeypatch.setattr(elevate.sys, "frozen", True, raising=False)
    monkeypatch.setattr(elevate.sys, "executable", str(tmp_path / "RunnerDeck.exe"))
    exe, args = elevate.helper_command()
    assert Path(exe) == tmp_path / "runnerdeck-cli.exe" and args == ["elevated"]


def test_shell_execute_runas_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(elevate.sys, "platform", "darwin")
    with pytest.raises(ServiceError, match="Windows"):
        elevate.shell_execute_runas("x.exe", "", 1)


def test_last_arg_of_a_windows_command_line() -> None:
    line = subprocess.list2cmdline(
        ["-m", "runnerdeck.core.cli", "elevated", r"C:\Users\A B\x.json"]
    )
    assert elevate._last_arg(line) == r"C:\Users\A B\x.json"
    assert elevate._last_arg(r"elevated D:\x\y.json") == r"D:\x\y.json"


def test_default_launcher_runs_in_process_when_elevated(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(elevate, "is_elevated", lambda: True)
    monkeypatch.setattr(elevated, "main", lambda path: seen.append(path) or 0)
    monkeypatch.setattr(elevate, "shell_execute_runas", lambda *a: pytest.fail("prompted"))
    assert elevate.default_launcher("x.exe", r'elevated "C:\a b\r.json"', 5) == 0
    assert seen == [r"C:\a b\r.json"]


# --- review fixes: the helper's input checks, timeouts --------------------------------------


@pytest.mark.parametrize(
    ("request_", "message"),
    [
        ({"version": 1, "op": "install_service", "runner_dir": "\\\\server\\share\\r"}, "本机"),
        ({"version": 1, "op": "install_service", "runner_dir": "//server/share/r"}, "本机"),
        (
            {
                "version": 1,
                "op": "grant_control",
                "service_name": service_name(),
                "grant_sid": "S-1-1-0",
            },
            "SID",
        ),
        (
            {
                "version": 1,
                "op": "grant_control",
                "service_name": service_name(),
                "grant_sid": "S-1-5-11",
            },
            "SID",
        ),
    ],
)
def test_helper_rejects_remote_dirs_and_group_sids(
    tmp_path: Path, request_: Any, message: str
) -> None:
    code, result = run_helper(tmp_path, request_)
    assert code == 1 and message in (result.error or "")


def test_helper_rejects_an_old_request(tmp_path: Path) -> None:
    import os
    import time

    path = tmp_path / "req.json"
    path.write_text(json.dumps({"version": 1, "op": "grant_control"}), "utf-8")
    old = time.time() - 3600
    os.utime(path, (old, old))
    assert elevated.main(str(path), services=FakeServices()) == 1
    result = ElevatedResult.from_json((tmp_path / ("req" + RESULT_SUFFIX)).read_text("utf-8"))
    assert "过期" in (result.error or "")


def test_helper_remove_checks_the_service_name(tmp_path: Path) -> None:
    runner_dir = make_runner_dir(tmp_path / "r")
    (runner_dir / ".service").write_text(service_name("other"), encoding="utf-8")
    request = {
        "version": 1, "op": "remove_service", "service_name": service_name(),
        "runner_dir": str(runner_dir), "remove_token": "FAKEREMOVETOKEN000000000002",
    }  # fmt: skip
    code, result = run_helper(tmp_path, request)
    assert code == 1 and "不是" in (result.error or "")


def test_add_service_timeout_rolls_back_as_registered(svc: SvcEnv) -> None:
    from runnerdeck.core.errors import ElevationTimeoutError

    def slow(_exe: str, _params: str, _timeout: float) -> int:
        svc.cmd(  # the helper got as far as registering before we gave up on it
            [
                *[
                    "cmd.exe",
                    "/c",
                    "config.cmd",
                    "--unattended",
                    "--url",
                    f"https://github.com/{OWNER}/{REPO}",
                ],
                "--token",
                "FAKEREGTOKEN0000000000000001",
                "--name",
                DEFAULT_NAME,
                "--work",
                "_work",
            ],
            cwd=svc.env.root / REPO,
        )
        raise ElevationTimeoutError("管理员操作 300 秒内没有完成")

    svc.manager._elevator = Elevator(launcher=slow, directory=svc.env.tmp / "elevate")
    with pytest.raises(ElevationTimeoutError):
        svc.add()
    assert svc.remote_ids() == []  # rolled back as "may be registered"
    assert not (svc.env.root / REPO).exists()
