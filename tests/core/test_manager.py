"""Tests for runnerdeck.core.manager.

Everything runs against fakes: a recording GitHub client, a fake config.cmd (installer `run`
function) that writes / deletes `.runner` like the real one (spike §3), a fake release cache and
synthetic runner directories under tmp_path. The `.runner` contents are SYNTHETIC samples built
from the field list in docs/m1-design.md §6. Tokens are obviously fake values.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import subprocess
import zipfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import psutil
import pytest
from pydantic import SecretStr

from runnerdeck.core import installer
from runnerdeck.core import manager as manager_mod
from runnerdeck.core.errors import (
    AuthError,
    ChecksumMismatchError,
    InstallError,
    InvalidInputError,
    NetworkError,
    NotFoundError,
    PermissionDeniedError,
    ProcessError,
    ReleaseError,
    RunnerDeckError,
    RunnerNotFoundError,
    StoreError,
)
from runnerdeck.core.github_client import GitHubClient
from runnerdeck.core.manager import (
    AddPlan,
    RemoteRemoval,
    RollbackError,
    RunnerManager,
    RunnerStatusRow,
    Step,
)
from runnerdeck.core.models import (
    AppConfig,
    DisplayState,
    LocalState,
    RemoteRunner,
    RemoteStatus,
    Repo,
    RunnerRecord,
    RunnerToken,
)
from runnerdeck.core.redact import redact
from runnerdeck.core.release import ReleaseCache
from runnerdeck.core.status import (
    DETAIL_EXTERNAL_LOCAL,
    DETAIL_EXTERNAL_REMOTE,
    DETAIL_REMOTE_UNKNOWN,
)
from runnerdeck.core.store import ConfigStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CONFIG_OK = (FIXTURES / "config" / "config_ok.txt").read_bytes()
REMOVE_OK = (FIXTURES / "config" / "remove_ok.txt").read_bytes()

OWNER, REPO = "octo", "demo"
URL = f"https://github.com/{OWNER}/{REPO}"
HOST = "BUILDBOX"
DEFAULT_NAME = f"{HOST}-{REPO}"
REG_TOKEN = "FAKEREGTOKEN0000000000000001"  # fake, shaped like a runner registration token
REMOVE_TOKEN = "FAKEREMOVETOKEN000000000002"
AGENT_ID = 4242
VERSION = "2.338.0"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


# --- fakes ---------------------------------------------------------------------------------


def _key(owner: str, repo: str) -> tuple[str, str]:
    return owner.casefold(), repo.casefold()


def remote(id: int, name: str, status: str = "offline", *, busy: bool = False) -> RemoteRunner:
    labels = [{"name": "self-hosted"}, {"name": "Windows"}, {"name": "X64"}]
    return RemoteRunner(id=id, name=name, status=status, busy=busy, labels=labels)


class FakeClient:
    """Records every call; per-method failures via `failures`, per-repo list failures too.

    `hidden`: repos the PAT cannot see. Like GitHub, every repo endpoint then answers 404:
    NotFoundError, except get_runner / delete_runner, which (like GitHubClient) map 404 to
    None / False unless `hidden_runner_raises` is set.
    """

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[Any, ...]] = []
        self.repos: dict[tuple[str, str], Repo] = {}
        self.runners: dict[tuple[str, str], list[RemoteRunner]] = {}
        self.failures: dict[str, BaseException] = {}
        self.list_failures: dict[tuple[str, str], BaseException] = {}
        self.hidden: set[tuple[str, str]] = set()
        self.hidden_runner_raises = False
        self.release_json: dict[str, Any] = {}
        self.download_bytes = b""
        self.closed = 0

    def _call(self, name: str, *args: Any) -> None:
        self.calls.append((name, *args))
        self.events.append(name)
        exc = self.failures.get(name)
        if exc is not None:
            raise exc

    def _is_hidden(
        self, method: str, owner: str, repo: str, suffix: str = "", *, maps_404: bool = False
    ) -> bool:
        """True if hidden and the caller maps the 404 to None / False; raises otherwise."""
        if _key(owner, repo) not in self.hidden:
            return False
        if maps_404 and not self.hidden_runner_raises:
            return True
        path = f"/repos/{owner}/{repo}{suffix}"
        raise NotFoundError(
            f"仓库不存在或 PAT 没有访问权限（{method} {path} -> 404：Not Found）",
            status=404,
            method=method,
            path=path,
        )

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def count(self, name: str) -> int:
        return self.names().count(name)

    def close(self) -> None:
        self.closed += 1

    def get_repo(self, owner: str, repo: str) -> Repo:
        self._call("get_repo", owner, repo)
        self._is_hidden("GET", owner, repo)
        try:
            return self.repos[_key(owner, repo)]
        except KeyError:
            raise NotFoundError("仓库不存在或 PAT 没有访问权限", status=404) from None

    def list_runners(self, owner: str, repo: str) -> list[RemoteRunner]:
        self._call("list_runners", owner, repo)
        self._is_hidden("GET", owner, repo, "/actions/runners")
        exc = self.list_failures.get(_key(owner, repo))
        if exc is not None:
            raise exc
        return list(self.runners.get(_key(owner, repo), []))

    def get_runner(self, owner: str, repo: str, runner_id: int) -> RemoteRunner | None:
        self._call("get_runner", owner, repo, runner_id)
        if self._is_hidden("GET", owner, repo, f"/actions/runners/{runner_id}", maps_404=True):
            return None
        return next((r for r in self.runners.get(_key(owner, repo), []) if r.id == runner_id), None)

    def create_registration_token(self, owner: str, repo: str) -> RunnerToken:
        self._call("create_registration_token", owner, repo)
        self._is_hidden("POST", owner, repo, "/actions/runners/registration-token")
        return RunnerToken(token=SecretStr(REG_TOKEN), expires_at=NOW + timedelta(hours=1))

    def create_remove_token(self, owner: str, repo: str) -> RunnerToken:
        self._call("create_remove_token", owner, repo)
        self._is_hidden("POST", owner, repo, "/actions/runners/remove-token")
        return RunnerToken(token=SecretStr(REMOVE_TOKEN), expires_at=NOW + timedelta(hours=1))

    def delete_runner(self, owner: str, repo: str, runner_id: int) -> bool:
        self._call("delete_runner", owner, repo, runner_id)
        if self._is_hidden("DELETE", owner, repo, f"/actions/runners/{runner_id}", maps_404=True):
            return False
        runners = self.runners.get(_key(owner, repo), [])
        for r in runners:
            if r.id == runner_id:
                runners.remove(r)
                return True
        return False

    def get_runner_release(self, version: str | None = None) -> dict[str, Any]:
        self._call("get_runner_release", version)
        return self.release_json

    def download(
        self, url: str, dest: Path, on_progress: Callable[[int, int | None], None] | None = None
    ) -> str:
        self._call("download", url)
        dest.write_bytes(self.download_bytes)
        if on_progress is not None:
            on_progress(len(self.download_bytes), len(self.download_bytes))
        return hashlib.sha256(self.download_bytes).hexdigest()


class FakeCache:
    def __init__(self, zip_path: Path, events: list[str]) -> None:
        self.zip_path = zip_path
        self.events = events
        self.error: BaseException | None = None
        self.progress_steps: list[tuple[int, int | None]] = [(0, 100), (5, 100), (55, 100)]
        self.progress_steps.append((100, 100))
        self.ensured: list[Any] = []
        self.pruned = 0
        self.prune_result: list[Path] = []

    def ensure(self, client: Any, info: Any, on_progress: Any = None) -> Path:
        self.events.append("ensure")
        self.ensured.append(info)
        if self.error is not None:
            raise self.error
        if on_progress is not None:
            for done, total in self.progress_steps:
                on_progress(done, total)
        return self.zip_path

    def prune(self, keep: int = 2) -> list[Path]:
        self.pruned += 1
        return self.prune_result


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
    text = "﻿" + json.dumps(runner_json(agent_id, name, url))
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
    """Stands in for installer.run_command: behaves like config.cmd per mode."""

    def __init__(self, events: list[str], client: FakeClient) -> None:
        self.events = events
        self.client = client
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.configure_mode = "ok"
        self.remove_mode = "ok"
        self.next_agent_id = AGENT_ID
        self.on_configure: Callable[[], None] | None = None
        self.written_name: str | None = None  # .runner agentName, if not the --name value

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        args = list(args)
        self.calls.append((args, kwargs))
        cwd = Path(kwargs["cwd"])
        if "remove" in args:
            self.events.append("config.cmd remove")
            return self._remove(args, cwd)
        self.events.append("config.cmd configure")
        if self.on_configure is not None:
            self.on_configure()
        return self._configure(args, cwd)

    def _register_remote(self, args: list[str]) -> int:
        owner, repo = args[args.index("--url") + 1].rstrip("/").split("/")[-2:]
        name = args[args.index("--name") + 1]
        agent_id = self.next_agent_id
        self.next_agent_id += 1
        self.client.runners.setdefault(_key(owner, repo), []).append(remote(agent_id, name))
        return agent_id

    def _configure(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
        mode = self.configure_mode
        name = args[args.index("--name") + 1]
        if mode == "timeout":
            raise subprocess.TimeoutExpired(args, 120, output=b"partial output\r\n")
        if mode == "fail":
            return subprocess.CompletedProcess(args, 1, b"Error: Http response code: NotFound\r\n")
        agent_id = self._register_remote(args)
        if mode == "added_only":
            out = b"\xe2\x88\x9a Runner successfully added\r\nFailed to save settings\r\n"
            return subprocess.CompletedProcess(args, 1, out)
        if mode == "replaced_only":  # --replace over an existing runner (synthetic output)
            out = b"\xe2\x88\x9a Successfully replaced the runner\r\nFailed to save settings\r\n"
            return subprocess.CompletedProcess(args, 1, out)
        if mode == "no_runner_file":
            return subprocess.CompletedProcess(args, 0, CONFIG_OK)
        write_registration(cwd, agent_id, self.written_name or name)
        if mode == "interrupt":
            raise KeyboardInterrupt
        if mode == "partial":
            return subprocess.CompletedProcess(args, 1, b"Error: disk full\r\n")
        assert mode == "ok", mode
        return subprocess.CompletedProcess(args, 0, CONFIG_OK)

    def _remove(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
        mode = self.remove_mode
        if mode == "fail":
            return subprocess.CompletedProcess(args, 1, b"Failed: Removing runner\r\n")
        runner = cwd / ".runner"
        agent_id = json.loads(runner.read_text(encoding="utf-8-sig"))["agentId"]
        runner.unlink()
        (cwd / ".credentials").unlink()
        if mode == "fail_after_delete":  # .runner is already gone when it fails
            return subprocess.CompletedProcess(args, 1, b"Failed: Removing runner\r\n")
        assert mode == "ok", mode
        for runners in self.client.runners.values():
            runners[:] = [r for r in runners if r.id != agent_id]
        return subprocess.CompletedProcess(args, 0, REMOVE_OK)


class FlakyStore(ConfigStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.fail_saves = 0
        self.fail_loads = 0
        self.fail_load_after_failed_save = False
        self.raise_after_save: BaseException | None = None

    def save(self, config: AppConfig) -> None:
        if self.raise_after_save is not None:
            exc, self.raise_after_save = self.raise_after_save, None
            super().save(config)
            raise exc
        if self.fail_saves:
            self.fail_saves -= 1
            if self.fail_load_after_failed_save:
                self.fail_loads = 1
            raise StoreError("无法写入配置文件（测试注入）")
        super().save(config)

    def load(self) -> AppConfig:
        if self.fail_loads:
            self.fail_loads -= 1
            raise StoreError("无法读取配置文件（测试注入）")
        return super().load()


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


def release_json(data: bytes, version: str = VERSION) -> dict[str, Any]:
    name = f"actions-runner-win-x64-{version}.zip"
    return {
        "tag_name": f"v{version}",
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


@dataclass
class FakeRunnerProcess:
    """Duck-typed RunnerProcess for statuses()."""

    state: LocalState
    running: bool
    state_since: datetime = NOW
    last_stop_forced: bool = False
    stopped_at: datetime | None = None
    current_job: str | None = None
    last_error: str | None = None

    def is_running(self) -> bool:
        return self.running


@dataclass
class Env:
    tmp: Path
    root: Path
    store: FlakyStore
    client: FakeClient
    cmd: FakeConfigCmd
    cache: FakeCache
    events: list[str]
    running: dict[str, list[Any]] = field(default_factory=dict)
    factory_calls: int = 0
    factory_error: BaseException | None = None
    manager: RunnerManager = field(init=False)

    def factory(self) -> Any:
        self.factory_calls += 1
        if self.factory_error is not None:
            raise self.factory_error
        return self.client

    def finder(self, runner_dir: Path) -> list[Any]:
        value = self.running.get(str(runner_dir))
        if isinstance(value, BaseException):
            raise value
        return value or []

    def config(self) -> AppConfig:
        return self.store.load()

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


@pytest.fixture(autouse=True)
def _propagate_logs() -> Iterator[None]:
    # logs.setup_logging() (other tests) turns propagation off; caplog needs it on.
    logger = logging.getLogger("runnerdeck")
    previous = logger.propagate
    logger.propagate = True
    yield
    logger.propagate = previous


@pytest.fixture(autouse=True)
def _no_inherited_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dev machines may set HTTPS_PROXY; child environments are compared exactly below.
    for name in ("https_proxy", "http_proxy", "HTTPS_PROXY", "HTTP_PROXY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    events: list[str] = []
    client = FakeClient(events)
    client.repos[_key(OWNER, REPO)] = Repo(
        name=REPO, full_name=f"{OWNER}/{REPO}", owner={"login": OWNER}, private=True, html_url=URL
    )
    zip_path = tmp_path / "pkg" / f"actions-runner-win-x64-{VERSION}.zip"
    data = build_runner_zip(zip_path)
    client.release_json = release_json(data)
    client.download_bytes = data

    store = FlakyStore(tmp_path / "config.json")
    config = AppConfig()
    root = tmp_path / "runners"
    config.settings.runners_root = str(root)
    config.settings.default_labels = ["gpu", "Windows"]
    store.save(config)

    e = Env(
        tmp=tmp_path,
        root=root,
        store=store,
        client=client,
        cmd=FakeConfigCmd(events, client),
        cache=FakeCache(zip_path, events),
        events=events,
    )
    e.manager = RunnerManager(
        store,
        e.factory,
        cache=e.cache,  # type: ignore[arg-type]
        hostname=HOST,
        installer_run=e.cmd,
        find_processes=e.finder,
        clock=lambda: NOW,
    )
    return e


def collect() -> tuple[list[tuple[str, str]], Callable[[str, str], None]]:
    seen: list[tuple[str, str]] = []
    return seen, lambda step, text: seen.append((step, text))


def is_cmd_exe(arg: str) -> bool:
    return os.path.basename(arg).lower() == "cmd.exe"  # absolute %SystemRoot% path on Windows


def proxy_vars(child_env: Mapping[str, str]) -> dict[str, str]:
    return {
        k.lower(): v for k, v in child_env.items() if k.lower() in ("https_proxy", "http_proxy")
    }


def both_proxies(url: str) -> dict[str, str]:
    return {"https_proxy": url, "http_proxy": url}


def set_proxy(env: Env, proxy: str | None) -> None:
    config = env.config()
    config.settings.proxy = proxy
    env.store.save(config)


def assert_no_repo_access(err: BaseException, full_name: str, kept: str) -> None:
    text = str(err)
    assert isinstance(err, NotFoundError)
    assert f"PAT 无法访问仓库 {full_name}" in text
    assert "Administration: Read and write" in text
    assert kept in text


def steps_of(progress: list[tuple[str, str]]) -> list[str]:
    out: list[str] = []
    for step, _ in progress:
        if not out or out[-1] != step:
            out.append(step)
    return out


def assert_rolled_back(env: Env, plan: AddPlan) -> None:
    assert not plan.target_dir.exists()
    assert env.config().runners == []


# --- plan_add ------------------------------------------------------------------------------


def test_plan_add_defaults(env: Env) -> None:
    before = env.store.path.read_bytes()
    plan = env.manager.plan_add(OWNER, REPO)

    assert (plan.owner, plan.repo, plan.full_name) == (OWNER, REPO, f"{OWNER}/{REPO}")
    assert plan.name == DEFAULT_NAME
    assert plan.labels == ("gpu",)  # default_labels, "Windows" is added by the runner itself
    assert plan.target_dir == env.root / REPO
    assert plan.target_dir.is_absolute()
    assert plan.private and not plan.archived
    assert plan.version is None
    assert plan.remote_conflict is None and plan.same_repo_runners == ()
    assert plan.warnings == () and not plan.needs_confirmation
    assert plan.repo_url == URL
    assert plan.actions_settings_url == f"{URL}/settings/actions"
    # read-only: nothing written, nothing created, client closed
    assert env.store.path.read_bytes() == before
    assert not env.root.exists()
    assert env.client.names() == ["get_repo", "list_runners"]
    assert env.client.closed == 1


def test_plan_add_uses_canonical_spelling_from_github(env: Env) -> None:
    plan = env.manager.plan_add("OCTO", "Demo")
    assert (plan.owner, plan.repo) == (OWNER, REPO)
    assert plan.name == DEFAULT_NAME
    assert plan.target_dir == env.root / REPO


def test_plan_add_keeps_input_spelling_if_github_spelling_is_unusable(env: Env) -> None:
    env.client.repos[_key(OWNER, REPO)] = env.client.repos[_key(OWNER, REPO)].model_copy(
        update={"owner": "-not-valid-"}
    )
    plan = env.manager.plan_add(OWNER, REPO)
    assert (plan.owner, plan.repo) == (OWNER, REPO)


def test_plan_add_explicit_name_labels_root_version(env: Env) -> None:
    plan = env.manager.plan_add(
        OWNER,
        REPO,
        name=" my-runner ",
        labels=["gpu", "self-hosted", "GPU", "linux-cross"],
        root=env.tmp / "other",
        version="v2.337.0",
    )
    assert plan.name == "my-runner"
    assert plan.labels == ("gpu", "linux-cross")
    assert plan.target_dir == env.tmp / "other" / REPO
    assert plan.version == "2.337.0"


def test_plan_add_labels_as_comma_string(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO, labels="a, b,a")
    assert plan.labels == ("a", "b")


@pytest.mark.parametrize("version", ["latest", "2.338", "v2.338.0.1", ""])
def test_plan_add_rejects_bad_version(env: Env, version: str) -> None:
    with pytest.raises(InvalidInputError, match="版本号"):
        env.manager.plan_add(OWNER, REPO, version=version)
    assert env.factory_calls == 0


@pytest.mark.parametrize(
    ("owner", "repo", "kw"),
    [("bad owner", REPO, {}), (OWNER, "a/b", {}), (OWNER, REPO, {"name": "bad name&"})],
)
def test_plan_add_rejects_invalid_input(env: Env, owner: str, repo: str, kw: Any) -> None:
    with pytest.raises(InvalidInputError):
        env.manager.plan_add(owner, repo, **kw)


def test_plan_add_invalid_label(env: Env) -> None:
    with pytest.raises(InvalidInputError, match="标签"):
        env.manager.plan_add(OWNER, REPO, labels=["ok", "not ok"])


def test_plan_add_public_repo_warns_about_fork_prs(env: Env) -> None:
    env.client.repos[_key(OWNER, REPO)] = env.client.repos[_key(OWNER, REPO)].model_copy(
        update={"private": False}
    )
    plan = env.manager.plan_add(OWNER, REPO)
    assert plan.needs_confirmation
    assert len(plan.warnings) == 1
    assert "公开仓库" in plan.warnings[0] and "fork PR" in plan.warnings[0]
    assert plan.actions_settings_url in plan.warnings[0]


def test_plan_add_archived_repo_warns(env: Env) -> None:
    env.client.repos[_key(OWNER, REPO)] = env.client.repos[_key(OWNER, REPO)].model_copy(
        update={"archived": True}
    )
    plan = env.manager.plan_add(OWNER, REPO)
    assert plan.archived
    assert any("已归档" in w for w in plan.warnings)


def test_plan_add_reports_remote_and_same_repo_runners(env: Env) -> None:
    env.client.runners[_key(OWNER, REPO)] = [
        remote(7, "other"),
        remote(9, DEFAULT_NAME.upper(), "online"),
    ]
    env.seed(name="laptop-demo", path=str(env.tmp / "elsewhere"))
    plan = env.manager.plan_add(OWNER, REPO)

    assert plan.remote_conflict is not None and plan.remote_conflict.id == 9
    assert plan.needs_confirmation
    assert plan.same_repo_runners == ("laptop-demo",)
    assert any("同名 runner" in w and "替换" in w for w in plan.warnings)
    assert any("laptop-demo" in w for w in plan.warnings)


def test_plan_add_rejects_local_same_name(env: Env) -> None:
    env.seed(owner="Octo", repo="DEMO", name=DEFAULT_NAME.lower())
    with pytest.raises(InvalidInputError, match="同名 runner"):
        env.manager.plan_add(OWNER, REPO)
    # same name in another repo is fine
    env.client.repos[_key(OWNER, "other")] = Repo(
        name="other", full_name=f"{OWNER}/other", owner=OWNER, private=True, html_url=URL
    )
    plan = env.manager.plan_add(OWNER, "other", name=DEFAULT_NAME)
    assert plan.name == DEFAULT_NAME


def test_plan_add_target_dir_skips_taken_paths(env: Env) -> None:
    (env.root / REPO).mkdir(parents=True)
    (env.root / REPO / "file.txt").write_text("x", encoding="utf-8")  # non-empty dir
    (env.root / f"{REPO}-2").write_text("x", encoding="utf-8")  # a file
    env.seed(name="old", path=str(env.root / f"{REPO}-3"))  # recorded, missing on disk
    (env.root / f"{REPO}-5").mkdir()  # empty and unrecorded, but -4 comes first

    plan = env.manager.plan_add(OWNER, REPO)
    assert plan.target_dir == env.root / f"{REPO}-4"


def test_plan_add_empty_existing_dir_is_reused(env: Env) -> None:
    (env.root / REPO).mkdir(parents=True)
    assert env.manager.plan_add(OWNER, REPO).target_dir == env.root / REPO


def test_plan_add_propagates_github_errors(env: Env) -> None:
    with pytest.raises(NotFoundError):
        env.manager.plan_add(OWNER, "missing")
    env.client.failures["list_runners"] = PermissionDeniedError("需要 Administration 权限")
    with pytest.raises(PermissionDeniedError):
        env.manager.plan_add(OWNER, REPO)
    assert env.client.closed == 2


# --- add_runner: success --------------------------------------------------------------------


def test_add_runner_happy_path(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    real_extract = installer.extract_runner

    def spy_extract(zip_path: Path, target: Path) -> None:
        env.events.append("extract")
        real_extract(zip_path, target)

    monkeypatch.setattr(installer, "extract_runner", spy_extract)
    plan = env.manager.plan_add(OWNER, REPO)
    env.events.clear()
    progress, report = collect()

    record = env.manager.add_runner(plan, progress=report)

    # record
    assert (record.owner, record.repo, record.name) == (OWNER, REPO, DEFAULT_NAME)
    assert record.labels == ["gpu"]
    assert record.path == str(plan.target_dir)
    assert record.github_runner_id == AGENT_ID
    assert env.config().runners == [record]
    # directory: extracted and registered
    assert (plan.target_dir / "config.cmd").is_file()
    assert (plan.target_dir / ".runner").is_file()
    # registration token fetched right before configure, after download and extraction
    assert env.events == [
        "get_runner_release",
        "ensure",
        "extract",
        "create_registration_token",
        "config.cmd configure",
    ]
    args, kwargs = env.cmd.calls[0]
    assert is_cmd_exe(args[0])
    assert args[1:4] == ["/c", "config.cmd", "--unattended"]
    assert args[args.index("--url") + 1] == URL
    assert args[args.index("--token") + 1] == REG_TOKEN
    assert args[args.index("--name") + 1] == DEFAULT_NAME
    assert args[args.index("--labels") + 1] == "gpu"
    assert "--replace" in args
    assert kwargs["cwd"] == plan.target_dir
    assert kwargs["stdin"] == subprocess.DEVNULL
    # progress: Chinese, ordered, no secrets
    assert steps_of(progress) == [
        Step.RELEASE,
        Step.DOWNLOAD,
        Step.EXTRACT,
        Step.TOKEN,
        Step.CONFIGURE,
        Step.VERIFY,
        Step.SAVE,
        Step.DONE,
    ]
    assert all(text and REG_TOKEN not in text for _, text in progress)
    assert "最新版" in progress[0][1]
    assert str(AGENT_ID) in progress[-1][1]
    # housekeeping: one client per call (plan_add + add_runner), each closed
    assert env.factory_calls == 2 and env.client.closed == 2
    assert env.cache.pruned == 1
    assert env.client.count("create_remove_token") == 0
    assert env.client.count("delete_runner") == 0


def test_add_runner_logs_name_mismatch_and_pruned_packages(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="runnerdeck")
    env.cmd.written_name = "Renamed-By-Runner"
    env.cache.prune_result = [env.tmp / "actions-runner-win-x64-2.300.0.zip"]
    record = env.manager.add_runner(env.manager.plan_add(OWNER, REPO))
    assert record.name == DEFAULT_NAME
    assert "Renamed-By-Runner" in caplog.text
    assert "pruned old runner package actions-runner-win-x64-2.300.0.zip" in caplog.text


def test_add_runner_survives_a_failing_prune(env: Env) -> None:
    def broken_prune(keep: int = 2) -> list[Path]:
        raise OSError("cache locked")

    env.cache.prune = broken_prune  # type: ignore[method-assign]
    record = env.manager.add_runner(env.manager.plan_add(OWNER, REPO))
    assert env.config().runners == [record]


def test_add_runner_download_progress_is_throttled(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    progress, report = collect()
    env.manager.add_runner(plan, progress=report)
    texts = [t for s, t in progress if s == Step.DOWNLOAD and "%" in t]
    # (0, 5) share the 0-10 % bucket; 55 % and 100 % each get one message.
    assert len(texts) == 3
    assert texts[0].startswith(f"下载 runner {VERSION}：0%")
    assert "100%" in texts[-1]


def test_add_runner_download_progress_without_total(env: Env) -> None:
    mb = 1024 * 1024
    env.cache.progress_steps = [(0, None), (3 * mb, None), (12 * mb, None), (15 * mb, None)]
    plan = env.manager.plan_add(OWNER, REPO)
    progress, report = collect()
    env.manager.add_runner(plan, progress=report)
    texts = [t for s, t in progress if s == Step.DOWNLOAD and "已下载" in t]
    assert len(texts) == 2  # buckets 0 MB and 10 MB
    assert "12.0 MB" in texts[1]


def test_add_runner_passes_version(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO, version="2.338.0")
    progress, report = collect()
    env.manager.add_runner(plan, progress=report)
    assert ("get_runner_release", "2.338.0") in env.client.calls
    assert "2.338.0" in progress[0][1]


def test_add_runner_fetches_a_fresh_token_every_time(env: Env) -> None:
    first = env.manager.add_runner(env.manager.plan_add(OWNER, REPO))
    second = env.manager.add_runner(env.manager.plan_add(OWNER, REPO, name="second"))
    assert env.client.count("create_registration_token") == 2
    tokens = [i for i, e in enumerate(env.events) if e == "create_registration_token"]
    configures = [i for i, e in enumerate(env.events) if e == "config.cmd configure"]
    assert all(t + 1 == c for t, c in zip(tokens, configures, strict=True))
    assert first.github_runner_id != second.github_runner_id
    assert second.path == str(env.root / f"{REPO}-2")
    assert [r.name for r in env.config().runners] == [DEFAULT_NAME, "second"]


def test_add_runner_token_is_redacted_everywhere(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="runnerdeck")
    plan = env.manager.plan_add(OWNER, REPO)
    progress, report = collect()
    env.manager.add_runner(plan, progress=report)

    assert REG_TOKEN not in redact(f"token {REG_TOKEN} here")
    assert REG_TOKEN not in caplog.text
    assert REG_TOKEN not in env.store.path.read_text(encoding="utf-8")
    assert all(REG_TOKEN not in text for _, text in progress)


def test_add_runner_with_real_release_cache(env: Env) -> None:
    cache = ReleaseCache(env.tmp / "cache")
    manager = RunnerManager(
        env.store,
        env.factory,
        cache=cache,
        hostname=HOST,
        installer_run=env.cmd,
        find_processes=env.finder,
    )
    record = manager.add_runner(manager.plan_add(OWNER, REPO))
    assert (env.tmp / "cache" / f"actions-runner-win-x64-{VERSION}.zip").is_file()
    assert not list((env.tmp / "cache").glob("*.part"))
    assert record.github_runner_id == AGENT_ID
    # second add reuses the verified zip
    manager.add_runner(manager.plan_add(OWNER, REPO, name="again"))
    assert env.client.count("download") == 1


# --- add_runner: failures before anything exists ---------------------------------------------


def test_add_runner_release_failure_creates_nothing(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    env.client.failures["get_runner_release"] = NetworkError("无法连接 GitHub")
    with pytest.raises(NetworkError):
        env.manager.add_runner(plan)
    assert not env.root.exists()
    assert env.client.count("create_registration_token") == 0
    assert env.client.closed == 2


def test_add_runner_bad_release_metadata(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    env.client.release_json = {"tag_name": "v2.338.0", "assets": []}
    with pytest.raises(ReleaseError, match="win-x64"):
        env.manager.add_runner(plan)
    assert not env.root.exists()


def test_add_runner_checksum_mismatch_creates_nothing(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    env.cache.error = ChecksumMismatchError("校验失败")
    with pytest.raises(ChecksumMismatchError):
        env.manager.add_runner(plan)
    assert not env.root.exists()
    assert env.client.count("create_registration_token") == 0
    assert env.config().runners == []


def test_add_runner_never_touches_a_dir_that_became_non_empty(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    plan.target_dir.mkdir(parents=True)
    (plan.target_dir / "precious.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(InstallError, match="不为空"):
        env.manager.add_runner(plan)
    assert (plan.target_dir / "precious.txt").read_text(encoding="utf-8") == "keep me"
    assert env.client.count("create_registration_token") == 0


def test_add_runner_rechecks_local_conflicts(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    env.seed(name=DEFAULT_NAME, path=str(env.tmp / "elsewhere"))
    with pytest.raises(InvalidInputError, match="同名 runner"):
        env.manager.add_runner(plan)
    assert env.client.count("get_runner_release") == 0


def test_add_runner_rechecks_target_dir_owner(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    env.seed(name="other", path=str(plan.target_dir))
    with pytest.raises(InvalidInputError, match="已被 runner"):
        env.manager.add_runner(plan)


def test_add_runner_rejects_relative_target(env: Env) -> None:
    plan = AddPlan(OWNER, REPO, DEFAULT_NAME, (), Path("relative/demo"), private=True)
    with pytest.raises(InvalidInputError, match="绝对路径"):
        env.manager.add_runner(plan)
    assert env.factory_calls == 0


# --- add_runner: rollback branches ------------------------------------------------------------


def test_rollback_when_registration_token_fails(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    env.client.failures["create_registration_token"] = AuthError("PAT 无效或已过期")
    progress, report = collect()
    with pytest.raises(AuthError):
        env.manager.add_runner(plan, progress=report)
    assert_rolled_back(env, plan)
    assert env.cmd.calls == []
    assert env.client.count("create_remove_token") == 0
    assert env.client.count("delete_runner") == 0
    assert progress[-1][0] == Step.ROLLBACK
    assert env.cache.pruned == 0


def test_rollback_when_configure_fails_without_registration(env: Env) -> None:
    env.cmd.configure_mode = "fail"
    plan = env.manager.plan_add(OWNER, REPO)
    with pytest.raises(InstallError) as info:
        env.manager.add_runner(plan)
    assert info.value.returncode == 1
    assert "NotFound" in info.value.output
    assert_rolled_back(env, plan)
    assert env.client.count("create_remove_token") == 0
    assert env.client.count("delete_runner") == 0
    assert env.client.count("list_runners") == 1  # only plan_add's


def test_rollback_when_configure_times_out(env: Env) -> None:
    env.cmd.configure_mode = "timeout"
    plan = env.manager.plan_add(OWNER, REPO)
    with pytest.raises(InstallError, match="超时"):
        env.manager.add_runner(plan)
    assert_rolled_back(env, plan)
    assert env.client.count("delete_runner") == 0


def test_rollback_unconfigures_when_configure_failed_after_writing_runner_file(
    env: Env,
) -> None:
    env.cmd.configure_mode = "partial"
    plan = env.manager.plan_add(OWNER, REPO)
    progress, report = collect()
    with pytest.raises(InstallError, match="disk full"):
        env.manager.add_runner(plan, progress=report)
    assert_rolled_back(env, plan)
    assert env.events[-2:] == ["create_remove_token", "config.cmd remove"]
    remove_args = env.cmd.calls[-1][0]
    assert remove_args[remove_args.index("--token") + 1] == REMOVE_TOKEN
    assert env.client.count("delete_runner") == 0
    assert env.client.runners[_key(OWNER, REPO)] == []
    assert any("注销" in text for step, text in progress if step == Step.ROLLBACK)


@pytest.mark.parametrize("mode", ["added_only", "replaced_only"])
def test_rollback_deletes_by_name_when_only_the_added_line_was_printed(env: Env, mode: str) -> None:
    env.cmd.configure_mode = mode
    plan = env.manager.plan_add(OWNER, REPO)
    with pytest.raises(InstallError):
        env.manager.add_runner(plan)
    assert_rolled_back(env, plan)
    assert env.client.count("create_remove_token") == 0  # nothing for config.cmd to remove
    assert ("delete_runner", OWNER, REPO, AGENT_ID) in env.client.calls
    assert env.client.runners[_key(OWNER, REPO)] == []


def test_rollback_when_runner_file_missing_after_success(env: Env) -> None:
    env.cmd.configure_mode = "no_runner_file"
    plan = env.manager.plan_add(OWNER, REPO)
    with pytest.raises(InstallError, match="没有生成 .runner"):
        env.manager.add_runner(plan)
    assert_rolled_back(env, plan)
    assert ("delete_runner", OWNER, REPO, AGENT_ID) in env.client.calls


def test_rollback_by_name_finds_nothing(env: Env) -> None:
    env.cmd.configure_mode = "added_only"
    plan = env.manager.plan_add(OWNER, REPO)

    def vanish(*_: Any) -> None:
        env.client.runners.clear()

    env.cmd.on_configure = None
    real = env.cmd._register_remote
    env.cmd._register_remote = lambda args: (real(args), vanish())[0]  # type: ignore[method-assign]
    with pytest.raises(InstallError):
        env.manager.add_runner(plan)
    assert_rolled_back(env, plan)
    assert env.client.count("delete_runner") == 0


def test_rollback_after_save_failure_unconfigures(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    before = env.store.path.read_bytes()
    env.store.fail_saves = 1
    with pytest.raises(StoreError):
        env.manager.add_runner(plan)
    assert not plan.target_dir.exists()
    assert env.store.path.read_bytes() == before  # never half-written
    assert env.events[-2:] == ["create_remove_token", "config.cmd remove"]
    assert env.client.runners[_key(OWNER, REPO)] == []


def test_rollback_falls_back_to_api_delete_by_agent_id(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    env.store.fail_saves = 1
    env.cmd.remove_mode = "fail"
    with pytest.raises(StoreError):  # rollback complete -> original error type
        env.manager.add_runner(plan)
    assert_rolled_back(env, plan)
    names = env.client.names()
    assert names.index("create_remove_token") < names.index("delete_runner")
    assert ("delete_runner", OWNER, REPO, AGENT_ID) in env.client.calls
    assert env.client.runners[_key(OWNER, REPO)] == []


def test_rollback_uses_api_when_remove_token_fails(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    env.store.fail_saves = 1
    env.client.failures["create_remove_token"] = NetworkError("连接超时")
    with pytest.raises(StoreError):
        env.manager.add_runner(plan)
    assert_rolled_back(env, plan)
    assert "config.cmd remove" not in env.events
    assert ("delete_runner", OWNER, REPO, AGENT_ID) in env.client.calls


def test_rollback_incomplete_raises_rollback_error(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    env.store.fail_saves = 1
    env.cmd.remove_mode = "fail"
    env.client.failures["delete_runner"] = NetworkError("网络中断")
    with pytest.raises(RollbackError) as info:
        env.manager.add_runner(plan)
    err = info.value
    assert isinstance(err.original, StoreError)
    assert err.__cause__ is err.original
    assert len(err.problems) == 1
    assert "GitHub 上可能残留" in err.problems[0] and DEFAULT_NAME in err.problems[0]
    assert "测试注入" in str(err) and "回滚未完成" in str(err)
    # the directory is still removed and no record exists
    assert_rolled_back(env, plan)


@pytest.mark.parametrize("runner_raises", [False, True])
def test_rollback_api_404_on_invisible_repo_is_not_reported_as_undone(
    env: Env, runner_raises: bool
) -> None:
    # The PAT loses access to the repo while config.cmd runs; DELETE then answers 404.
    plan = env.manager.plan_add(OWNER, REPO)
    env.store.fail_saves = 1
    env.client.hidden_runner_raises = runner_raises
    env.cmd.on_configure = lambda: env.client.hidden.add(_key(OWNER, REPO))
    with pytest.raises(RollbackError) as info:
        env.manager.add_runner(plan)
    assert isinstance(info.value.original, StoreError)
    (problem,) = info.value.problems
    assert "GitHub 上可能残留" in problem and DEFAULT_NAME in problem
    assert "PAT" in problem and "访问" in problem
    assert ("delete_runner", OWNER, REPO, AGENT_ID) in env.client.calls
    assert [r.id for r in env.client.runners[_key(OWNER, REPO)]] == [AGENT_ID]
    assert_rolled_back(env, plan)  # the local side is still undone


def test_rollback_api_404_with_visible_repo_counts_as_undone(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    env.store.fail_saves = 1
    env.cmd.remove_mode = "fail"

    def already_gone(owner: str, repo: str, runner_id: int) -> bool:
        env.client.calls.append(("delete_runner", owner, repo, runner_id))
        env.client.runners[_key(owner, repo)].clear()
        return False

    monkeypatch.setattr(env.client, "delete_runner", already_gone)
    with pytest.raises(StoreError):  # rollback complete -> original error type
        env.manager.add_runner(plan)
    assert_rolled_back(env, plan)
    assert env.client.names()[-2:] == ["delete_runner", "list_runners"]


def test_rollback_reports_directory_that_cannot_be_deleted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.cmd.configure_mode = "fail"
    plan = env.manager.plan_add(OWNER, REPO)

    def refuse(path: Path, *, keep_work: bool = False) -> None:
        raise InstallError("文件被占用")

    monkeypatch.setattr(installer, "remove_runner_dir", refuse)
    with pytest.raises(RollbackError) as info:
        env.manager.add_runner(plan)
    assert isinstance(info.value.original, InstallError)
    assert "未能删除" in info.value.problems[0] and "文件被占用" in info.value.problems[0]


def test_rollback_reports_unverifiable_record(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    env.store.fail_saves = 1
    env.store.fail_load_after_failed_save = True
    with pytest.raises(RollbackError) as info:
        env.manager.add_runner(plan)
    assert "无法确认 runner 记录已删除" in info.value.problems[0]
    assert not plan.target_dir.exists()
    assert env.events[-1] == "config.cmd remove"


def test_rollback_removes_a_record_written_just_before_the_failure(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    env.store.raise_after_save = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        env.manager.add_runner(plan)
    assert_rolled_back(env, plan)
    assert env.events[-1] == "config.cmd remove"


def test_rollback_when_conflicting_record_appears_during_install(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    other = RunnerRecord(owner=OWNER, repo=REPO, name=DEFAULT_NAME, path=str(env.tmp / "x"))

    def sneak_in() -> None:
        config = env.config()
        config.upsert(other)
        env.store.save(config)

    env.cmd.on_configure = sneak_in
    with pytest.raises(InvalidInputError, match="同名 runner"):
        env.manager.add_runner(plan)
    assert not plan.target_dir.exists()
    assert env.config().runners == [other]  # the other record is left alone
    assert env.events[-1] == "config.cmd remove"


def test_rollback_on_keyboard_interrupt_during_configure(env: Env) -> None:
    env.cmd.configure_mode = "interrupt"
    plan = env.manager.plan_add(OWNER, REPO)
    with pytest.raises(KeyboardInterrupt):
        env.manager.add_runner(plan)
    assert_rolled_back(env, plan)
    assert env.events[-1] == "config.cmd remove"


def test_rollback_incomplete_after_non_runnerdeck_error_reraises_original(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    env.cmd.configure_mode = "interrupt"
    env.cmd.remove_mode = "fail"
    env.client.failures["delete_runner"] = NetworkError("网络中断")
    plan = env.manager.plan_add(OWNER, REPO)
    with pytest.raises(KeyboardInterrupt):
        env.manager.add_runner(plan)
    assert "rollback of" in caplog.text
    assert not plan.target_dir.exists()


def test_progress_exception_cancels_and_rolls_back(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    seen: list[str] = []

    def report(step: str, text: str) -> None:
        seen.append(step)
        if step == Step.CONFIGURE:
            raise RuntimeError("用户取消")

    with pytest.raises(RuntimeError, match="用户取消"):
        env.manager.add_runner(plan, progress=report)
    assert_rolled_back(env, plan)
    assert env.cmd.calls == []
    assert seen[-1] == Step.ROLLBACK


def test_failing_progress_does_not_break_rollback_or_done(env: Env) -> None:
    plan = env.manager.plan_add(OWNER, REPO)
    env.cmd.configure_mode = "partial"

    def report(step: str, text: str) -> None:
        if step in (Step.CONFIGURE, Step.ROLLBACK):
            raise RuntimeError("callback bug")

    with pytest.raises(RuntimeError):
        env.manager.add_runner(plan, progress=report)
    assert_rolled_back(env, plan)

    env.cmd.configure_mode = "ok"

    def broken_done(step: str, text: str) -> None:
        if step == Step.DONE:
            raise RuntimeError("callback bug")

    record = env.manager.add_runner(env.manager.plan_add(OWNER, REPO), progress=broken_done)
    assert env.config().runners == [record]


# --- config.cmd environment ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("proxy", "expected"),
    [(None, None), ("127.0.0.1:7890", "http://127.0.0.1:7890"), ("http://p:8080", "http://p:8080")],
)
def test_config_cmd_gets_the_proxy(env: Env, proxy: str | None, expected: str | None) -> None:
    # The runner reads https_proxy / http_proxy when it registers and unregisters, not only
    # in run.cmd, so every config.cmd call needs the RunnerDeck proxy.
    set_proxy(env, proxy)
    record = env.manager.add_runner(env.manager.plan_add(OWNER, REPO))  # configure
    env.manager.remove_runner(record.id)  # config.cmd remove
    env.cmd.configure_mode = "partial"
    with pytest.raises(InstallError):  # configure, then the rollback's config.cmd remove
        env.manager.add_runner(env.manager.plan_add(OWNER, REPO))

    assert [("remove" in args) for args, _ in env.cmd.calls] == [False, True, False, True]
    want = {} if expected is None else both_proxies(expected)
    for _, kwargs in env.cmd.calls:
        assert proxy_vars(kwargs.get("env") or {}) == want


# --- remove_runner ---------------------------------------------------------------------------


def seed_runner(env: Env, **kw: Any) -> RunnerRecord:
    """A registered runner: directory + GitHub entry + record."""
    agent_id = kw.pop("agent_id", AGENT_ID)
    name = kw.get("name", DEFAULT_NAME)
    path = Path(kw.pop("path", env.root / REPO))
    make_runner_dir(path, agent_id=agent_id, name=name, credentials=kw.pop("credentials", True))
    env.client.runners.setdefault(_key(OWNER, REPO), []).append(remote(agent_id, name))
    kw.setdefault("github_runner_id", agent_id)
    return env.seed(path=str(path), **kw)


def test_remove_runner_happy_path(env: Env) -> None:
    record = seed_runner(env)
    progress, report = collect()
    result = env.manager.remove_runner(record.name, progress=report)

    assert result.record == record
    assert result.remote is RemoteRemoval.UNCONFIGURED
    assert result.runner_id is None
    assert result.dir_removed and result.warnings == ()
    assert not Path(record.path).exists()
    assert env.config().runners == []
    assert env.client.runners[_key(OWNER, REPO)] == []
    assert env.client.count("delete_runner") == 0
    args = env.cmd.calls[0][0]
    assert is_cmd_exe(args[0])
    assert args[1:] == ["/c", "config.cmd", "remove", "--token", REMOVE_TOKEN]
    assert steps_of(progress) == [
        Step.CHECK,
        Step.READ,
        Step.UNCONFIGURE,
        Step.DELETE_DIR,
        Step.SAVE,
        Step.DONE,
    ]
    assert env.client.closed == 1


def test_remove_runner_refuses_while_running(env: Env) -> None:
    record = seed_runner(env)
    env.running[record.path] = [SimpleNamespace(pid=111), SimpleNamespace(pid=222)]
    with pytest.raises(ProcessError, match="111, 222") as info:
        env.manager.remove_runner(record.id)
    assert "请先停止" in str(info.value)
    assert env.factory_calls == 0
    assert Path(record.path, ".runner").is_file()
    assert env.config().runners == [record]


def test_remove_runner_process_scan_failure(env: Env) -> None:
    record = seed_runner(env)
    env.running[record.path] = psutil.AccessDenied(pid=5)  # type: ignore[assignment]
    with pytest.raises(ProcessError, match="无法检查"):
        env.manager.remove_runner(record.id)
    assert env.config().runners == [record]


def test_remove_reads_agent_id_before_config_remove_deletes_it(env: Env) -> None:
    # spike §3: config.cmd remove deletes .runner; the record has no id to fall back on.
    record = seed_runner(env, github_runner_id=None)
    env.cmd.remove_mode = "fail_after_delete"
    result = env.manager.remove_runner(record.name)

    assert result.remote is RemoteRemoval.API_DELETED
    assert result.runner_id == AGENT_ID
    assert env.client.names()[-1] == "delete_runner"
    assert ("delete_runner", OWNER, REPO, AGENT_ID) in env.client.calls
    assert env.client.count("list_runners") == 0
    assert any("config.cmd remove 失败" in w for w in result.warnings)
    assert result.dir_removed
    assert env.config().runners == []


def test_remove_prefers_runner_file_id_over_record(env: Env) -> None:
    record = seed_runner(env, github_runner_id=1)
    env.cmd.remove_mode = "fail"
    result = env.manager.remove_runner(record.name)
    assert ("delete_runner", OWNER, REPO, AGENT_ID) in env.client.calls
    assert result.runner_id == AGENT_ID


def test_remove_runner_dir_already_gone_uses_api(env: Env) -> None:
    record = env.seed(path=str(env.root / "gone"), github_runner_id=77)
    env.client.runners[_key(OWNER, REPO)] = [remote(77, DEFAULT_NAME)]
    progress, report = collect()
    result = env.manager.remove_runner(record.name, progress=report)

    assert result.remote is RemoteRemoval.API_DELETED
    assert result.runner_id == 77
    assert not result.dir_removed
    assert env.cmd.calls == []
    assert env.client.count("create_remove_token") == 0
    assert Step.READ not in steps_of(progress) and Step.DELETE_DIR not in steps_of(progress)
    assert env.config().runners == []


def test_remove_runner_404_counts_as_removed(env: Env) -> None:
    record = env.seed(path=str(env.root / "gone"), github_runner_id=77)
    result = env.manager.remove_runner(record.id)
    assert result.remote is RemoteRemoval.ALREADY_GONE
    assert result.runner_id == 77
    assert env.config().runners == []
    # the 404 only counts once the repo's runner list proved the PAT can see the repo
    assert env.client.names() == ["delete_runner", "list_runners"]


KEPT_ON_REMOVE = "目录和记录都保留"


def test_remove_runner_invisible_repo_keeps_everything(env: Env) -> None:
    # e.g. a fine-grained PAT that no longer selects the repo: every repo endpoint is 404
    record = seed_runner(env)
    env.client.hidden.add(_key(OWNER, REPO))
    progress, report = collect()
    with pytest.raises(NotFoundError) as info:
        env.manager.remove_runner(record.id, progress=report)
    assert_no_repo_access(info.value, f"{OWNER}/{REPO}", KEPT_ON_REMOVE)
    assert env.cmd.calls == []  # config.cmd remove never ran
    assert env.client.count("delete_runner") == 0
    assert Path(record.path, ".runner").is_file()
    assert Path(record.path, ".credentials").is_file()
    assert env.config().runners == [record]
    assert [r.id for r in env.client.runners[_key(OWNER, REPO)]] == [AGENT_ID]
    assert Step.DELETE_DIR not in steps_of(progress) and Step.DONE not in steps_of(progress)


def test_remove_runner_local_only_is_the_way_out_for_an_invisible_repo(env: Env) -> None:
    # Repo deleted / access revoked: every GitHub call is 404, so only a local removal works.
    record = seed_runner(env)
    env.client.hidden.add(_key(OWNER, REPO))
    progress, report = collect()
    result = env.manager.remove_runner(record.id, local_only=True, progress=report)
    assert result.remote is RemoteRemoval.SKIPPED
    assert env.client.calls == []  # GitHub was not touched at all
    assert env.cmd.calls == []
    assert result.dir_removed and not Path(record.path).exists()
    assert env.config().runners == []
    assert any("没有在 GitHub 上注销" in w for w in result.warnings)
    assert Step.UNCONFIGURE not in steps_of(progress) and Step.API_DELETE not in steps_of(progress)


def test_remove_runner_local_only_still_refuses_while_running(env: Env) -> None:
    record = seed_runner(env)
    env.running[record.path] = [SimpleNamespace(pid=4242)]
    with pytest.raises(ProcessError):
        env.manager.remove_runner(record.id, local_only=True)
    assert env.config().runners == [record]


@pytest.mark.parametrize(
    ("runner_id", "runner_raises"),
    [(77, False), (77, True), (None, False)],
    ids=["delete-404", "delete-raises", "no-id"],
)
def test_remove_runner_dir_gone_invisible_repo_keeps_record(
    env: Env, runner_id: int | None, runner_raises: bool
) -> None:
    record = env.seed(path=str(env.root / "gone"), github_runner_id=runner_id)
    env.client.runners[_key(OWNER, REPO)] = [remote(77, DEFAULT_NAME)]
    env.client.hidden.add(_key(OWNER, REPO))
    env.client.hidden_runner_raises = runner_raises
    with pytest.raises(NotFoundError) as info:
        env.manager.remove_runner(record.id)
    assert_no_repo_access(info.value, f"{OWNER}/{REPO}", KEPT_ON_REMOVE)
    assert env.config().runners == [record]


def test_remove_runner_404_while_github_still_lists_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = env.seed(path=str(env.root / "gone"), github_runner_id=77)
    env.client.runners[_key(OWNER, REPO)] = [remote(77, DEFAULT_NAME)]
    monkeypatch.setattr(env.client, "delete_runner", lambda owner, repo, runner_id: False)
    with pytest.raises(RunnerDeckError, match="仍有它"):
        env.manager.remove_runner(record.id)
    assert env.config().runners == [record]


def test_remove_token_404_on_a_visible_repo_still_falls_back_to_the_api(env: Env) -> None:
    record = seed_runner(env)
    env.client.failures["create_remove_token"] = NotFoundError("Not Found", status=404)
    result = env.manager.remove_runner(record.id)
    assert result.remote is RemoteRemoval.API_DELETED
    assert env.client.names() == ["create_remove_token", "list_runners", "delete_runner"]
    assert any("config.cmd remove 失败" in w for w in result.warnings)
    assert env.config().runners == []


@pytest.mark.parametrize("dir_exists", [True, False])
def test_remove_runner_invisible_repo_with_the_real_client(env: Env, dir_exists: bool) -> None:
    """GitHubClient on a transport where every request answers 404 like an invisible repo."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(404, json={"message": "Not Found"})

    client = GitHubClient("github_pat_" + "F" * 40, transport=httpx.MockTransport(handler))
    manager = RunnerManager(
        env.store,
        lambda: client,
        cache=env.cache,  # type: ignore[arg-type]
        hostname=HOST,
        installer_run=env.cmd,
        find_processes=env.finder,
    )
    if dir_exists:
        record = seed_runner(env)
    else:
        record = env.seed(path=str(env.root / "gone"), github_runner_id=AGENT_ID)
    with pytest.raises(NotFoundError) as info:
        manager.remove_runner(record.id)
    assert_no_repo_access(info.value, f"{OWNER}/{REPO}", KEPT_ON_REMOVE)
    assert env.cmd.calls == []
    assert env.config().runners == [record]
    if dir_exists:
        assert Path(record.path, ".runner").is_file()
        assert seen[0] == f"POST /repos/{OWNER}/{REPO}/actions/runners/remove-token"
    else:
        assert seen[0] == f"DELETE /repos/{OWNER}/{REPO}/actions/runners/{AGENT_ID}"


def test_remove_runner_without_any_id_matches_by_name(env: Env) -> None:
    record = env.seed(path=str(env.root / "gone"), github_runner_id=None)
    env.client.runners[_key(OWNER, REPO)] = [remote(5, DEFAULT_NAME.upper())]
    result = env.manager.remove_runner(record.id)
    assert result.remote is RemoteRemoval.API_DELETED
    assert result.runner_id == 5


def test_remove_runner_without_any_id_and_no_match(env: Env) -> None:
    record = env.seed(path=str(env.root / "gone"), github_runner_id=None)
    result = env.manager.remove_runner(record.id)
    assert result.remote is RemoteRemoval.ALREADY_GONE
    assert result.runner_id is None
    assert env.client.count("delete_runner") == 0
    assert env.config().runners == []


def test_remove_runner_api_failure_keeps_everything(env: Env) -> None:
    record = seed_runner(env)
    env.cmd.remove_mode = "fail"
    env.client.failures["delete_runner"] = PermissionDeniedError("需要 Administration 权限")
    with pytest.raises(PermissionDeniedError):
        env.manager.remove_runner(record.id)
    assert Path(record.path, "config.cmd").is_file()
    assert env.config().runners == [record]


def test_remove_runner_auth_failure_keeps_everything(env: Env) -> None:
    record = seed_runner(env)
    env.client.failures["create_remove_token"] = AuthError("PAT 无效或已过期")
    env.client.failures["delete_runner"] = AuthError("PAT 无效或已过期")
    with pytest.raises(AuthError):
        env.manager.remove_runner(record.id)
    assert env.cmd.calls == []
    assert Path(record.path, ".runner").is_file()
    assert env.config().runners == [record]


def test_remove_runner_keep_work(env: Env) -> None:
    record = seed_runner(env)
    progress, report = collect()
    result = env.manager.remove_runner(record.id, keep_work=True, progress=report)
    assert result.dir_removed
    assert sorted(p.name for p in Path(record.path).iterdir()) == ["_work"]
    assert (Path(record.path) / "_work" / "keep.txt").is_file()
    assert any("保留 _work" in t for s, t in progress if s == Step.DELETE_DIR)


def test_remove_runner_unconfigured_dir_goes_straight_to_api(env: Env) -> None:
    record = seed_runner(env, credentials=False)
    result = env.manager.remove_runner(record.id)
    assert env.cmd.calls == []
    assert result.remote is RemoteRemoval.API_DELETED
    assert result.dir_removed


def test_remove_runner_corrupt_runner_file_uses_record_id(env: Env) -> None:
    record = seed_runner(env, github_runner_id=AGENT_ID)
    Path(record.path, ".runner").write_text("{not json", encoding="utf-8")
    env.cmd.remove_mode = "fail"
    result = env.manager.remove_runner(record.id)
    assert ("delete_runner", OWNER, REPO, AGENT_ID) in env.client.calls
    assert any("无法读取 .runner" in w for w in result.warnings)


def test_remove_runner_dir_deletion_failure_is_a_warning(env: Env) -> None:
    record = seed_runner(env)
    Path(record.path, "run.cmd").unlink()  # no longer looks like a runner dir: never deleted
    result = env.manager.remove_runner(record.id)
    assert not result.dir_removed
    assert any("目录未删除" in w for w in result.warnings)
    assert Path(record.path).is_dir()
    assert env.config().runners == []


def test_remove_runner_unknown_key(env: Env) -> None:
    with pytest.raises(RunnerNotFoundError):
        env.manager.remove_runner("nope")


def test_remove_runner_store_failure_is_retryable(env: Env) -> None:
    record = seed_runner(env)
    env.store.fail_saves = 1
    with pytest.raises(StoreError):
        env.manager.remove_runner(record.id)
    assert env.config().runners == [record]
    # retry: registration and directory are gone already, only the record is left
    result = env.manager.remove_runner(record.id)
    assert result.remote is RemoteRemoval.ALREADY_GONE
    assert env.config().runners == []


# --- import_runner -------------------------------------------------------------------------


def test_import_runner(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    unrelated = env.seed(name="unrelated", path=str(env.root / "u"), github_runner_id=1)
    path = make_runner_dir(env.tmp / "manual" / "actions-runner", name="hand-made")
    env.client.runners[_key(OWNER, REPO)] = [
        RemoteRunner(
            id=AGENT_ID,
            name="renamed-on-github",
            status="online",
            labels=["self-hosted", "Windows", "X64", "gpu", "has space"],
        )
    ]
    record = env.manager.import_runner(path)
    assert (record.owner, record.repo, record.name) == (OWNER, REPO, "hand-made")
    assert record.github_runner_id == AGENT_ID
    assert record.labels == ["gpu"]
    assert record.path == str(path)
    assert env.config().runners == [unrelated, record]
    assert ("get_runner", OWNER, REPO, AGENT_ID) in env.client.calls
    assert "renamed-on-github" in caplog.text


def test_import_runner_same_path_returns_existing(env: Env) -> None:
    path = make_runner_dir(env.tmp / "manual")
    existing = env.seed(path=str(path))
    assert env.manager.import_runner(Path(str(path) + "/")) == existing
    assert env.manager.import_runner(path / ".." / "manual") == existing
    assert env.factory_calls == 0


def test_import_runner_missing_on_github(env: Env) -> None:
    path = make_runner_dir(env.tmp / "manual")
    with pytest.raises(RunnerDeckError, match="重新添加") as info:
        env.manager.import_runner(path)
    assert type(info.value) is RunnerDeckError
    assert env.config().runners == []
    # "gone" only after the repo's runner list proved the PAT can see the repo
    assert env.client.names() == ["get_runner", "list_runners"]


@pytest.mark.parametrize("runner_raises", [False, True])
def test_import_runner_invisible_repo_does_not_advise_deleting(
    env: Env, runner_raises: bool
) -> None:
    # The PAT covers only octo/demo; the directory is a working runner of someone/repoB.
    path = make_runner_dir(env.tmp / "manual", url="https://github.com/someone/repoB")
    env.client.runners[_key("someone", "repoB")] = [remote(AGENT_ID, DEFAULT_NAME, "online")]
    env.client.hidden.add(_key("someone", "repoB"))
    env.client.hidden_runner_raises = runner_raises
    with pytest.raises(NotFoundError) as info:
        env.manager.import_runner(path)
    assert_no_repo_access(info.value, "someone/repoB", "请不要删除该目录")
    assert "重新添加" not in str(info.value)
    assert env.config().runners == []
    assert Path(path, ".runner").is_file()


def test_import_runner_trusts_the_list_over_a_404(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_runner_dir(env.tmp / "manual")
    env.client.runners[_key(OWNER, REPO)] = [remote(AGENT_ID, DEFAULT_NAME)]
    monkeypatch.setattr(env.client, "get_runner", lambda owner, repo, runner_id: None)
    record = env.manager.import_runner(path)
    assert record.github_runner_id == AGENT_ID
    assert env.config().runners == [record]


@pytest.mark.parametrize(
    ("setup", "match"),
    [
        ("missing", "目录不存在"),
        ("not_runner", "不是 runner 目录"),
        ("unconfigured", "还没有注册"),
        ("no_credentials", "还没有注册"),
    ],
)
def test_import_runner_rejects_bad_dirs(env: Env, setup: str, match: str) -> None:
    path = env.tmp / "manual"
    if setup == "not_runner":
        path.mkdir()
        (path / "config.cmd").write_text("x", encoding="utf-8")
    elif setup == "unconfigured":
        make_runner_dir(path, agent_id=None)
    elif setup == "no_credentials":
        make_runner_dir(path, credentials=False)
    with pytest.raises(InvalidInputError, match=match):
        env.manager.import_runner(path)
    assert env.factory_calls == 0


def test_import_runner_rejects_non_repo_url(env: Env) -> None:
    path = make_runner_dir(env.tmp / "manual", url="https://github.com/enterprises/acme")
    with pytest.raises(InvalidInputError):
        env.manager.import_runner(path)


def test_import_runner_moved_directory_updates_record(env: Env) -> None:
    old = env.seed(path=str(env.tmp / "old-location"), github_runner_id=AGENT_ID)
    env.client.runners[_key(OWNER, REPO)] = [remote(AGENT_ID, DEFAULT_NAME)]
    path = make_runner_dir(env.tmp / "new-location")
    moved = env.manager.import_runner(path)
    assert moved.id == old.id
    assert moved.path == str(path)
    assert env.config().runners == [moved]


def test_import_runner_rejects_second_copy_of_a_registration(env: Env) -> None:
    original = make_runner_dir(env.tmp / "original")
    env.seed(path=str(original), github_runner_id=AGENT_ID)
    env.client.runners[_key(OWNER, REPO)] = [remote(AGENT_ID, DEFAULT_NAME)]
    copy = make_runner_dir(env.tmp / "copy")
    with pytest.raises(InvalidInputError, match="共用同一个注册"):
        env.manager.import_runner(copy)
    assert len(env.config().runners) == 1


def test_import_runner_race_with_same_path(env: Env) -> None:
    path = make_runner_dir(env.tmp / "manual")
    env.client.runners[_key(OWNER, REPO)] = [remote(AGENT_ID, DEFAULT_NAME)]
    existing = RunnerRecord(owner=OWNER, repo=REPO, name="x", path=str(path))
    real_get = env.client.get_runner

    def get_and_race(owner: str, repo: str, runner_id: int) -> RemoteRunner | None:
        env.seed(**existing.model_dump(exclude={"created_at"}))
        return real_get(owner, repo, runner_id)

    env.client.get_runner = get_and_race  # type: ignore[method-assign]
    assert env.manager.import_runner(path).id == existing.id
    assert len(env.config().runners) == 1


# --- scan_unregistered ---------------------------------------------------------------------


def test_scan_unregistered(env: Env) -> None:
    registered = make_runner_dir(env.root / "a")
    unregistered = make_runner_dir(env.root / "b")
    make_runner_dir(env.root / "c", agent_id=None)  # not configured
    (env.root / "d.txt").write_text("x", encoding="utf-8")
    env.seed(path=str(registered))
    assert env.manager.scan_unregistered() == [unregistered]


def test_scan_unregistered_missing_root(env: Env) -> None:
    assert env.manager.scan_unregistered() == []


# --- statuses --------------------------------------------------------------------------------


def status_env(env: Env) -> tuple[RunnerRecord, RunnerRecord, RunnerRecord, RunnerRecord]:
    a = env.seed(name="a", path=str(env.root / "a"), github_runner_id=1)
    b = env.seed(owner="OCTO", repo="Demo", name="b", path=str(env.root / "b"))
    gone = env.seed(name="gone", path=str(env.root / "gone"), github_runner_id=77)
    other = env.seed(repo="other", name="c", path=str(env.root / "c"), github_runner_id=3)
    env.client.runners[_key(OWNER, REPO)] = [
        remote(1, "renamed-a", "online", busy=True),
        remote(9, "B", "offline"),
    ]
    env.client.runners[_key(OWNER, "other")] = [remote(3, "c", "online")]
    return a, b, gone, other


def by_name(rows: list[RunnerStatusRow]) -> dict[str, RunnerStatusRow]:
    return {row.record.name: row for row in rows}


def test_statuses_empty_config_needs_no_client(env: Env) -> None:
    assert env.manager.statuses() == []
    assert env.factory_calls == 0


def test_statuses_groups_requests_per_repo(env: Env) -> None:
    status_env(env)
    rows = env.manager.statuses()
    assert [r.record.name for r in rows] == ["a", "b", "gone", "c"]
    assert env.client.calls == [
        ("list_runners", OWNER, REPO),
        ("list_runners", OWNER, "other"),
    ]
    assert env.client.closed == 1
    named = by_name(rows)
    assert named["a"].remote_runner is not None and named["a"].remote_runner.id == 1
    assert named["a"].display.state is DisplayState.EXTERNAL  # online, not ours
    assert named["a"].display.detail == DETAIL_EXTERNAL_REMOTE
    assert named["b"].remote_runner is not None and named["b"].remote_runner.id == 9
    assert named["b"].display.state is DisplayState.STOPPED
    assert named["gone"].remote.status is RemoteStatus.MISSING
    assert named["gone"].display.state is DisplayState.MISSING
    assert named["c"].remote.fetched_at == NOW
    assert all(row.remote_error is None for row in rows)


def test_statuses_isolates_a_failing_repo(env: Env) -> None:
    status_env(env)
    env.client.list_failures[_key(OWNER, REPO)] = NetworkError("连接超时")
    named = by_name(env.manager.statuses())
    for name in ("a", "b", "gone"):
        assert named[name].remote.status is RemoteStatus.UNKNOWN
        assert named[name].remote_error == "连接超时"
        assert named[name].display.state is DisplayState.STOPPED
        assert named[name].display.detail == DETAIL_REMOTE_UNKNOWN
        assert named[name].remote_runner is None
    assert named["c"].remote.status is RemoteStatus.ONLINE
    assert named["c"].remote_error is None


def test_statuses_without_client(env: Env) -> None:
    status_env(env)
    env.factory_error = AuthError("请先登录")
    rows = env.manager.statuses()
    assert env.factory_calls == 1
    assert all(r.remote.status is RemoteStatus.UNKNOWN for r in rows)
    assert all(r.remote_error == "请先登录" for r in rows)


def test_statuses_uses_owned_processes(env: Env) -> None:
    a, b, gone, other = status_env(env)
    processes = {
        a.id: FakeRunnerProcess(LocalState.BUSY, True, current_job="build"),
        other.id: FakeRunnerProcess(
            LocalState.STOPPED,
            False,
            last_stop_forced=True,
            stopped_at=NOW - timedelta(seconds=30),
        ),
        b.id: FakeRunnerProcess(LocalState.STARTING, True, state_since=NOW - timedelta(seconds=90)),
    }
    named = by_name(env.manager.statuses(processes))  # type: ignore[arg-type]
    assert named["a"].display.state is DisplayState.BUSY
    assert named["a"].display.label == "运行中：build"
    assert named["a"].local.running and named["a"].local.current_job == "build"
    assert named["c"].display.state is DisplayState.SESSION_LINGERING
    assert named["b"].display.state is DisplayState.STALLED  # offline for 90 s while running


def test_statuses_detects_external_processes(env: Env) -> None:
    a, b, gone, other = status_env(env)
    env.running[b.path] = [SimpleNamespace(pid=1)]
    env.running[other.path] = [SimpleNamespace(pid=2)]
    env.running[a.path] = psutil.AccessDenied(pid=3)  # type: ignore[assignment]
    processes = {other.id: FakeRunnerProcess(LocalState.STOPPED, False)}
    named = by_name(env.manager.statuses(processes))  # type: ignore[arg-type]
    assert named["b"].local.external
    assert named["b"].display.state is DisplayState.EXTERNAL
    assert named["b"].display.detail == DETAIL_EXTERNAL_LOCAL
    assert named["c"].local.external  # our process object exists but is not running
    assert not named["a"].local.external  # scan failure -> not external


def test_statuses_running_process_is_never_external(env: Env) -> None:
    a, *_ = status_env(env)
    env.running[a.path] = [SimpleNamespace(pid=1)]  # its own Listener
    processes = {a.id: FakeRunnerProcess(LocalState.IDLE, True)}
    row = by_name(env.manager.statuses(processes))["a"]  # type: ignore[arg-type]
    assert not row.local.external
    assert row.display.state is DisplayState.BUSY  # remote says busy


# --- process_for / misc ---------------------------------------------------------------------


class _Proc:
    def __init__(self, data: bytes) -> None:
        self.pid = 4321
        self.stdout = io.BytesIO(data)
        self.returncode: int | None = 0

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int | None:
        return 0

    def kill(self) -> None:
        pass


@pytest.mark.parametrize(
    ("proxy", "expected"),
    [
        (None, None),
        ("  ", None),
        ("127.0.0.1:7890", "http://127.0.0.1:7890"),
        ("http://proxy:8080", "http://proxy:8080"),
    ],
)
def test_process_for(env: Env, proxy: str | None, expected: str | None) -> None:
    set_proxy(env, proxy)
    record = env.seed(path=str(make_runner_dir(env.root / "p")))
    popen_calls: list[tuple[list[str], dict[str, Any]]] = []

    def popen(args: list[str], **kwargs: Any) -> _Proc:
        popen_calls.append((args, kwargs))
        return _Proc(b"\xe2\x88\x9a Connected to GitHub\r\n2026-10-22Z: Listening for Jobs\r\n")

    manager = RunnerManager(env.store, env.factory, cache=env.cache, popen=popen)  # type: ignore[arg-type]
    lines: list[str] = []
    proc = manager.process_for(record, on_line=lines.append, max_lines=10)
    assert proc.runner_dir == Path(record.path)
    assert not proc.is_running()
    proc.start()
    assert proc.wait(5) == 0
    args, kwargs = popen_calls[0]
    assert is_cmd_exe(args[0])
    assert args[1:] == ["/c", "run.cmd"]
    assert kwargs["cwd"] == record.path
    # process.py builds the full child environment from the overrides process_for passes
    child_env = kwargs["env"] or {}
    assert proxy_vars(child_env) == ({} if expected is None else both_proxies(expected))
    assert lines == ["√ Connected to GitHub", "2026-10-22Z: Listening for Jobs"]


def test_manager_defaults(isolated_home: Path) -> None:
    def no_login() -> Any:
        raise AuthError("请先登录")

    store = ConfigStore()
    manager = RunnerManager(store, no_login)
    assert manager.hostname and "." not in manager.hostname
    assert manager.load_config() == AppConfig()
    assert manager.statuses() == []

    config = AppConfig()
    config.runners.append(
        RunnerRecord(owner=OWNER, repo=REPO, name="a", path=str(isolated_home / "missing"))
    )
    store.save(config)
    (row,) = manager.statuses()
    assert row.remote_error == "请先登录"
    assert row.local.state_since.tzinfo is not None
    assert row.display.state is DisplayState.STOPPED


def test_rollback_error_message() -> None:
    original = InstallError("runner 注册失败")
    err = RollbackError(original, ["目录 X 未能删除", "GitHub 上可能残留 runner"])
    assert str(err) == (
        "runner 注册失败\n回滚未完成，需要手动处理：\n"
        "  - 目录 X 未能删除\n  - GitHub 上可能残留 runner"
    )
    assert err.original is original
    assert err.problems == ("目录 X 未能删除", "GitHub 上可能残留 runner")


def test_occupied_treats_unreadable_dirs_as_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "x"
    target.mkdir()

    def boom(self: Path) -> Iterator[Path]:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", boom)
    assert manager_mod._occupied(target)
    assert not manager_mod._occupied(tmp_path / "missing")
