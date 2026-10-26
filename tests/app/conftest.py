"""Fixtures and fakes for the app layer tests (docs/m2-design.md §2 / §4).

`isolated_home` / `MemoryKeyring` are copies of the core fixtures (tests must not import
tests.core). The fakes are duck-typed stand-ins for RunnerManager, RunnerProcess and
GitHubClient (plus the controller's autostart / diag operations, docs/m3-design.md §4): they
record calls, can be told to raise, and FakeProcess can deliver the callbacks it was given from
a non-main thread, as the real RunnerProcess does. FakeClock is the controller's UTC clock.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError
from PyQt5.QtCore import QThreadPool, QUrl

from runnerdeck.app.controller import RunnerController
from runnerdeck.core.manager import (
    AddPlan,
    RemoteRemoval,
    RemoveResult,
    RunnerStatusRow,
    Step,
)
from runnerdeck.core.models import (
    AppConfig,
    AutostartState,
    DiagLog,
    Discovery,
    GitHubUser,
    LocalState,
    ProxyApplyResult,
    RemoteStatus,
    Repo,
    RunnerMode,
    RunnerRecord,
)
from runnerdeck.core.process import ExternalStop, Launch, StopMethod, StopResult
from runnerdeck.core.status import LocalSnapshot, RemoteSnapshot, merge_status
from runnerdeck.core.store import ConfigStore

OWNER, REPO = "octo", "demo"
FAKE_PAT = "github_pat_" + "0FAKE0" * 10  # right shape, obviously not real
OTHER_PAT = "ghp_" + "FAKE0000" * 5
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
WAIT_MS = 5000
AUTOSTART_COMMAND = '"C:\\RunnerDeck\\RunnerDeck.exe" --autostart'


# --- core fixture copies ----------------------------------------------------------------------


class MemoryKeyring(KeyringBackend):
    """In-memory keyring: never touches the real credential store."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.passwords: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.passwords[(service, username)]
        except KeyError:
            raise PasswordDeleteError(f"{username}@{service} not found") from None


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every RunnerDeck app dir (config, cache, logs) to tmp_path."""
    monkeypatch.setenv("RUNNERDECK_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def memory_keyring() -> Iterator[MemoryKeyring]:
    """Install a fresh MemoryKeyring as keyring's backend; restore the previous one after."""
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


# --- Qt ---------------------------------------------------------------------------------------


@pytest.fixture
def pool(qapp: Any) -> Iterator[QThreadPool]:
    """A private thread pool per test, drained on teardown."""
    pool = QThreadPool()
    pool.setMaxThreadCount(4)
    try:
        yield pool
    finally:
        pool.waitForDone(WAIT_MS)


# --- fakes ------------------------------------------------------------------------------------


def user_model(login: str = "octo", kind: str = "fine-grained") -> GitHubUser:
    return GitHubUser(login=login, id=1, token_kind=kind)  # type: ignore[arg-type]


def repo_model(name: str, *, private: bool = True) -> Repo:
    return Repo(
        name=name,
        full_name=f"{OWNER}/{name}",
        owner=OWNER,
        private=private,
        html_url=f"https://github.com/{OWNER}/{name}",
    )


def record_model(name: str = "box-demo", repo: str = REPO, path: str | None = None) -> RunnerRecord:
    return RunnerRecord(
        owner=OWNER, repo=repo, name=name, path=path or f"/runners/{repo}", github_runner_id=42
    )


def plan_model(repo: str = REPO, **kw: Any) -> AddPlan:
    defaults: dict[str, Any] = {
        "owner": OWNER,
        "repo": repo,
        "name": f"box-{repo}",
        "labels": ("gpu",),
        "target_dir": Path(f"/runners/{repo}"),
        "private": True,
    }
    defaults.update(kw)
    return AddPlan(**defaults)


class FakeClient:
    """GitHubClient stand-in: records the PAT/proxy it was built with, raises on demand."""

    def __init__(self, pat: str, proxy: str | None, *, registry: FakeClientFactory) -> None:
        self.pat = pat
        self.proxy = proxy
        self.calls: list[str] = []
        self.closed = False
        self._registry = registry

    def _call(self, name: str) -> None:
        self.calls.append(name)
        error = self._registry.errors.get(name)
        if error is not None:
            raise error

    def get_user(self) -> GitHubUser:
        self._call("get_user")
        return self._registry.user

    def list_repos(self) -> list[Repo]:
        self._call("list_repos")
        return list(self._registry.repos)

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeClientFactory:
    """(pat, proxy) -> FakeClient; what the controller's `client_factory` keyword takes."""

    user: GitHubUser = field(default_factory=user_model)
    repos: list[Repo] = field(default_factory=lambda: [repo_model("demo"), repo_model("other")])
    errors: dict[str, Exception] = field(default_factory=dict)
    clients: list[FakeClient] = field(default_factory=list)
    construct_error: Exception | None = None

    def __call__(self, pat: str, proxy: str | None) -> FakeClient:
        if self.construct_error is not None:
            raise self.construct_error
        client = FakeClient(pat, proxy, registry=self)
        self.clients.append(client)
        return client


def _deliver(fn: Any, *args: Any) -> None:
    """Invoke a RunnerProcess-style callback from a non-main thread and wait for it."""
    thread = threading.Thread(target=fn, args=args, name="fake-runner-output", daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "callback did not return"


class FakeProcess:
    """RunnerProcess stand-in.

    start()/stop() run on whatever thread calls them (the pool in the controller), record the
    thread, and publish STARTING / STOPPED through the on_state callback the way the real
    process does (on the calling thread). `block_start` / `block_stop` hold the call until the
    test sets the event, to keep a runner busy. drive_state / push_line / finish_job invoke the
    callbacks from a fresh non-main thread.
    """

    def __init__(
        self,
        record: RunnerRecord,
        *,
        on_line: Any = None,
        on_state: Any = None,
        on_job_finished: Any = None,
        launch: Launch = Launch.CONSOLE,
    ) -> None:
        self.record = record
        self.runner_dir = Path(record.path)
        self.launch = launch
        self._on_line = on_line
        self._on_state = on_state
        self._on_job_finished = on_job_finished
        self.calls: list[str] = []
        self.threads: list[threading.Thread] = []
        self.lines: list[str] = []
        self._running = False
        self._state = LocalState.STOPPED
        self._job: str | None = None
        self.start_error: Exception | None = None
        self.stop_error: Exception | None = None
        self.block_start: threading.Event | None = None
        self.block_stop: threading.Event | None = None
        self.stop_hook: Any = None  # called inside stop() before it completes
        self.stop_result = StopResult(True, StopMethod.CTRL_BREAK, 0, [])
        self.stop_timeouts: list[float | None] = []
        self.state_since = NOW
        self.last_stop_forced = False
        self.stopped_at: datetime | None = None
        self.last_error: str | None = None
        self.signal_error: Exception | None = None

    # RunnerProcess API ------------------------------------------------------------------------

    @property
    def state(self) -> LocalState:
        return self._state

    @property
    def current_job(self) -> str | None:
        return self._job

    def is_running(self) -> bool:
        return self._running

    def output(self) -> list[str]:
        return list(self.lines)

    def start(self) -> None:
        self.calls.append("start")
        self.threads.append(threading.current_thread())
        if self.block_start is not None:
            assert self.block_start.wait(5)
        if self.start_error is not None:
            raise self.start_error
        self._running = True
        self._publish(LocalState.STARTING, None)

    def stop(self, timeout: float | None = None) -> StopResult:
        self.calls.append("stop")
        self.threads.append(threading.current_thread())
        self.stop_timeouts.append(timeout)
        if self.stop_hook is not None:
            self.stop_hook(self)
        if self.block_stop is not None:
            assert self.block_stop.wait(5)
        if self.stop_error is not None:
            raise self.stop_error
        if self._running:
            self._publish(LocalState.STOPPING, self._job)
            self._running = False
            self.stopped_at = NOW
            self._publish(LocalState.STOPPED, None)
        return self.stop_result

    def signal_stop(self) -> bool:
        """Records the call; True while running (the runner exits later, on its own)."""
        self.calls.append("signal_stop")
        self.threads.append(threading.current_thread())
        if self.signal_error is not None:
            raise self.signal_error
        return self._running

    # test drivers ----------------------------------------------------------------------------

    def _publish(self, state: LocalState, job: str | None) -> None:
        self._state, self._job = state, job
        if self._on_state is not None:
            self._on_state(state, job)

    def drive_state(self, state: LocalState, job: str | None = None) -> None:
        """Set the state and deliver on_state from a non-main thread."""
        self._state, self._job = state, job
        if self._on_state is not None:
            _deliver(self._on_state, state, job)

    def push_line(self, line: str) -> None:
        self.lines.append(line)
        if self._on_line is not None:
            _deliver(self._on_line, line)

    def finish_job(self, job: str, result: str) -> None:
        if self._on_job_finished is not None:
            _deliver(self._on_job_finished, job, result)


def status_row(
    record: RunnerRecord,
    state: LocalState = LocalState.STOPPED,
    *,
    remote_error: str | None = None,
    remote: RemoteStatus | None = None,
    busy: bool = False,
    fetched_at: datetime | None = None,
    external: bool = False,
    stopped_at: datetime | None = None,
    forced: bool = False,
) -> RunnerStatusRow:
    """A row as manager.statuses returns it. remote: default OFFLINE (UNKNOWN with an error);
    pass RemoteStatus.UNKNOWN without an error for a `remote=False` row."""
    local = LocalSnapshot(
        state=state,
        running=state is not LocalState.STOPPED,
        external=external,
        state_since=NOW,
        last_stop_forced=forced,
        stopped_at=stopped_at,
    )
    if remote is None:
        remote = RemoteStatus.UNKNOWN if remote_error else RemoteStatus.OFFLINE
    snapshot = RemoteSnapshot(remote, busy=busy, fetched_at=fetched_at)
    return RunnerStatusRow(
        record=record,
        display=merge_status(local, snapshot, now=NOW),
        local=local,
        remote=snapshot,
        remote_error=remote_error,
    )


class FakeManager:
    """RunnerManager stand-in (duck-typed): records calls, raises what `errors` says."""

    def __init__(self, store: ConfigStore, client_factory: Any) -> None:
        self.store = store
        self.client_factory = client_factory
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.env_texts: dict[str, str] = {}  # read_env / write_env
        self.job_snapshots: list[Any] = []  # job_snapshot answers, in order (last repeats)
        self.threads: list[threading.Thread] = []
        self.errors: dict[str, Exception] = {}
        self.service_states: dict[str, LocalSnapshot] = {}  # service_locals() answers
        self.processes: dict[str, FakeProcess] = {}
        self.rows: list[RunnerStatusRow] = []
        self.statuses_seen: list[Mapping[str, Any]] = []
        self.block_statuses: threading.Event | None = None  # holds statuses() until set
        self.next_record: RunnerRecord | None = None
        self.discovery = Discovery(root=Path("/runners"))
        self.external_result = ExternalStop(killed=[101, 102], survivors=[])
        self.stray_result = ExternalStop(killed=[201], survivors=[])
        self.services: set[Path] = set()
        self.stop_marks: list[tuple[Path, bool]] = []  # (runner_dir, forced) per record_stop

    def _call(self, method: str, **kw: Any) -> None:
        self.calls.append((method, kw))
        self.threads.append(threading.current_thread())
        error = self.errors.get(method)
        if error is not None:
            raise error

    def load_config(self) -> AppConfig:
        return self.store.load()

    def plan_add(
        self,
        owner: str,
        repo: str,
        *,
        name: str | None = None,
        labels: Sequence[str] | None = None,
        root: Any = None,
        version: str | None = None,
        runner_env: Mapping[str, str] | None = None,
        mode: RunnerMode = RunnerMode.PROCESS,
    ) -> AddPlan:
        extra = {"mode": mode} if mode is not RunnerMode.PROCESS else {}
        self._call(
            "plan_add",
            owner=owner,
            repo=repo,
            name=name,
            labels=labels,
            runner_env=runner_env,
            **extra,
        )
        return plan_model(
            repo,
            name=name or f"box-{repo}",
            labels=tuple(labels or ()),
            runner_env=tuple((runner_env or {}).items()),
        )

    def add_runner(
        self, plan: AddPlan, *, auto_start: bool = False, progress: Any = None
    ) -> RunnerRecord:
        if progress is not None:
            progress(Step.RELEASE, "查询 runner 版本")
            progress(Step.DOWNLOAD, "下载 50%")
        self._call("add_runner", plan=plan, auto_start=auto_start)
        record = self.next_record or RunnerRecord(
            owner=plan.owner,
            repo=plan.repo,
            name=plan.name,
            labels=list(plan.labels),
            path=str(plan.target_dir),
            github_runner_id=7,
            auto_start=auto_start,
        )
        config = self.store.load()
        config.upsert(record)
        self.store.save(config)
        if progress is not None:
            progress(Step.DONE, f"已添加 runner {record.name}")
        return record

    def remove_runner(
        self,
        key: str,
        *,
        keep_work: bool = False,
        local_only: bool = False,
        progress: Any = None,
    ) -> RemoveResult:
        if progress is not None:
            progress(Step.CHECK, "检查")
        self._call("remove_runner", key=key, keep_work=keep_work, local_only=local_only)
        config = self.store.load()
        record = config.remove(key)
        assert record is not None, key
        self.store.save(config)
        if progress is not None:
            progress(Step.DONE, f"已移除 runner {record.name}")
        return RemoveResult(
            record=record, remote=RemoteRemoval.UNCONFIGURED, runner_id=None, dir_removed=True
        )

    def statuses(
        self, processes: Mapping[str, Any] | None = None, *, remote: bool = True
    ) -> list[RunnerStatusRow]:
        self.statuses_seen.append(dict(processes or {}))
        if self.block_statuses is not None:
            assert self.block_statuses.wait(5)
        self._call("statuses", remote=remote)
        return list(self.rows)

    def import_runner(self, path: Any, *, auto_start: bool = False) -> RunnerRecord:
        self._call("import_runner", path=path, auto_start=auto_start)
        record = self.next_record or RunnerRecord(
            owner=OWNER,
            repo="imported",
            name="box-imported",
            path=str(path),
            github_runner_id=9,
            auto_start=auto_start,
        )
        config = self.store.load()
        config.upsert(record)
        self.store.save(config)
        return record

    def set_auto_start(self, record_id: str, enabled: bool) -> RunnerRecord:
        self._call("set_auto_start", record_id=record_id, enabled=enabled)
        config = self.store.load()
        record = config.get(record_id)
        assert record is not None, record_id
        updated = record.model_copy(update={"auto_start": enabled})
        config.upsert(updated)
        self.store.save(config)
        return updated

    # service mode (docs/m4-design.md §2)

    def service_locals(self, record_ids: Sequence[str]) -> dict[str, LocalSnapshot]:
        self._call("service_locals", record_ids=list(record_ids))
        return {rid: self.service_states[rid] for rid in record_ids if rid in self.service_states}

    def start_service(self, record_id: str, *, elevate: bool = True) -> Any:
        self._call("start_service", record_id=record_id, elevate=elevate)
        return SimpleNamespace(state="running")

    def stop_service(self, record_id: str, *, elevate: bool = True) -> Any:
        self._call("stop_service", record_id=record_id, elevate=elevate)
        return SimpleNamespace(state="stopped")

    def convert_to_service(self, record_id: str) -> RunnerRecord:
        self._call("convert_to_service", record_id=record_id)
        config = self.store.load()
        record = config.get(record_id)
        assert record is not None, record_id
        updated = record.model_copy(
            update={"mode": RunnerMode.SERVICE, "service_name": "actions.runner.o-r.x"}
        )
        config.upsert(updated)
        self.store.save(config)
        return updated

    def grant_service_control(self, record_id: str) -> None:
        self._call("grant_service_control", record_id=record_id)

    def job_snapshot(self, record_id: str, job_id: int | None = None) -> Any:
        from runnerdeck.core.models import JobSnapshot

        self._call("job_snapshot", record_id=record_id, job_id=job_id)
        if not self.job_snapshots:
            return JobSnapshot()
        return self.job_snapshots.pop(0) if len(self.job_snapshots) > 1 else self.job_snapshots[0]

    def repo_insights(self, record_id: str) -> Any:
        from runnerdeck.core.models import RepoInsights

        self._call("repo_insights", record_id=record_id)
        return RepoInsights(fetched_at=NOW)

    def apply_proxy(self, old: str | None, new: str | None) -> ProxyApplyResult:
        self._call("apply_proxy", old=old, new=new)
        return ProxyApplyResult(updated=("r1",))

    def read_env(self, key: str) -> str:
        self._call("read_env", key=key)
        return self.env_texts.get(key, "")

    def write_env(self, key: str, text: str) -> None:
        self._call("write_env", key=key, text=text)
        self.env_texts[key] = text

    def apply_pip_index(self, old: str | None, new: str | None) -> ProxyApplyResult:
        self._call("apply_pip_index", old=old, new=new)
        return ProxyApplyResult(updated=("r1",))

    def harden_runners_root(self) -> Path:
        self._call("harden_runners_root")
        return Path(self.store.load().settings.runners_root)

    def forget_runner(self, record_id: str) -> RunnerRecord:
        self._call("forget_runner", record_id=record_id)
        config = self.store.load()
        record = config.remove(record_id)
        assert record is not None, record_id
        self.store.save(config)
        return record

    def discover(self) -> Discovery:
        self._call("discover")
        return self.discovery

    def is_service(self, runner_dir: Path) -> bool:
        return Path(runner_dir) in self.services

    def kill_external(self, record_id: str) -> ExternalStop:
        self._call("kill_external", record_id=record_id)
        return self.external_result

    def kill_stray(self, path: Any) -> ExternalStop:
        self._call("kill_stray", path=path)
        return self.stray_result

    def record_stop(self, runner_dir: Path, *, forced: bool, at: datetime | None = None) -> None:
        """Never raises (like the real one); the call is still recorded with its thread."""
        self.calls.append(("record_stop", {"runner_dir": Path(runner_dir), "forced": forced}))
        self.threads.append(threading.current_thread())
        self.stop_marks.append((Path(runner_dir), forced))

    def process_for(
        self,
        record: RunnerRecord,
        *,
        on_line: Any = None,
        on_state: Any = None,
        on_job_finished: Any = None,
        max_lines: int = 2000,
        launch: Launch = Launch.CONSOLE,
    ) -> FakeProcess:
        self._call("process_for", record=record, launch=launch)
        proc = FakeProcess(
            record,
            on_line=on_line,
            on_state=on_state,
            on_job_finished=on_job_finished,
            launch=launch,
        )
        self.processes[record.id] = proc
        return proc


class FakeAutostart:
    """AutostartOps stand-in: an in-memory Run entry, never the real registry."""

    def __init__(self, *, supported: bool = True) -> None:
        self.supported = supported
        self.registered = False
        self.error: Exception | None = None
        self.calls: list[tuple[Any, ...]] = []
        self.threads: list[threading.Thread] = []

    def _call(self, *call: Any) -> None:
        self.calls.append(call)
        self.threads.append(threading.current_thread())
        if self.error is not None:
            raise self.error

    def _state(self) -> AutostartState:
        return AutostartState(
            supported=self.supported,
            registered=self.registered,
            command=AUTOSTART_COMMAND if self.registered else None,
            expected=AUTOSTART_COMMAND if self.supported else None,
        )

    def state(self) -> AutostartState:
        self._call("state")
        return self._state()

    def enable(self) -> AutostartState:
        self._call("enable")
        self.registered = True
        return self._state()

    def disable(self) -> AutostartState:
        self._call("disable")
        self.registered = False
        return self._state()

    def reconcile(self, wanted: bool) -> AutostartState:
        self._call("reconcile", wanted)
        if self.supported:
            self.registered = wanted
        return self._state()


class FakeDiag:
    """DiagOps stand-in: `logs` is what list_logs returns, `texts[path]` what read_tail does."""

    def __init__(self) -> None:
        self.logs: list[DiagLog] = []
        self.texts: dict[Path, str] = {}
        self.error: Exception | None = None
        self.calls: list[tuple[str, Path]] = []
        self.threads: list[threading.Thread] = []

    def _call(self, name: str, path: Path) -> None:
        self.calls.append((name, path))
        self.threads.append(threading.current_thread())
        if self.error is not None:
            raise self.error

    def list_logs(self, runner_dir: Path) -> list[DiagLog]:
        self._call("list_logs", runner_dir)
        return list(self.logs)

    def read_tail(self, path: Path) -> str:
        self._call("read_tail", path)
        return self.texts.get(path, "")


class FakeClock:
    """The controller's UTC clock: fixed at NOW until a test advances it."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


# --- controller harness --------------------------------------------------------------------


@dataclass
class Harness:
    controller: RunnerController
    manager: FakeManager
    clients: FakeClientFactory
    store: ConfigStore
    keyring: MemoryKeyring
    environ: dict[str, str]
    opened: list[QUrl]
    autostart: FakeAutostart
    diag: FakeDiag
    clock: FakeClock

    def process(self, record_id: str) -> FakeProcess:
        return self.manager.processes[record_id]


@pytest.fixture
def make_controller(
    isolated_home: Path, memory_keyring: MemoryKeyring, pool: QThreadPool
) -> Iterator[Any]:
    """Factory: make_controller(records=[...], account=..., environ={...}) -> Harness.

    The controller's clock is a FakeClock fixed at NOW (the fakes' timestamps), so merged
    displays only change when a test advances it.
    """
    harnesses: list[Harness] = []

    def factory(
        *,
        records: Sequence[RunnerRecord] = (),
        account: str | None = None,
        environ: dict[str, str] | None = None,
        launch: Launch = Launch.HELPER,
        open_ok: bool = True,
        autostart_supported: bool = True,
        settings: Any = None,
    ) -> Harness:
        store = ConfigStore()
        config = AppConfig(account=account, runners=list(records))
        if settings is not None:
            config.settings = settings
        store.save(config)
        clients = FakeClientFactory()
        env = {} if environ is None else environ
        opened: list[QUrl] = []
        managers: list[FakeManager] = []

        def manager_factory(st: ConfigStore, cf: Any) -> FakeManager:
            manager = FakeManager(st, cf)
            managers.append(manager)
            return manager

        def open_url(url: QUrl) -> bool:
            opened.append(url)
            return open_ok

        autostart = FakeAutostart(supported=autostart_supported)
        diag = FakeDiag()
        clock = FakeClock()
        controller = RunnerController(
            store=store,
            manager_factory=manager_factory,
            environ=env,
            pool=pool,
            launch=launch,
            client_factory=clients,
            open_url=open_url,
            autostart=autostart,
            diag=diag,
            clock=clock,
        )
        harness = Harness(
            controller,
            managers[0],
            clients,
            store,
            memory_keyring,
            env,
            opened,
            autostart,
            diag,
            clock,
        )
        harnesses.append(harness)
        return harness

    yield factory
    for harness in harnesses:
        if harness.manager.block_statuses is not None:
            harness.manager.block_statuses.set()
        for proc in harness.manager.processes.values():
            for event in (proc.block_start, proc.block_stop):
                if event is not None:
                    event.set()
