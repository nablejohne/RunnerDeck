"""Tests for runnerdeck.core.cli.

main() runs with injected streams, prompts, a recording GitHub client and a real RunnerManager
wired to fakes (config.cmd stand-in, release cache, process finder), so whole commands run end
to end without network, keyring or Windows. `start` uses fake RunnerProcess objects, plus one
real RunnerProcess fed by a fake Popen. `.runner` contents are SYNTHETIC samples (field names
from docs/m1-design.md §6); every token and PAT here is an obviously fake value.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psutil
import pytest
from pydantic import SecretStr

from runnerdeck.core import cli, secrets, winproc
from runnerdeck.core.cli import (
    LINGER_HINT,
    RECONNECTING_HINT,
    CliDeps,
    Console,
    build_parser,
    main,
    permission_hints,
    render_table,
)
from runnerdeck.core.errors import (
    AuthError,
    GitHubError,
    NotFoundError,
    PermissionDeniedError,
    ProcessError,
)
from runnerdeck.core.github_client import GitHubClient
from runnerdeck.core.manager import RunnerManager
from runnerdeck.core.models import (
    AppConfig,
    GitHubUser,
    LocalState,
    RemoteRunner,
    Repo,
    RunnerRecord,
    RunnerToken,
)
from runnerdeck.core.process import StopMethod, StopResult
from runnerdeck.core.redact import register_secret
from runnerdeck.core.store import ConfigStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FAKE_RUNNER = Path(__file__).resolve().parent / "fake_runner"
CONFIG_OK = (FIXTURES / "config" / "config_ok.txt").read_bytes()
REMOVE_OK = (FIXTURES / "config" / "remove_ok.txt").read_bytes()
JOB_OUTPUT = (FIXTURES / "stdout" / "job_then_ctrl_break.txt").read_bytes()

OWNER, REPO = "octo", "demo"
URL = f"https://github.com/{OWNER}/{REPO}"
HOST = "BUILDBOX"
DEFAULT_NAME = f"{HOST}-{REPO}"
VERSION = "2.338.0"
AGENT_ID = 4242
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)

FAKE_PAT = "github_pat_" + "0FAKE0" * 10  # right shape, obviously not real
CLASSIC_PAT = "ghp_" + "FAKE0000" * 5
REG_TOKEN = "FAKEREGTOKEN0000000000000001"
REMOVE_TOKEN = "FAKEREMOVETOKEN000000000002"


# --- logging isolation ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def restore_runnerdeck_logger() -> Iterator[None]:
    """main() calls setup_logging, which sets propagate=False on 'runnerdeck' for good."""
    logger = logging.getLogger("runnerdeck")
    saved = (list(logger.handlers), logger.level, logger.propagate)
    yield
    for handler in list(logger.handlers):
        if handler not in saved[0]:
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(saved[1])
    logger.propagate = saved[2]


# --- fakes ----------------------------------------------------------------------------------


def _key(owner: str, repo: str) -> tuple[str, str]:
    return owner.casefold(), repo.casefold()


def remote(id: int, name: str, status: str = "offline", *, busy: bool = False) -> RemoteRunner:
    labels = [{"name": "self-hosted"}, {"name": "Windows"}, {"name": "X64"}, {"name": "gpu"}]
    return RemoteRunner(id=id, name=name, status=status, busy=busy, labels=labels)


def repo_model(name: str, *, private: bool, owner: str = OWNER, **kw: Any) -> Repo:
    return Repo(
        name=name,
        full_name=f"{owner}/{name}",
        owner={"login": owner},
        private=private,
        html_url=f"https://github.com/{owner}/{name}",
        **kw,
    )


class FakeClient:
    """Duck-typed GitHubClient: records calls, per-method failures via `failures`."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.user = GitHubUser(
            login=OWNER,
            id=1,
            token_kind="fine-grained",
            token_expires_at=NOW + timedelta(days=40),
        )
        self.repo_list: list[Repo] = []
        self.repos: dict[tuple[str, str], Repo] = {}
        self.runners: dict[tuple[str, str], list[RemoteRunner]] = {}
        self.list_failures: dict[tuple[str, str], BaseException] = {}
        self.failures: dict[str, BaseException] = {}
        self.release_json: dict[str, Any] = {}
        self.closed = 0

    def _call(self, name: str, *args: Any) -> None:
        self.calls.append((name, *args))
        exc = self.failures.get(name)
        if exc is not None:
            raise exc

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def close(self) -> None:
        self.closed += 1

    def get_user(self) -> GitHubUser:
        self._call("get_user")
        return self.user

    def list_repos(self) -> list[Repo]:
        self._call("list_repos")
        return list(self.repo_list)

    def get_repo(self, owner: str, repo: str) -> Repo:
        self._call("get_repo", owner, repo)
        try:
            return self.repos[_key(owner, repo)]
        except KeyError:
            raise NotFoundError("仓库不存在或 PAT 没有访问权限", status=404) from None

    def list_runners(self, owner: str, repo: str) -> list[RemoteRunner]:
        self._call("list_runners", owner, repo)
        exc = self.list_failures.get(_key(owner, repo))
        if exc is not None:
            raise exc
        return list(self.runners.get(_key(owner, repo), []))

    def get_runner(self, owner: str, repo: str, runner_id: int) -> RemoteRunner | None:
        self._call("get_runner", owner, repo, runner_id)
        return next((r for r in self.runners.get(_key(owner, repo), []) if r.id == runner_id), None)

    def create_registration_token(self, owner: str, repo: str) -> RunnerToken:
        self._call("create_registration_token", owner, repo)
        return RunnerToken(token=SecretStr(REG_TOKEN), expires_at=NOW + timedelta(hours=1))

    def create_remove_token(self, owner: str, repo: str) -> RunnerToken:
        self._call("create_remove_token", owner, repo)
        return RunnerToken(token=SecretStr(REMOVE_TOKEN), expires_at=NOW + timedelta(hours=1))

    def delete_runner(self, owner: str, repo: str, runner_id: int) -> bool:
        self._call("delete_runner", owner, repo, runner_id)
        runners = self.runners.get(_key(owner, repo), [])
        for r in runners:
            if r.id == runner_id:
                runners.remove(r)
                return True
        return False

    def get_runner_release(self, version: str | None = None) -> dict[str, Any]:
        self._call("get_runner_release", version)
        return self.release_json


class FakeCache:
    def __init__(self, zip_path: Path) -> None:
        self.zip_path = zip_path

    def ensure(self, client: Any, info: Any, on_progress: Any = None) -> Path:
        if on_progress is not None:
            on_progress(0, 100)
            on_progress(100, 100)
        return self.zip_path

    def prune(self, keep: int = 2) -> list[Path]:
        return []


def runner_json(agent_id: int, name: str, url: str = URL) -> dict[str, Any]:
    """SYNTHETIC `.runner` sample (field names from docs/m1-design.md §6)."""
    return {
        "agentId": agent_id,
        "agentName": name,
        "poolId": 1,
        "poolName": "Default",
        "serverUrl": "https://pipelines.example.invalid/fake/",
        "gitHubUrl": url,
        "workFolder": "_work",
    }


def write_registration(runner_dir: Path, agent_id: int, name: str, url: str = URL) -> None:
    text = "\ufeff" + json.dumps(runner_json(agent_id, name, url))
    (runner_dir / ".runner").write_bytes(text.encode("utf-8"))
    (runner_dir / ".credentials").write_text("{}", encoding="utf-8")  # never read by the app


def make_runner_dir(
    path: Path,
    *,
    agent_id: int | None = AGENT_ID,
    name: str = DEFAULT_NAME,
    url: str = URL,
    credentials: bool = True,
) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.cmd").write_text("@echo fake\r\n", encoding="utf-8")
    (path / "run.cmd").write_text("@echo fake\r\n", encoding="utf-8")
    (path / "_work").mkdir(exist_ok=True)
    (path / "_work" / "keep.txt").write_text("job data", encoding="utf-8")
    if agent_id is not None:
        write_registration(path, agent_id, name, url)
        if not credentials:
            (path / ".credentials").unlink()
    return path


class FakeConfigCmd:
    """Stands in for installer.run_command, behaving like config.cmd (spike §3)."""

    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.calls: list[list[str]] = []
        self.next_agent_id = AGENT_ID

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        args = list(args)
        self.calls.append(args)
        cwd = Path(kwargs["cwd"])
        if "remove" in args:
            runner = cwd / ".runner"
            agent_id = json.loads(runner.read_text(encoding="utf-8-sig"))["agentId"]
            runner.unlink()
            (cwd / ".credentials").unlink()
            for runners in self.client.runners.values():
                runners[:] = [r for r in runners if r.id != agent_id]
            return subprocess.CompletedProcess(args, 0, REMOVE_OK)
        owner, repo = args[args.index("--url") + 1].rstrip("/").split("/")[-2:]
        name = args[args.index("--name") + 1]
        agent_id = self.next_agent_id
        self.next_agent_id += 1
        self.client.runners.setdefault(_key(owner, repo), []).append(remote(agent_id, name))
        write_registration(cwd, agent_id, name, f"https://github.com/{owner}/{repo}")
        return subprocess.CompletedProcess(args, 0, CONFIG_OK)


def build_runner_zip(path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("config.cmd", "@echo fake config\r\n")
        zf.writestr("run.cmd", "@echo fake run\r\n")
        zf.writestr("bin/Runner.Listener.dll", b"fake")
    data = buf.getvalue()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def release_json(data: bytes) -> dict[str, Any]:
    name = f"actions-runner-win-x64-{VERSION}.zip"
    return {
        "tag_name": f"v{VERSION}",
        "body": "",
        "assets": [
            {
                "name": name,
                "browser_download_url": f"https://example.invalid/{name}",
                "size": len(data),
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            }
        ],
    }


# --- fake processes for `start` ---------------------------------------------------------------


@dataclass
class Script:
    """What a FakeProcess does: output on start, how it ends, what stop() returns."""

    lines: list[str] = field(default_factory=list)
    states: list[tuple[LocalState, str | None]] = field(default_factory=list)
    start_error: BaseException | None = None
    exit_code: int | None = None  # set: the runner exits on its own right after start
    last_error: str | None = None
    stop_results: list[StopResult | BaseException] = field(default_factory=list)


class FakeProcess:
    _next_pid = 1000

    def __init__(self, record: RunnerRecord, script: Script, on_line: Any, on_state: Any) -> None:
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid
        self.record = record
        self.script = script
        self.on_line = on_line
        self.on_state = on_state
        self.running = False
        self.exit_code: int | None = None
        self.last_error: str | None = None
        self.stop_calls: list[float] = []

    def start(self) -> None:
        if self.script.start_error is not None:
            raise self.script.start_error
        self.running = True
        for line in self.script.lines:
            self.on_line(line)
        for state, job in self.script.states:
            self.on_state(state, job)
        if self.script.exit_code is not None:
            self.running = False
            self.exit_code = self.script.exit_code
            self.last_error = self.script.last_error

    def is_running(self) -> bool:
        return self.running

    def stop(self, timeout: float = 15.0) -> StopResult:
        self.stop_calls.append(timeout)
        result = (
            self.script.stop_results.pop(0)
            if self.script.stop_results
            else StopResult(True, StopMethod.CTRL_BREAK, 0, [])
        )
        if isinstance(result, BaseException):
            raise result
        self.running = False
        self.exit_code = result.exit_code
        return result


class FakeStartManager:
    """Only what `start` needs from RunnerManager."""

    def __init__(self, scripts: dict[str, Script]) -> None:
        self.scripts = scripts
        self.procs: dict[str, FakeProcess] = {}

    def process_for(self, record: RunnerRecord, *, on_line: Any, on_state: Any) -> FakeProcess:
        proc = FakeProcess(record, self.scripts[record.name], on_line, on_state)
        self.procs[record.name] = proc
        return proc


class FakePopenProc:
    def __init__(self, data: bytes, rc: int) -> None:
        self.pid = 4321
        self.stdout = io.BytesIO(data)
        self.returncode: int | None = None
        self._rc = rc

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self._rc
        return self._rc

    def poll(self) -> int | None:
        return self.returncode


class FakePopen:
    def __init__(self, data: bytes, rc: int = 0) -> None:
        self.data = data
        self.rc = rc
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakePopenProc:
        self.calls.append((list(args), kwargs))
        return FakePopenProc(self.data, self.rc)


# --- harness ----------------------------------------------------------------------------------


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return self.out + self.err


class Harness:
    def __init__(self, home: Path, keyring_backend: Any) -> None:
        self.home = home
        self.keyring = keyring_backend
        self.root = home / "runners"
        self.store = ConfigStore(home / "config.json")
        config = AppConfig()
        config.settings.runners_root = str(self.root)
        self.store.save(config)

        self.environ: dict[str, str] = {}
        self.answers: list[str | BaseException] = []
        self.prompts: list[str] = []
        self.secret_inputs: list[str | BaseException] = []
        self.secret_prompts: list[str] = []
        self.client = FakeClient()
        self.client_calls: list[tuple[str, str | None]] = []
        self.client_error: BaseException | None = None
        self.cmd = FakeConfigCmd(self.client)
        zip_path = home / "pkg" / f"actions-runner-win-x64-{VERSION}.zip"
        self.client.release_json = release_json(build_runner_zip(zip_path))
        self.cache = FakeCache(zip_path)
        self.running: dict[str, list[Any]] = {}
        self.stop_results: dict[str, list[int] | BaseException] = {}
        self.stopped: list[Path] = []
        self.sleep_fn: Callable[[float], None] = lambda s: None
        self.sleeps: list[float] = []
        self.popen: Any = subprocess.Popen
        self.start_manager: FakeStartManager | None = None
        self.out = io.StringIO()
        self.err = io.StringIO()

    # injected functions

    def _read_secret(self, prompt: str) -> str:
        self.secret_prompts.append(prompt)
        value = self.secret_inputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def _read_line(self, prompt: str) -> str:
        self.prompts.append(prompt)
        value = self.answers.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def _client_factory(self, pat: str, proxy: str | None) -> Any:
        self.client_calls.append((pat, proxy))
        if self.client_error is not None:
            raise self.client_error
        return self.client

    def _manager_factory(self, store: ConfigStore, client_factory: Any) -> Any:
        if self.start_manager is not None:
            return self.start_manager
        return RunnerManager(
            store,
            client_factory,
            cache=self.cache,  # type: ignore[arg-type]
            hostname=HOST,
            installer_run=self.cmd,
            popen=self.popen,
            find_processes=self._find,
            clock=lambda: NOW,
        )

    def _find(self, runner_dir: Path) -> list[Any]:
        return self.running.get(str(runner_dir), [])

    def _stop_external(self, runner_dir: Path) -> list[int]:
        self.stopped.append(runner_dir)
        value = self.stop_results.get(str(runner_dir), [])
        if isinstance(value, BaseException):
            raise value
        return value

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.sleep_fn(seconds)

    def deps(self) -> CliDeps:
        return CliDeps(
            stdout=self.out,
            stderr=self.err,
            environ=self.environ,
            read_secret=self._read_secret,
            read_line=self._read_line,
            store_factory=lambda: ConfigStore(self.store.path),
            client_factory=self._client_factory,
            manager_factory=self._manager_factory,
            stop_external=self._stop_external,
            sleep=self._sleep,
            clock=lambda: NOW,
            poll_interval_s=0.01,
            stop_timeout_s=7.5,
        )

    def run(self, *argv: str) -> Result:
        self.out = io.StringIO()
        self.err = io.StringIO()
        code = main(list(argv), self.deps())
        return Result(code, self.out.getvalue(), self.err.getvalue())

    # state helpers

    def config(self) -> AppConfig:
        return self.store.load()

    def login(self, account: str = OWNER, pat: str = FAKE_PAT) -> None:
        self.keyring.passwords[(secrets.KEYRING_SERVICE, account)] = pat
        config = self.config()
        config.account = account
        self.store.save(config)

    def seed(self, **kw: Any) -> RunnerRecord:
        kw.setdefault("owner", OWNER)
        kw.setdefault("repo", REPO)
        kw.setdefault("name", DEFAULT_NAME)
        kw.setdefault("path", str(self.root / kw["repo"]))
        record = RunnerRecord(**kw)
        config = self.config()
        config.upsert(record)
        self.store.save(config)
        return record


@pytest.fixture
def h(isolated_home: Path, memory_keyring: Any) -> Harness:
    return Harness(isolated_home, memory_keyring)


# --- argument parsing -------------------------------------------------------------------------


def _parse(argv: list[str]) -> Any:
    return build_parser(Console(io.StringIO(), io.StringIO())).parse_args(argv)


@pytest.mark.parametrize(
    ("argv", "handler", "expected"),
    [
        (["login"], "_cmd_login", {}),
        (["logout"], "_cmd_logout", {}),
        (["whoami"], "_cmd_whoami", {}),
        (["repos"], "_cmd_repos", {}),
        (
            ["add", "octo/demo"],
            "_cmd_add",
            {"repo": "octo/demo", "name": None, "labels": None, "root": None, "yes": False},
        ),
        (
            ["add", "demo", "--name", "n1", "--labels", "a,b", "--root", "R", "-y"],
            "_cmd_add",
            {"repo": "demo", "name": "n1", "labels": "a,b", "root": "R", "yes": True},
        ),
        (["add", "demo", "--version", "2.337.0"], "_cmd_add", {"runner_version": "2.337.0"}),
        (["list"], "_cmd_list", {}),
        (["status"], "_cmd_status", {}),
        (["start", "a", "b"], "_cmd_start", {"keys": ["a", "b"], "all": False}),
        (["start", "--all"], "_cmd_start", {"keys": [], "all": True}),
        (["stop", "a"], "_cmd_stop", {"keys": ["a"]}),
        (["stop", "a", "b"], "_cmd_stop", {"keys": ["a", "b"]}),
        (["remove", "a"], "_cmd_remove", {"key": "a", "keep_work": False, "yes": False}),
        (
            ["remove", "a", "--keep-work", "--yes"],
            "_cmd_remove",
            {"key": "a", "keep_work": True, "yes": True},
        ),
        (["import", "D:/runners/x"], "_cmd_import", {"path": "D:/runners/x"}),
        (["scan"], "_cmd_scan", {}),
        (["settings"], "_cmd_settings_show", {}),
        (["settings", "show"], "_cmd_settings_show", {}),
        (
            ["settings", "set", "proxy", "none"],
            "_cmd_settings_set",
            {"key": "proxy", "value": "none"},
        ),
        (["--debug", "list"], "_cmd_list", {"debug": True}),
    ],
)
def test_parser_accepts_every_command(
    argv: list[str], handler: str, expected: dict[str, Any]
) -> None:
    args = _parse(argv)
    assert args.handler is getattr(cli, handler)
    for key, value in expected.items():
        assert getattr(args, key) == value


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["bogus"],
        ["add"],
        ["remove"],
        ["stop"],
        ["import"],
        ["settings", "set", "theme", "dark"],
        ["settings", "set", "proxy"],
        ["list", "--unknown"],
    ],
)
def test_bad_arguments_exit_2(h: Harness, argv: list[str]) -> None:
    result = h.run(*argv)
    assert result.code == 2
    assert "usage:" in result.err
    assert "错误" in result.err
    assert result.out == ""


def test_help_and_version_exit_0(h: Harness) -> None:
    result = h.run("--help")
    assert result.code == 0
    assert "login" in result.out and "settings" in result.out
    assert "显示帮助并退出" in result.out
    result = h.run("add", "--help")
    assert result.code == 0
    assert "--labels" in result.out
    result = h.run("--version")
    assert result.code == 0
    assert result.out.startswith("runnerdeck-cli ")


def test_start_needs_keys_or_all(h: Harness) -> None:
    result = h.run("start")
    assert result.code == 2
    assert "--all" in result.err
    h.seed()
    result = h.run("start", "--all", DEFAULT_NAME)
    assert result.code == 2
    assert "不能" in result.err


def test_argparse_output_is_redacted(h: Harness) -> None:
    result = h.run("list", FAKE_PAT)  # "unrecognized arguments: <pat>"
    assert result.code == 2
    assert FAKE_PAT not in result.text
    assert "***" in result.err


# --- login / logout / whoami ------------------------------------------------------------------


def test_login_saves_pat_and_account(h: Harness) -> None:
    h.secret_inputs.append(f"  {FAKE_PAT}\n")
    result = h.run("login")
    assert result.code == 0, result.err
    assert h.secret_prompts and "PAT" in h.secret_prompts[0]
    assert h.client_calls == [(FAKE_PAT, None)]
    assert h.keyring.passwords[(secrets.KEYRING_SERVICE, OWNER)] == FAKE_PAT
    assert h.config().account == OWNER
    assert f"已登录：{OWNER}" in result.out
    assert "token 类型：fine-grained PAT" in result.out
    assert "Administration: Read and write" in result.out
    assert "Metadata: Read" in result.out
    assert "还剩 40 天" in result.out
    assert h.client.closed == 1
    assert FAKE_PAT not in result.text
    assert FAKE_PAT not in h.store.path.read_text(encoding="utf-8")


def test_login_rejects_bad_format_without_echo(h: Harness) -> None:
    pasted = "my-runnerdeck-token-name"  # the token's *name*, spike §2
    h.secret_inputs.append(pasted)
    result = h.run("login")
    assert result.code == 1
    assert "PAT 格式不对" in result.err
    assert "名字" in result.err
    assert pasted not in result.text
    assert h.client_calls == []
    assert h.keyring.passwords == {}
    assert h.config().account is None


def test_login_from_env_with_classic_token_missing_repo_scope(h: Harness) -> None:
    h.environ[cli.ENV_PAT] = CLASSIC_PAT
    h.client.user = GitHubUser(login=OWNER, id=1, token_kind="classic", scopes=["workflow"])
    result = h.run("login")
    assert result.code == 0, result.err
    assert h.secret_prompts == []  # never asked
    assert f"环境变量 {cli.ENV_PAT}" in result.out
    assert "token 类型：classic PAT" in result.out
    assert "权限范围：workflow" in result.out
    assert "缺少 repo 权限" in result.out
    assert "没有返回过期时间" in result.out
    assert h.keyring.passwords[(secrets.KEYRING_SERVICE, OWNER)] == CLASSIC_PAT


def test_login_env_bad_format_names_the_variable(h: Harness) -> None:
    h.environ[cli.ENV_PAT] = "RUNNER_TOKEN_NAME_NOT_VALUE"
    result = h.run("login")
    assert result.code == 1
    assert cli.ENV_PAT in result.err
    assert "RUNNER_TOKEN_NAME_NOT_VALUE" not in result.text


def test_login_as_other_account_deletes_previous_pat(h: Harness) -> None:
    h.login("olduser", CLASSIC_PAT)
    h.secret_inputs.append(FAKE_PAT)
    result = h.run("login")
    assert result.code == 0, result.err
    assert (secrets.KEYRING_SERVICE, "olduser") not in h.keyring.passwords
    assert h.keyring.passwords[(secrets.KEYRING_SERVICE, OWNER)] == FAKE_PAT
    assert "olduser" in result.out
    assert h.config().account == OWNER


def test_login_same_account_other_case_keeps_new_pat(h: Harness) -> None:
    h.login(OWNER.upper(), CLASSIC_PAT)
    h.secret_inputs.append(FAKE_PAT)
    assert h.run("login").code == 0
    assert h.keyring.passwords[(secrets.KEYRING_SERVICE, OWNER)] == FAKE_PAT


def test_login_auth_error_saves_nothing(h: Harness) -> None:
    h.secret_inputs.append(FAKE_PAT)
    h.client.failures["get_user"] = AuthError("PAT 无效或已过期（GET /user -> 401）", status=401)
    result = h.run("login")
    assert result.code == 1
    assert result.err.startswith("错误：PAT 无效或已过期")
    assert h.keyring.passwords == {}
    assert h.config().account is None
    assert h.client.closed == 1


def test_login_end_of_input(h: Harness) -> None:
    h.secret_inputs.append(EOFError())
    result = h.run("login")
    assert result.code == 1
    assert "没有读到 PAT" in result.err


def test_login_uses_configured_proxy(h: Harness) -> None:
    config = h.config()
    config.settings.proxy = "http://127.0.0.1:7890"
    h.store.save(config)
    h.secret_inputs.append(FAKE_PAT)
    assert h.run("login").code == 0
    assert h.client_calls == [(FAKE_PAT, "http://127.0.0.1:7890")]


def test_ctrl_c_outside_start_exits_130(h: Harness) -> None:
    h.secret_inputs.append(KeyboardInterrupt())
    result = h.run("login")
    assert result.code == 130
    assert "已中断" in result.err


def test_login_warns_when_previous_pat_cannot_be_deleted(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.login("olduser", CLASSIC_PAT)

    def refuse(service: str, username: str) -> None:
        raise OSError("credential store locked")

    monkeypatch.setattr(h.keyring, "delete_password", refuse)
    h.secret_inputs.append(FAKE_PAT)
    result = h.run("login")
    assert result.code == 0
    assert "警告：无法删除之前账号 olduser 保存的 PAT" in result.err
    assert "credential store locked" in result.err
    assert h.config().account == OWNER


def test_logout_deletes_pat_and_account(h: Harness) -> None:
    h.login()
    result = h.run("logout")
    assert result.code == 0
    assert h.keyring.passwords == {}
    assert h.config().account is None
    assert f"已退出登录 {OWNER}" in result.out
    assert cli.ENV_PAT not in result.out


def test_logout_when_not_logged_in_mentions_env(h: Harness) -> None:
    h.environ[cli.ENV_PAT] = FAKE_PAT
    result = h.run("logout")
    assert result.code == 0
    assert "当前没有登录" in result.out
    assert cli.ENV_PAT in result.out


def test_whoami_with_stored_pat(h: Harness) -> None:
    h.login()
    result = h.run("whoami")
    assert result.code == 0, result.err
    assert h.client_calls == [(FAKE_PAT, None)]
    assert f"登录名：{OWNER}" in result.out
    assert "PAT 来源：系统凭据管理器" in result.out
    assert "注意：配置里" not in result.out


def test_whoami_env_pat_wins_and_reports_mismatch(h: Harness) -> None:
    h.login("someone-else", CLASSIC_PAT)
    h.environ[cli.ENV_PAT] = FAKE_PAT
    result = h.run("whoami")
    assert result.code == 0, result.err
    assert h.client_calls == [(FAKE_PAT, None)]
    assert f"PAT 来源：环境变量 {cli.ENV_PAT}" in result.out
    assert "注意：配置里登录的账号是 someone-else" in result.out


def test_whoami_not_logged_in(h: Harness) -> None:
    result = h.run("whoami")
    assert result.code == 1
    assert "还没有登录" in result.err
    assert "login" in result.err
    assert h.client_calls == []


def test_whoami_pat_missing_from_keyring(h: Harness) -> None:
    config = h.config()
    config.account = OWNER
    h.store.save(config)
    result = h.run("whoami")
    assert result.code == 1
    assert "凭据管理器里没有" in result.err


def test_env_pat_with_bad_format_is_rejected_before_network(h: Harness) -> None:
    h.environ[cli.ENV_PAT] = "not-a-token-value"
    result = h.run("repos")
    assert result.code == 1
    assert cli.ENV_PAT in result.err
    assert h.client_calls == []


def test_expiry_formatting() -> None:
    assert cli._format_expiry(None, NOW)[1] is False
    text, attention = cli._format_expiry(NOW - timedelta(minutes=1), NOW)
    assert attention and "已过期" in text
    text, attention = cli._format_expiry(NOW + timedelta(hours=5), NOW)
    assert attention and "不到 1 天" in text
    text, attention = cli._format_expiry(NOW + timedelta(days=3, hours=1), NOW)
    assert attention and "还剩 3 天" in text
    expires = datetime(2026, 12, 1, tzinfo=UTC) + timedelta(hours=-8)
    text, attention = cli._format_expiry(expires, NOW)
    assert not attention
    assert text.startswith("2026-11-30 16:00+00:00")


def test_expiry_warning_is_shown(h: Harness) -> None:
    h.login()
    h.client.user = GitHubUser(
        login=OWNER, id=1, token_kind="fine-grained", token_expires_at=NOW + timedelta(days=2)
    )
    result = h.run("whoami")
    assert "注意：过期时间" in result.out
    assert "即将过期" in result.out


@pytest.mark.parametrize(
    ("user", "needle"),
    [
        (
            GitHubUser(login="a", id=1, token_kind="classic", scopes=["repo", "workflow"]),
            "已有 repo",
        ),
        (GitHubUser(login="a", id=1, token_kind="classic", scopes=[]), "当前：无"),
        (GitHubUser(login="a", id=1, token_kind="unknown"), "无法判断 token 类型"),
        (GitHubUser(login="a", id=1, token_kind="fine-grained"), "Administration"),
    ],
)
def test_permission_hints(user: GitHubUser, needle: str) -> None:
    assert needle in " ".join(permission_hints(user))


# --- repos / list -------------------------------------------------------------------------------


def test_repos_marks_visibility_and_local_runners(h: Harness) -> None:
    h.login()
    h.client.repo_list = [
        repo_model(REPO, private=False),
        repo_model("secret-repo", private=True, archived=True, fork=True),
    ]
    h.seed(name="r1")
    h.seed(name="r2")
    result = h.run("repos")
    assert result.code == 0, result.err
    lines = result.out.splitlines()
    assert lines[0].split() == ["仓库", "可见性", "本机", "runner"]
    demo = next(line for line in lines if line.startswith(f"{OWNER}/{REPO} "))
    assert "公开" in demo and "r1, r2" in demo
    other = next(line for line in lines if line.startswith(f"{OWNER}/secret-repo"))
    assert "私有，已归档，fork" in other
    assert other.rstrip().endswith("-")
    assert "共 2 个仓库" in result.out


def test_repos_empty(h: Harness) -> None:
    h.login()
    result = h.run("repos")
    assert result.code == 0
    assert "没有自己的仓库" in result.out


def test_list_empty_and_table(h: Harness) -> None:
    result = h.run("list")
    assert result.code == 0
    assert "还没有登记任何 runner" in result.out
    record = h.seed(labels=["gpu", "fast"])
    result = h.run("list")
    assert result.code == 0
    header, rule, row = result.out.splitlines()
    assert header.split() == ["ID", "名称", "仓库", "自定义标签", "目录"]
    assert set(rule) <= {"-", " "}
    assert row.startswith(record.id[:8])
    assert DEFAULT_NAME in row and "gpu, fast" in row and record.path in row
    assert h.client_calls == []  # no network, no login needed


# --- add --------------------------------------------------------------------------------------


def test_add_public_repo_confirmed(h: Harness) -> None:
    h.login()
    h.client.repos[_key(OWNER, REPO)] = repo_model(REPO, private=False)
    h.answers.append("y")
    result = h.run("add", f"{OWNER}/{REPO}")
    assert result.code == 0, result.err

    settings_url = f"https://github.com/{OWNER}/{REPO}/settings/actions"
    assert settings_url in result.out
    assert "fork PR" in result.out
    assert "（公开）" in result.out
    assert len(h.prompts) == 1 and "y" in h.prompts[0]
    assert "  > 查询 runner 版本" in result.out
    assert "  > 注册 runner" in result.out
    assert f"前台运行它：runnerdeck-cli start {DEFAULT_NAME}" in result.out

    (record,) = h.config().runners
    assert record.name == DEFAULT_NAME
    assert record.github_runner_id == AGENT_ID
    assert Path(record.path) == h.root / REPO
    assert (h.root / REPO / ".runner").is_file()
    assert REG_TOKEN not in result.text
    assert FAKE_PAT not in result.text


def test_add_public_repo_refused(h: Harness) -> None:
    h.login()
    h.client.repos[_key(OWNER, REPO)] = repo_model(REPO, private=False)
    h.answers.append("n")
    result = h.run("add", f"{OWNER}/{REPO}")
    assert result.code == 1
    assert "已取消" in result.out
    assert h.config().runners == []
    assert "create_registration_token" not in h.client.names()
    assert "get_runner_release" not in h.client.names()
    assert not (h.root / REPO).exists()
    assert h.cmd.calls == []


def test_add_without_tty_is_cancelled(h: Harness) -> None:
    h.login()
    h.client.repos[_key(OWNER, REPO)] = repo_model(REPO, private=False)
    h.answers.append(EOFError())
    result = h.run("add", f"{OWNER}/{REPO}")
    assert result.code == 1
    assert "--yes" in result.out
    assert h.config().runners == []


def test_add_public_repo_with_yes_skips_prompt(h: Harness) -> None:
    h.login()
    h.client.repos[_key(OWNER, REPO)] = repo_model(REPO, private=False)
    result = h.run("add", f"{OWNER}/{REPO}", "--yes")
    assert result.code == 0, result.err
    assert h.prompts == []
    assert "settings/actions" in result.out  # the warning is still shown
    assert len(h.config().runners) == 1


def test_add_private_repo_needs_no_confirmation_and_uses_options(h: Harness) -> None:
    h.login()
    h.client.repos[_key(OWNER, REPO)] = repo_model(REPO, private=True)
    other_root = h.home / "elsewhere"
    result = h.run(
        "add", REPO, "--name", "box-1", "--labels", "gpu, fast", "--root", str(other_root)
    )
    assert result.code == 0, result.err
    assert h.prompts == []
    assert "fork PR" not in result.out
    assert "标签：self-hosted, Windows, X64, gpu, fast" in result.out
    (record,) = h.config().runners
    assert (record.owner, record.repo, record.name) == (OWNER, REPO, "box-1")
    assert record.labels == ["gpu", "fast"]
    assert Path(record.path) == other_root / REPO
    labels_arg = h.cmd.calls[0][h.cmd.calls[0].index("--labels") + 1]
    assert labels_arg == "gpu,fast"


def test_add_short_repo_with_env_pat_asks_github_for_owner(h: Harness) -> None:
    h.environ[cli.ENV_PAT] = FAKE_PAT
    h.client.repos[_key(OWNER, REPO)] = repo_model(REPO, private=True)
    result = h.run("add", REPO)
    assert result.code == 0, result.err
    assert h.client.names()[0] == "get_user"
    assert h.config().runners[0].owner == OWNER


def test_add_accepts_repo_url_and_version(h: Harness) -> None:
    h.login()
    h.client.repos[_key(OWNER, REPO)] = repo_model(REPO, private=True)
    result = h.run("add", f"{URL}/", "--version", f"v{VERSION}", "-y")
    assert result.code == 0, result.err
    assert ("get_runner_release", VERSION) in h.client.calls
    assert f"runner 版本：{VERSION}" in result.out


def test_add_failure_reports_error_and_rolls_back(h: Harness) -> None:
    h.login()
    h.client.repos[_key(OWNER, REPO)] = repo_model(REPO, private=True)
    h.client.failures["create_registration_token"] = PermissionDeniedError(
        "fine-grained PAT 需要选中该仓库，并授予 Administration: Read and write 权限", status=403
    )
    result = h.run("add", f"{OWNER}/{REPO}")
    assert result.code == 1
    assert result.err.startswith("错误：fine-grained PAT 需要选中该仓库")
    assert "  > 获取注册 token" in result.out
    assert "回滚" in result.out
    assert not (h.root / REPO).exists()
    assert h.config().runners == []
    assert REG_TOKEN not in result.text


def test_add_repo_not_found(h: Harness) -> None:
    h.login()
    result = h.run("add", f"{OWNER}/missing")
    assert result.code == 1
    assert "仓库不存在" in result.err


def test_add_not_logged_in(h: Harness) -> None:
    result = h.run("add", REPO)
    assert result.code == 1
    assert "还没有登录" in result.err


# --- status -----------------------------------------------------------------------------------


def test_status_table(h: Harness) -> None:
    h.login()
    online = h.seed(name="on", github_runner_id=1, path=str(h.root / "on"))
    offline = h.seed(name="off", github_runner_id=2, path=str(h.root / "off"))
    gone = h.seed(name="gone", github_runner_id=3, path=str(h.root / "gone"))
    ext = h.seed(name="ext", github_runner_id=4, path=str(h.root / "ext"))
    other = h.seed(name="far", repo="other", path=str(h.root / "other"))
    h.client.runners[_key(OWNER, REPO)] = [
        remote(1, "on", "online", busy=True),
        remote(2, "off"),
        remote(4, "ext"),
    ]
    h.client.list_failures[_key(OWNER, "other")] = GitHubError("GitHub 服务暂时不可用", status=502)
    h.running[ext.path] = [object()]

    result = h.run("status")
    assert result.code == 0, result.err
    lines = result.out.splitlines()
    assert lines[0].split() == ["ID", "名称", "仓库", "状态", "GitHub", "说明"]

    def row(record: RunnerRecord) -> str:
        return next(line for line in lines if line.startswith(record.id[:8]))

    assert "被其他进程运行" in row(online) and "在线（忙）" in row(online)
    assert "已停止" in row(offline) and "离线" in row(offline)
    assert "已在 GitHub 端删除" in row(gone) and "不存在" in row(gone)
    assert "被其他进程运行" in row(ext) and "目录下有不归本应用管理" in row(ext)
    assert "已停止" in row(other) and "未知" in row(other)
    assert f"注意：无法获取 GitHub 状态（{OWNER}/other）：GitHub 服务暂时不可用" in result.out


def test_status_not_logged_in_reports_once(h: Harness) -> None:
    h.seed(name="a", path=str(h.root / "a"))
    h.seed(name="b", path=str(h.root / "b"))
    result = h.run("status")
    assert result.code == 0
    error_lines = [line for line in result.out.splitlines() if line.startswith("注意：")]
    assert len(error_lines) == 1
    assert f"（{OWNER}/{REPO}）" in error_lines[0]
    assert "还没有登录" in error_lines[0]


def test_status_empty(h: Harness) -> None:
    result = h.run("status")
    assert result.code == 0
    assert "还没有登记任何 runner" in result.out


# --- start --------------------------------------------------------------------------------------


def _start_scripts(h: Harness, scripts: dict[str, Script]) -> dict[str, RunnerRecord]:
    h.start_manager = FakeStartManager(scripts)
    return {name: h.seed(name=name, path=str(h.root / name)) for name in scripts}


def test_start_supervises_until_ctrl_c(h: Harness) -> None:
    lines = JOB_OUTPUT.decode("utf-8").splitlines()
    scripts = {
        "alpha": Script(
            lines=[ln for ln in lines if ln.strip()][:3],
            states=[(LocalState.STARTING, None), (LocalState.IDLE, None)],
        ),
        "beta": Script(
            states=[(LocalState.BUSY, "build")],
            stop_results=[StopResult(False, StopMethod.KILL, 1, [77, 78])],
        ),
    }
    _start_scripts(h, scripts)

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    h.sleep_fn = interrupt
    result = h.run("start", "alpha", "beta")
    assert result.code == 0, result.err
    out = result.out
    assert "[alpha] \u221a Connected to GitHub" in out
    assert "[alpha] 状态：启动中" in out
    assert "[alpha] 状态：空闲" in out
    assert "[beta] 状态：运行中：build" in out
    assert "[alpha] 已启动（PID" in out
    assert "按 Ctrl+C 停止所有 runner" in out
    assert "收到 Ctrl+C" in out
    assert "[alpha] 已停止" in out
    assert "[beta] 没能正常停止，已强制结束（PID 77, 78）" in out
    assert LINGER_HINT in out
    procs = h.start_manager.procs  # type: ignore[union-attr]
    assert procs["alpha"].stop_calls == [7.5]
    assert procs["beta"].stop_calls == [7.5]
    assert h.sleeps == [0.01]


def test_start_all_graceful_stop_has_no_linger_hint(h: Harness) -> None:
    _start_scripts(h, {"alpha": Script(), "beta": Script()})
    h.sleep_fn = lambda s: (_ for _ in ()).throw(KeyboardInterrupt())
    result = h.run("start", "--all")
    assert result.code == 0
    assert result.out.count("已停止") == 2
    assert LINGER_HINT not in result.out


def test_start_runners_that_exit_on_their_own(h: Harness) -> None:
    _start_scripts(
        h,
        {
            "ok": Script(exit_code=0),
            "bad": Script(exit_code=3, last_error="runner 意外退出（退出码 3）：boom"),
        },
    )
    result = h.run("start", "ok", "bad")
    assert result.code == 1
    assert "[ok] 已退出（退出码 0）" in result.out
    assert "错误：[bad] runner 意外退出（退出码 3）：boom" in result.err
    assert h.sleeps == []  # nothing left running, no waiting
    assert "收到 Ctrl+C" not in result.out


def test_start_waits_while_running(h: Harness) -> None:
    _start_scripts(h, {"alpha": Script()})
    procs = h.start_manager.procs  # type: ignore[union-attr]

    def finish_after_three(seconds: float) -> None:
        if len(h.sleeps) == 3:
            procs["alpha"].running = False
            procs["alpha"].exit_code = 0

    h.sleep_fn = finish_after_three
    result = h.run("start", "alpha")
    assert result.code == 0
    assert len(h.sleeps) == 3
    assert "[alpha] 已退出（退出码 0）" in result.out


def test_start_failure_of_one_runner_keeps_the_others(h: Harness) -> None:
    _start_scripts(
        h,
        {
            "broken": Script(start_error=ProcessError("目录下已有 runner 进程在运行")),
            "fine": Script(),
        },
    )
    h.sleep_fn = lambda s: (_ for _ in ()).throw(KeyboardInterrupt())
    result = h.run("start", "broken", "fine")
    assert result.code == 1
    assert "错误：[broken] 无法启动：目录下已有 runner 进程在运行" in result.err
    assert "[fine] 已停止" in result.out


def test_start_nothing_started(h: Harness) -> None:
    _start_scripts(h, {"broken": Script(start_error=ProcessError("找不到 run.cmd"))})
    result = h.run("start", "broken")
    assert result.code == 1
    assert h.sleeps == []
    assert "按 Ctrl+C" not in result.out


def test_start_second_ctrl_c_forces(h: Harness) -> None:
    forced = StopResult(False, StopMethod.KILL, 1, [5])
    _start_scripts(
        h,
        {
            "alpha": Script(stop_results=[KeyboardInterrupt(), forced]),
            "beta": Script(stop_results=[forced]),
        },
    )
    h.sleep_fn = lambda s: (_ for _ in ()).throw(KeyboardInterrupt())
    result = h.run("start", "alpha", "beta")
    assert result.code == 0
    procs = h.start_manager.procs  # type: ignore[union-attr]
    assert procs["alpha"].stop_calls == [7.5, 0.0]
    assert procs["beta"].stop_calls == [0.0]  # no more waiting for the rest
    assert "再次收到 Ctrl+C" in result.out
    assert LINGER_HINT in result.out


def test_start_third_ctrl_c_gives_up(h: Harness) -> None:
    _start_scripts(h, {"alpha": Script(stop_results=[KeyboardInterrupt(), KeyboardInterrupt()])})
    h.sleep_fn = lambda s: (_ for _ in ()).throw(KeyboardInterrupt())
    result = h.run("start", "alpha")
    assert result.code == 130


def test_start_stop_failure_is_reported(h: Harness) -> None:
    _start_scripts(
        h, {"alpha": Script(stop_results=[ProcessError("无法结束 runner 进程（PID 9）")])}
    )
    h.sleep_fn = lambda s: (_ for _ in ()).throw(KeyboardInterrupt())
    result = h.run("start", "alpha")
    assert result.code == 1
    assert "错误：[alpha] 无法结束 runner 进程（PID 9）" in result.err


def test_start_reconnecting_hint_once_per_runner(h: Harness) -> None:
    states = [
        (LocalState.RECONNECTING, None),
        (LocalState.STARTING, None),
        (LocalState.RECONNECTING, None),
    ]
    _start_scripts(h, {"alpha": Script(states=states, exit_code=0)})
    result = h.run("start", "alpha")
    assert result.out.count("[alpha] 状态：等待旧会话过期") == 2
    assert result.out.count(RECONNECTING_HINT) == 1


def test_start_unexpected_error_stops_runners(h: Harness) -> None:
    _start_scripts(h, {"alpha": Script()})

    def broken_sleep(seconds: float) -> None:
        raise RuntimeError("clock broke")

    h.sleep_fn = broken_sleep
    result = h.run("start", "alpha")
    assert result.code == 1
    assert "内部错误：RuntimeError: clock broke" in result.err
    assert h.start_manager.procs["alpha"].stop_calls == [7.5]  # type: ignore[union-attr]


def test_ctrl_c_only_stops_runners_still_running(h: Harness) -> None:
    _start_scripts(h, {"done": Script(exit_code=0), "busy": Script()})
    h.sleep_fn = lambda s: (_ for _ in ()).throw(KeyboardInterrupt())
    result = h.run("start", "done", "busy")
    assert result.code == 0
    procs = h.start_manager.procs  # type: ignore[union-attr]
    assert procs["done"].stop_calls == []
    assert procs["busy"].stop_calls == [7.5]
    assert "[done] 已停止" not in result.out
    assert "[busy] 已停止" in result.out


def test_start_all_without_runners(h: Harness) -> None:
    result = h.run("start", "--all")
    assert result.code == 1
    assert "还没有登记任何 runner" in result.err


def test_start_unknown_runner(h: Harness) -> None:
    result = h.run("start", "nope")
    assert result.code == 1
    assert "找不到 runner" in result.err


def test_start_with_real_runner_process_and_fake_popen(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(winproc, "find_runner_processes", lambda runner_dir: [])
    record = h.seed(name="real", path=str(make_runner_dir(h.root / "real")))
    h.popen = FakePopen(JOB_OUTPUT, rc=0)

    def guard(seconds: float) -> None:
        if len(h.sleeps) > 500:
            raise AssertionError("runner never finished")
        time.sleep(seconds)

    h.sleep_fn = guard
    result = h.run("start", record.name)
    assert result.code == 0, result.err
    out = result.out
    assert "[real] Current runner version: '2.338.0'" in out
    assert "[real] 状态：空闲" in out
    assert "[real] 状态：运行中：spike" in out
    assert "[real] 已退出（退出码 0）" in out
    ((args, kwargs),) = h.popen.calls
    assert args == ["cmd.exe", "/c", "run.cmd"]
    assert kwargs["cwd"] == record.path
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_every_local_state_has_a_label() -> None:
    sup = cli._Supervisor.__new__(cli._Supervisor)
    out = io.StringIO()
    sup.console = Console(out, io.StringIO())
    sup._hinted = set()
    on_state = sup._on_state("x")
    for state in LocalState:
        on_state(state, "job")
    assert len(out.getvalue().splitlines()) == len(LocalState) + 1  # + reconnecting hint


# --- stop ---------------------------------------------------------------------------------------


def test_stop_kills_and_prints_linger_hint(h: Harness) -> None:
    record = h.seed()
    h.stop_results[record.path] = [11, 12]
    result = h.run("stop", DEFAULT_NAME)
    assert result.code == 0
    assert h.stopped == [Path(record.path)]
    assert f"已强制结束 runner {DEFAULT_NAME}（PID 11, 12）" in result.out
    assert LINGER_HINT in result.out


def test_stop_not_running(h: Harness) -> None:
    h.seed()
    result = h.run("stop", DEFAULT_NAME, f"{OWNER}/{REPO}")  # same runner twice
    assert result.code == 0
    assert result.out.count("没有在运行") == 1
    assert LINGER_HINT not in result.out


def test_stop_failure(h: Harness) -> None:
    record = h.seed()
    h.stop_results[record.path] = psutil.AccessDenied(pid=5)
    result = h.run("stop", DEFAULT_NAME)
    assert result.code == 1
    assert f"错误：无法结束 runner {DEFAULT_NAME}" in result.err


# --- remove -------------------------------------------------------------------------------------


def test_remove_confirmed_unconfigures_and_deletes(h: Harness) -> None:
    h.login()
    runner_dir = make_runner_dir(h.root / REPO)
    h.client.runners[_key(OWNER, REPO)] = [remote(AGENT_ID, DEFAULT_NAME)]
    h.seed(path=str(runner_dir), github_runner_id=AGENT_ID)
    h.answers.append("y")
    result = h.run("remove", DEFAULT_NAME)
    assert result.code == 0, result.err
    assert "确认移除" in h.prompts[0]
    assert "GitHub：已通过 config.cmd remove 注销" in result.out
    assert "目录：已删除" in result.out
    assert "  > 注销 runner" in result.out
    assert not runner_dir.exists()
    assert h.config().runners == []
    assert h.client.runners[_key(OWNER, REPO)] == []
    assert REMOVE_TOKEN not in result.text


def test_remove_refused(h: Harness) -> None:
    h.login()
    runner_dir = make_runner_dir(h.root / REPO)
    h.seed(path=str(runner_dir))
    h.answers.append("")
    result = h.run("remove", DEFAULT_NAME)
    assert result.code == 1
    assert "已取消" in result.out
    assert runner_dir.exists()
    assert len(h.config().runners) == 1
    assert h.client.calls == []


def test_remove_keep_work_via_api(h: Harness) -> None:
    h.login()
    runner_dir = make_runner_dir(h.root / REPO, credentials=False)  # not configured any more
    h.client.runners[_key(OWNER, REPO)] = [remote(AGENT_ID, DEFAULT_NAME)]
    h.seed(path=str(runner_dir))
    result = h.run("remove", DEFAULT_NAME, "--keep-work", "--yes")
    assert result.code == 0, result.err
    assert h.prompts == []
    assert f"GitHub：已通过 API 删除（runner id {AGENT_ID}）" in result.out
    assert "（保留了 _work）" in result.out
    assert (runner_dir / "_work" / "keep.txt").is_file()
    assert not (runner_dir / "config.cmd").exists()


def test_remove_already_gone_with_missing_dir(h: Harness) -> None:
    h.login()
    h.seed(path=str(h.root / "vanished"))
    result = h.run("remove", DEFAULT_NAME, "-y")
    assert result.code == 0, result.err
    assert "GitHub：上面已经没有这个 runner" in result.out
    assert "目录：" not in result.out
    assert h.config().runners == []


def test_remove_warnings_are_printed(h: Harness) -> None:
    h.login()
    runner_dir = make_runner_dir(h.root / REPO)
    (runner_dir / "run.cmd").unlink()  # no longer looks like a runner dir: refuse to delete
    h.client.runners[_key(OWNER, REPO)] = [remote(AGENT_ID, DEFAULT_NAME)]
    h.seed(path=str(runner_dir))
    result = h.run("remove", DEFAULT_NAME, "-y")
    assert result.code == 0, result.err
    assert "注意：目录未删除" in result.out
    assert runner_dir.exists()


def test_remove_while_running_suggests_stop(h: Harness) -> None:
    h.login()
    runner_dir = make_runner_dir(h.root / REPO)
    record = h.seed(path=str(runner_dir))
    h.running[record.path] = [psutil.Process()]  # any object with a pid
    result = h.run("remove", DEFAULT_NAME, "-y")
    assert result.code == 1
    assert "正在运行" in result.err
    assert f"runnerdeck-cli stop {DEFAULT_NAME}" in result.err
    assert runner_dir.exists()
    assert len(h.config().runners) == 1


def test_remove_hint_uses_id_when_name_is_ambiguous(h: Harness) -> None:
    h.login()
    first = h.seed(path=str(make_runner_dir(h.root / "a")))
    h.seed(repo="other", path=str(h.root / "b"))
    h.running[first.path] = [psutil.Process()]
    result = h.run("remove", first.id, "-y")
    assert f"stop {first.id[:8]}" in result.err


# --- import / scan ------------------------------------------------------------------------------


def test_import_new_existing_and_moved(h: Harness) -> None:
    h.login()
    runner_dir = make_runner_dir(h.root / "manual")
    h.client.runners[_key(OWNER, REPO)] = [remote(AGENT_ID, DEFAULT_NAME)]
    result = h.run("import", str(runner_dir))
    assert result.code == 0, result.err
    assert f"已导入 runner {DEFAULT_NAME}" in result.out
    assert f"GitHub id {AGENT_ID}" in result.out
    (record,) = h.config().runners
    assert record.labels == ["gpu"]

    result = h.run("import", str(runner_dir))
    assert result.code == 0
    assert "已经登记为 runner" in result.out

    moved = h.root / "moved"
    runner_dir.rename(moved)
    result = h.run("import", str(moved))
    assert result.code == 0, result.err
    assert "目录已更新" in result.out
    assert h.config().runners[0].path == str(moved)


def test_import_rejects_non_runner_dir(h: Harness) -> None:
    h.login()
    plain = h.home / "plain"
    plain.mkdir()
    result = h.run("import", str(plain))
    assert result.code == 1
    assert "不是 runner 目录" in result.err


def test_scan_lists_unregistered(h: Harness) -> None:
    make_runner_dir(h.root / "known")
    make_runner_dir(h.root / "stray")
    h.seed(path=str(h.root / "known"))
    result = h.run("scan")
    assert result.code == 0
    assert f"  {h.root / 'stray'}" in result.out
    assert str(h.root / "known") not in result.out
    assert "import" in result.out


def test_scan_nothing(h: Harness) -> None:
    result = h.run("scan")
    assert result.code == 0
    assert "没有未登记的 runner 目录" in result.out


# --- settings -----------------------------------------------------------------------------------


def test_settings_show(h: Harness) -> None:
    result = h.run("settings")
    assert result.code == 0
    assert f"runners_root     {h.root}" in result.out
    assert "default_labels   无" in result.out
    assert "poll_interval_s  30" in result.out
    assert "未设置" in result.out
    assert str(h.store.path) in result.out


@pytest.mark.parametrize(
    ("key", "value", "stored", "printed"),
    [
        ("poll_interval_s", "60", 60, "= 60"),
        ("default_labels", "gpu, fast,GPU", ["gpu", "fast"], "= gpu, fast"),
        ("default_labels", "", [], "= 无"),
        ("proxy", "127.0.0.1:7890", "http://127.0.0.1:7890", "= http://127.0.0.1:7890"),
        ("proxy", "https://proxy.example:8443", "https://proxy.example:8443", "8443"),
        ("proxy", "none", None, "= 未设置"),
    ],
)
def test_settings_set(h: Harness, key: str, value: str, stored: Any, printed: str) -> None:
    result = h.run("settings", "set", key, value)
    assert result.code == 0, result.err
    assert getattr(h.config().settings, key) == stored
    assert f"已设置 {key} {printed}" in result.out or printed in result.out


def test_settings_set_runners_root_is_absolute(h: Harness) -> None:
    result = h.run("settings", "set", "runners_root", "relative/runners")
    assert result.code == 0
    stored = h.config().settings.runners_root
    assert Path(stored).is_absolute()
    assert stored.endswith(os.path.join("relative", "runners"))


@pytest.mark.parametrize(
    ("key", "value", "needle"),
    [
        ("poll_interval_s", "1", "poll_interval_s 的值不合法"),
        ("poll_interval_s", "abc", "必须是整数"),
        ("proxy", "ftp://host:21", "代理地址不合法"),
        ("proxy", "http://host:99999", "代理地址不合法"),
        ("proxy", "http://", "代理地址不合法"),
        ("default_labels", "bad label!", "标签不合法"),
        ("runners_root", "  ", "不能为空"),
    ],
)
def test_settings_set_rejects_bad_values(h: Harness, key: str, value: str, needle: str) -> None:
    before = h.config().settings
    result = h.run("settings", "set", key, value)
    assert result.code == 1
    assert needle in result.err
    assert h.config().settings == before


def test_settings_proxy_password_is_masked(h: Harness) -> None:
    password = "hunter2-fake-pw"
    result = h.run("settings", "set", "proxy", f"http://user:{password}@127.0.0.1:7890")
    assert result.code == 0, result.err
    assert password not in result.text
    result = h.run("settings", "show")
    assert password not in result.text
    assert "user:***@127.0.0.1:7890" in result.out
    assert h.config().settings.proxy == f"http://user:{password}@127.0.0.1:7890"


# --- main: errors, logging, output ---------------------------------------------------------------


def test_corrupt_config_is_an_error(h: Harness) -> None:
    h.store.path.write_text("{not json", encoding="utf-8")
    result = h.run("list")
    assert result.code == 1
    assert result.err.startswith("错误：配置文件不是有效的 UTF-8 JSON")


def test_internal_error_points_to_log(h: Harness) -> None:
    h.login()
    h.client_error = RuntimeError("bug in the client")
    result = h.run("whoami")
    assert result.code == 1
    assert "内部错误：RuntimeError: bug in the client" in result.err
    assert "runnerdeck.log" in result.err
    assert "Traceback" not in result.err
    log_file = h.home / "logs" / "runnerdeck.log"
    for handler in logging.getLogger("runnerdeck").handlers:
        handler.flush()
    assert "command whoami crashed" in log_file.read_text(encoding="utf-8")


def test_debug_prints_traceback_and_logs_to_stream(h: Harness) -> None:
    calls: list[dict[str, Any]] = []

    def fake_setup(**kw: Any) -> Path:
        calls.append(kw)
        return h.home / "fake.log"

    h.login()
    h.client_error = RuntimeError("bug")
    deps = h.deps()
    deps.setup_logging = fake_setup
    err = io.StringIO()
    deps.stderr = err
    assert main(["--debug", "whoami"], deps) == 1
    assert calls == [{"level": logging.DEBUG, "stream": True}]
    assert "Traceback" in err.getvalue()
    assert "fake.log" in err.getvalue()


def test_logging_failure_does_not_stop_the_command(h: Harness) -> None:
    def broken_setup(**kw: Any) -> Path:
        raise PermissionError("denied")

    deps = h.deps()
    deps.setup_logging = broken_setup
    out, err = io.StringIO(), io.StringIO()
    deps.stdout, deps.stderr = out, err
    assert main(["list"], deps) == 0
    assert "无法写入日志文件" in err.getvalue()
    assert "还没有登记任何 runner" in out.getvalue()


def test_logging_goes_to_log_dir(h: Harness) -> None:
    assert h.run("list").code == 0
    assert (h.home / "logs" / "runnerdeck.log").is_file()


def test_registered_secrets_never_reach_stdout_or_stderr(h: Harness) -> None:
    secret = "FAKE-SECRET-VALUE-0123456789"  # not token-shaped: masked only by registration
    register_secret(secret)
    _start_scripts(
        h,
        {
            "alpha": Script(
                lines=[f"runner said {secret}"],
                states=[(LocalState.BUSY, f"job-{secret}")],
                stop_results=[ProcessError(f"cannot stop {secret}")],
            )
        },
    )
    h.sleep_fn = lambda s: (_ for _ in ()).throw(KeyboardInterrupt())
    result = h.run("start", "alpha")
    assert secret not in result.text
    assert "runner said ***" in result.out
    assert "错误：[alpha] cannot stop ***" in result.err

    h.start_manager = None
    h.login()
    h.client.failures["get_user"] = GitHubError(f"GitHub said {secret}", status=500)
    result = h.run("whoami")
    assert result.code == 1
    assert secret not in result.text
    assert "***" in result.err

    result = h.run("start", secret)  # resolve_runner echoes the key
    assert secret not in result.text


def test_default_factories(tmp_path: Path) -> None:
    client = cli._default_client(FAKE_PAT, "127.0.0.1:7890")
    try:
        assert isinstance(client, GitHubClient)
    finally:
        client.close()
    manager = cli._default_manager(ConfigStore(tmp_path / "config.json"), lambda: client)
    assert isinstance(manager, RunnerManager)
    assert cli._utcnow().tzinfo is UTC
    deps = CliDeps()
    assert deps.environ is os.environ
    assert deps.stop_external is cli.process.stop_external


def test_mask_proxy() -> None:
    assert cli._mask_proxy("http://u:pw-123@h:1") == "http://u:***@h:1"
    assert cli._mask_proxy("h:1") == "h:1"
    assert cli._mask_proxy("http://[broken") == "http://[broken"


def test_console_survives_unencodable_output() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii")
    Console(stream, stream).print("状态 ok")
    stream.flush()
    assert raw.getvalue() == b"?? ok\n"


def test_render_table_counts_wide_characters() -> None:
    lines = render_table(("名称", "x"), [("ab", "1"), ("长名字", None)])
    assert lines[0] == "名称    x"
    assert lines[1] == "------  -"
    assert lines[2] == "ab      1"
    assert lines[3] == "长名字  -"


def test_module_runs_as_script(tmp_path: Path) -> None:
    env = {**os.environ, "RUNNERDECK_HOME": str(tmp_path)}
    env.pop(cli.ENV_PAT, None)
    version = subprocess.run(
        [sys.executable, "-m", "runnerdeck.core.cli", "--version"],
        capture_output=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert version.returncode == 0
    assert b"runnerdeck-cli" in version.stdout
    listing = subprocess.run(
        [sys.executable, "-m", "runnerdeck.core.cli", "list"],
        capture_output=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert listing.returncode == 0, listing.stderr
    assert listing.stdout.strip()  # Chinese text, or "?" on a cp1252 pipe
    bad = subprocess.run(
        [sys.executable, "-m", "runnerdeck.core.cli", "bogus"],
        capture_output=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert bad.returncode == 2


# --- Windows only: real cmd.exe + fake listener ------------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def _python_for_fake_runner() -> str:
    # Prefer the base interpreter: a venv's python.exe is a launcher that adds a process level.
    base = getattr(sys, "_base_executable", "") or ""
    return base if base and Path(base).is_file() else sys.executable


def make_fake_runner_dir(path: Path, mode: str) -> Path:
    path.mkdir(parents=True)
    template = (FAKE_RUNNER / "run.cmd.template").read_text(encoding="utf-8")
    script = (
        template.replace("@PYTHON@", _python_for_fake_runner())
        .replace("@LISTENER@", str(FAKE_RUNNER / "fake_listener.py"))
        .replace("@MODE@", mode)
        .replace("@PIDFILE@", str(path.parent / f"{path.name}.pid"))
    )
    (path / "run.cmd").write_bytes("\r\n".join(script.splitlines()).encode("ascii") + b"\r\n")
    return path


def _guarded_sleep(h: Harness, limit_s: float, until: Callable[[], bool] | None = None) -> Any:
    deadline = time.monotonic() + limit_s

    def sleep(seconds: float) -> None:
        if until is not None and until():
            raise KeyboardInterrupt
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the fake runner")
        time.sleep(seconds)

    return sleep


@windows_only
def test_windows_start_reports_crash(h: Harness) -> None:
    runner_dir = make_fake_runner_dir(h.root / "crashy", "crash")
    h.seed(name="crashy", path=str(runner_dir))
    h.sleep_fn = _guarded_sleep(h, 60)
    result = h.run("start", "crashy")
    assert result.code == 1, result.text
    assert "[crashy] 状态：空闲" in result.out
    assert "退出码 3" in result.err


@windows_only
def test_windows_start_then_ctrl_c_stops_runner(h: Harness) -> None:
    runner_dir = make_fake_runner_dir(h.root / "idler", "idle")
    h.seed(name="idler", path=str(runner_dir))
    h.sleep_fn = _guarded_sleep(h, 60, until=lambda: "[idler] 状态：空闲" in h.out.getvalue())
    result = h.run("start", "idler")
    assert result.code == 0, result.text
    # Graceful when pytest has a console, otherwise killed (spike §5).
    assert "[idler] 已停止" in result.out or LINGER_HINT in result.out
    pid_file = h.root / "idler.pid"
    pid = int(pid_file.read_text(encoding="ascii"))
    deadline = time.monotonic() + 15
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not psutil.pid_exists(pid)
