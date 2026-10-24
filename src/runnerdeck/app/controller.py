"""RunnerController: the UI's only entry point to the core layer (docs/m2-design.md §4,
docs/m3-design.md §4.1).

Every blocking RunnerManager / GitHubClient / RunnerProcess call is submitted to a QThreadPool
through app.workers; results come back as signals emitted on the main thread. RunnerProcess
callbacks arrive on the process's reader thread and are forwarded through an internal _Bridge
whose signals are queued to the main thread.

Threading model: all controller state (`_processes`, `_busy`, cached config, status caches and
displays) is owned by the main thread; workers only get the objects they need as arguments. Per
runner only one of start / stop / remove / forget / kill runs at a time (`busy_changed`);
add_runner is serialized by the manager's own lock.

Live merge (M3): the controller caches the last `row.local` (fields that need a process scan or
a disk read, e.g. `external` and stop marks) and `row.remote` of every record and recomputes the
dashboard status on the main thread from those caches plus the live fields of our own
RunnerProcess (status.merge_status is a pure function). A 5 s timer re-merges everything,
because some states only change with time; `display_changed` is emitted only on change.

Errors: `failed(op, message, exc)` is always emitted from the main thread with a redacted,
Chinese `message`; the controller never shows dialogs. Quiet refreshes (the poller) only log.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from PyQt5.QtCore import QObject, Qt, QThreadPool, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices

from runnerdeck.app.workers import Task, submit
from runnerdeck.core import account, secrets
from runnerdeck.core.errors import InvalidInputError, RunnerDeckError
from runnerdeck.core.github_client import GitHubClient
from runnerdeck.core.manager import (
    POWERSHELL_POLICY_ENV,
    AddPlan,
    ClientFactory,
    RemoveResult,
    RunnerManager,
    RunnerStatusRow,
)
from runnerdeck.core.models import (
    AppConfig,
    AppSettings,
    AutostartState,
    DiagLog,
    GitHubUser,
    LocalState,
    RemoteStatus,
    Repo,
    RunnerRecord,
)
from runnerdeck.core.process import ExternalStop, Launch, RunnerProcess, StopResult
from runnerdeck.core.redact import redact
from runnerdeck.core.status import DisplayStatus, LocalSnapshot, RemoteSnapshot, merge_status
from runnerdeck.core.store import ConfigStore

log = logging.getLogger(__name__)

LOGIN_HINT = "请先登录"
# Progress step id emitted by remove() before manager.remove_runner's own steps when the
# runner had to be stopped first (not a manager.Step).
STEP_STOP = "stop"
REMERGE_INTERVAL_MS = 5000  # re-merge every display: some states only change with time
# Same as core.diag.DIAG_DIR (core.diag is only imported inside CoreDiag's calls).
DIAG_DIR = "_diag"
AUTOSTART_SKIPPED = "runner 目录下已有其他进程在运行，未自动启动："
AUTOSTART_UNSUPPORTED = "开机自启只支持 Windows"

ManagerFactory = Callable[[ConfigStore, ClientFactory], RunnerManager]
PatClientFactory = Callable[[str, str | None], GitHubClient]  # (pat, proxy) -> client
OpenUrlFn = Callable[[QUrl], bool]
Clock = Callable[[], datetime]  # timezone-aware UTC "now"


class AutostartOps(Protocol):
    """What the controller needs from core.autostart (HKCU Run entry). Calls may block."""

    @property
    def supported(self) -> bool: ...

    def state(self) -> AutostartState: ...

    def enable(self) -> AutostartState: ...

    def disable(self) -> AutostartState: ...

    def reconcile(self, wanted: bool) -> AutostartState: ...


class DiagOps(Protocol):
    """What the controller needs from core.diag (a runner's `_diag` logs). Calls may block."""

    def list_logs(self, runner_dir: Path) -> list[DiagLog]: ...

    def read_tail(self, path: Path) -> str: ...


class CoreAutostart:
    """AutostartOps over core.autostart and the real registry (imported on first use)."""

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    def state(self) -> AutostartState:
        from runnerdeck.core import autostart

        return autostart.state()

    def enable(self) -> AutostartState:
        from runnerdeck.core import autostart

        return autostart.enable()

    def disable(self) -> AutostartState:
        from runnerdeck.core import autostart

        return autostart.disable()

    def reconcile(self, wanted: bool) -> AutostartState:
        from runnerdeck.core import autostart

        return autostart.reconcile(wanted)


class CoreDiag:
    """DiagOps over core.diag (imported on first use)."""

    def list_logs(self, runner_dir: Path) -> list[DiagLog]:
        from runnerdeck.core import diag

        return diag.list_diag_logs(runner_dir)

    def read_tail(self, path: Path) -> str:
        from runnerdeck.core import diag

        return diag.read_tail(path)


def _default_client(pat: str, proxy: str | None) -> GitHubClient:
    return GitHubClient(pat, proxy=proxy)


def _default_manager(store: ConfigStore, client_factory: ClientFactory) -> RunnerManager:
    return RunnerManager(store, client_factory)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _is_inside(path: Path, directory: Path) -> bool:
    """`path` lies strictly below `directory` once symlinks and `..` are resolved."""
    try:
        target = os.path.normcase(os.path.realpath(path))
        base = os.path.normcase(os.path.realpath(directory))
        return target != base and os.path.commonpath([target, base]) == base
    except (OSError, ValueError):  # ValueError: different drives on Windows
        return False


class _Bridge(QObject):
    """Carries RunnerProcess callbacks (reader thread) to the main thread."""

    line = pyqtSignal(str, str)  # record id, output line
    state = pyqtSignal(str, object, object)  # record id, LocalState, current job
    job = pyqtSignal(str, str, str)  # record id, job, result


class _ProcessHooks:
    """The three callbacks given to one RunnerProcess.

    They run on the process's threads: quick, never call start()/stop(). Once the controller
    replaced the process (restart) the old one's late notifications are dropped.
    """

    def __init__(self, controller: RunnerController, record_id: str) -> None:
        self._controller = controller
        self._bridge = controller._bridge
        self.record_id = record_id
        self.process: RunnerProcess | None = None

    def _current(self) -> bool:
        return self._controller._processes.get(self.record_id) is self.process

    def on_line(self, line: str) -> None:
        if self._current():
            self._bridge.line.emit(self.record_id, line)

    def on_state(self, state: LocalState, job: str | None) -> None:
        if self._current():
            self._bridge.state.emit(self.record_id, state, job)

    def on_job_finished(self, job: str, result: str) -> None:
        if self._current():
            self._bridge.job.emit(self.record_id, job, result)


class RunnerController(QObject):
    """Main-thread facade over RunnerManager, GitHubClient and RunnerProcess (§4)."""

    # --- signals (all emitted on the main thread) -------------------------------------------
    config_changed = pyqtSignal(object)  # AppConfig, after every write
    logged_in = pyqtSignal(object)  # GitHubUser (see the note on `logged_in` below)
    logged_out = pyqtSignal()
    account_loaded = pyqtSignal(object, str)  # GitHubUser, PAT source (account.SOURCE_*)
    repos_loaded = pyqtSignal(object)  # list[Repo]
    plan_ready = pyqtSignal(object)  # AddPlan
    add_progress = pyqtSignal(str, str)  # manager.Step id, text
    runner_added = pyqtSignal(object)  # RunnerRecord
    runner_state = pyqtSignal(str, object, object)  # record id, LocalState, current job
    runner_stopped = pyqtSignal(str, object)  # record id, StopResult
    remove_progress = pyqtSignal(str, str)  # STEP_STOP / manager.Step id, text
    runner_removed = pyqtSignal(str, object)  # record id, RemoveResult
    statuses_updated = pyqtSignal(object)  # list[RunnerStatusRow]
    refreshing_changed = pyqtSignal(bool)  # refresh_statuses in flight
    busy_changed = pyqtSignal(str, bool)  # record id, busy
    runner_line = pyqtSignal(str, str)  # record id, output line
    runner_job_finished = pyqtSignal(str, str, str)  # record id, job, result
    # (op, redacted Chinese message, exception or None). A two-argument slot receives
    # (op, message): PyQt drops trailing arguments a Python slot does not accept.
    failed = pyqtSignal(str, str, object)
    # M3 (docs/m3-design.md §4.1)
    display_changed = pyqtSignal(str, object)  # record id, DisplayStatus (only on change)
    discovery_updated = pyqtSignal(object)  # Discovery
    runner_imported = pyqtSignal(object)  # RunnerRecord, then config_changed
    runner_forgotten = pyqtSignal(str)  # record id, then config_changed
    external_stopped = pyqtSignal(str, object)  # record id or stray dir path, ExternalStop
    diag_logs_loaded = pyqtSignal(str, object)  # record id, list[DiagLog]
    diag_text_loaded = pyqtSignal(str, str, str)  # record id, path, text
    autostart_state_loaded = pyqtSignal(object)  # AutostartState

    def __init__(
        self,
        *,
        store: ConfigStore | None = None,
        manager_factory: ManagerFactory | None = None,
        environ: Mapping[str, str] = os.environ,
        pool: QThreadPool | None = None,
        launch: Launch = Launch.HELPER,
        client_factory: PatClientFactory | None = None,
        open_url: OpenUrlFn | None = None,
        autostart: AutostartOps | None = None,
        diag: DiagOps | None = None,
        clock: Clock | None = None,
        parent: QObject | None = None,
    ) -> None:
        """Raises StoreError when config.json exists but cannot be read (never resets it).

        client_factory: builds the GitHubClient from (pat, proxy); tests inject a fake.
        open_url: what open_dir() calls (QDesktopServices.openUrl); tests inject a recorder.
        autostart / diag: the Run-key and `_diag` operations (default: CoreAutostart /
        CoreDiag); tests inject fakes so the real registry is never touched.
        clock: UTC "now" for the live merge (default datetime.now(UTC)).
        """
        super().__init__(parent)
        self._store = store if store is not None else ConfigStore()
        self._environ = environ
        self._pool = pool if pool is not None else QThreadPool.globalInstance()
        self._launch = Launch(launch)
        self._new_client: PatClientFactory = client_factory or _default_client
        self._open_url: OpenUrlFn = open_url or QDesktopServices.openUrl
        self._autostart: AutostartOps = autostart if autostart is not None else CoreAutostart()
        self._diag: DiagOps = diag if diag is not None else CoreDiag()
        self._clock: Clock = clock or _utcnow
        self._manager = (manager_factory or _default_manager)(self._store, self._client_factory)

        self._processes: dict[str, RunnerProcess] = {}
        self._busy: set[str] = set()
        self._refreshing = False
        # Live-merge inputs and outputs per record id (see the module docstring).
        self._locals: dict[str, LocalSnapshot] = {}  # last row.local
        self._remotes: dict[str, RemoteSnapshot] = {}  # last row.remote of a remote refresh
        self._displays: dict[str, DisplayStatus] = {}  # last merged display (change detection)

        self._bridge = _Bridge(self)
        self._bridge.line.connect(self._on_bridge_line, Qt.QueuedConnection)
        self._bridge.state.connect(self._on_bridge_state, Qt.QueuedConnection)
        self._bridge.job.connect(self._on_bridge_job, Qt.QueuedConnection)

        self._remerge_timer = QTimer(self)
        self._remerge_timer.setInterval(REMERGE_INTERVAL_MS)
        self._remerge_timer.timeout.connect(self._on_remerge_timer)

        self._config: AppConfig = self._store.load()
        now = self._clock()
        self._displays = {r.id: self._live_display(r.id, now) for r in self._config.runners}
        self._sync_remerge_timer()

    # --- read-only (main thread, never blocks) ------------------------------------------------

    @property
    def config(self) -> AppConfig:
        """The most recently loaded config (refreshed after every write)."""
        return self._config

    @property
    def is_logged_in(self) -> bool:
        """config.account is set, or RUNNERDECK_PAT is in the environment."""
        return bool(self._config.account) or bool(self._environ.get(account.ENV_PAT, "").strip())

    @property
    def launch(self) -> Launch:
        return self._launch

    @property
    def manager(self) -> RunnerManager:
        return self._manager

    @property
    def refreshing(self) -> bool:
        return self._refreshing

    @property
    def records(self) -> list[RunnerRecord]:
        return list(self._config.runners)

    @property
    def autostart_supported(self) -> bool:
        """Launch-on-login can be configured here (Windows)."""
        return self._autostart.supported

    def local_state(self, record_id: str) -> tuple[LocalState, str | None]:
        """(state, current job) of our process; (STOPPED, None) without one."""
        proc = self._processes.get(record_id)
        if proc is None:
            return LocalState.STOPPED, None
        return proc.state, proc.current_job

    def is_running(self, record_id: str) -> bool:
        proc = self._processes.get(record_id)
        return proc is not None and proc.is_running()

    def is_busy(self, record_id: str) -> bool:
        return record_id in self._busy

    def output(self, record_id: str) -> list[str]:
        proc = self._processes.get(record_id)
        return proc.output() if proc is not None else []

    def running_ids(self) -> list[str]:
        return [rid for rid, proc in self._processes.items() if proc.is_running()]

    def display(self, record_id: str) -> DisplayStatus:
        """The merged status right now (live merge of the caches and our process)."""
        return self._live_display(record_id, self._clock())

    def all_displays(self) -> dict[str, DisplayStatus]:
        """display() of every record, keyed by record id, in config order."""
        now = self._clock()
        return {r.id: self._live_display(r.id, now) for r in self._config.runners}

    def remote_busy(self, record_id: str) -> bool:
        """The latest GitHub snapshot says the runner is running a job (a zombie job hint
        before a take-over or kill)."""
        remote = self._remotes.get(record_id)
        return remote is not None and remote.busy

    # --- account ------------------------------------------------------------------------------

    def login(self, pat: str) -> None:
        """Validate the PAT format, ask GitHub who it is, store it, set config.account."""
        proxy = self._config.settings.proxy
        self._submit("login", self._do_login, pat, proxy, on_finished=self._login_done)

    def _do_login(self, raw: str, proxy: str | None) -> GitHubUser:  # worker
        pat = secrets.validate_pat_format(raw)  # never echoes the input; before any network
        with closing(self._new_client(pat, proxy)) as client:
            user = client.get_user()
        secrets.save_pat(user.login, pat)
        config = self._store.load()
        previous = config.account
        config.account = user.login
        self._store.save(config)
        if previous and previous.casefold() != user.login.casefold():
            try:
                secrets.delete_pat(previous)
            except RunnerDeckError as e:
                log.warning("could not delete the PAT of the previous account %s: %s", previous, e)
        log.info("logged in as %s (%s token)", user.login, user.token_kind)
        return user

    def _login_done(self, user: GitHubUser) -> None:
        self._reload_config("login")
        self.logged_in.emit(user)

    def logout(self) -> None:
        self._submit("logout", self._do_logout, on_finished=self._logout_done)

    def _do_logout(self) -> None:  # worker
        config = self._store.load()
        if config.account:
            secrets.delete_pat(config.account)
            log.info("logged out %s", config.account)
            config.account = None
            self._store.save(config)

    def _logout_done(self, _result: object) -> None:
        self._reload_config("logout")
        self.logged_out.emit()

    def load_account(self) -> None:
        """get_user() for the settings page: `account_loaded(user, source)`."""
        self._submit(
            "account",
            self._do_load_account,
            on_finished=lambda result: self.account_loaded.emit(*result),
        )

    def _do_load_account(self) -> tuple[GitHubUser, str]:  # worker
        config = self._config
        pat, source = account.resolve_pat(config.account, self._environ, login_hint=LOGIN_HINT)
        with closing(self._new_client(pat, config.settings.proxy)) as client:
            return client.get_user(), source

    def load_repos(self) -> None:
        self._submit("repos", self._do_load_repos, on_finished=self.repos_loaded)

    def _do_load_repos(self) -> list[Repo]:  # worker
        with closing(self._client_factory()) as client:
            return client.list_repos()

    # --- add ----------------------------------------------------------------------------------

    def plan_add(
        self,
        owner: str,
        repo: str,
        *,
        name: str | None = None,
        labels: Sequence[str] | None = None,
        ps_policy: bool = True,
    ) -> None:
        """manager.plan_add on a worker; `plan_ready(AddPlan)`.

        ps_policy: write PSExecutionPolicyPreference=RemoteSigned to the runner's `.env`
        (manager.POWERSHELL_POLICY_ENV, spike §10).
        """
        self._submit(
            "plan_add",
            self._manager.plan_add,
            owner,
            repo,
            name=name,
            labels=labels,
            runner_env=POWERSHELL_POLICY_ENV if ps_policy else None,
            on_finished=self.plan_ready,
        )

    def add_runner(
        self, plan: AddPlan, *, start_after: bool = False, auto_start: bool = False
    ) -> None:
        """manager.add_runner with `add_progress`; `config_changed` + `runner_added`.

        auto_start: the new record is started by start_auto_runners() (launch with RunnerDeck).
        On failure `failed("add", message, exc)`: an InstallError carries `.output`.
        """
        self._submit(
            "add",
            self._manager.add_runner,
            plan,
            auto_start=auto_start,
            progress=True,
            on_progress=self.add_progress,
            on_finished=lambda record: self._add_done(record, start_after),
        )

    def _add_done(self, record: RunnerRecord, start_after: bool) -> None:
        self._reload_config("add")
        self.runner_added.emit(record)
        if start_after:
            self.start(record.id)

    # --- import / forget / auto start -------------------------------------------------------------

    def import_runner(self, path: str, *, auto_start: bool = False) -> None:
        """manager.import_runner on a worker; `runner_imported(record)` then `config_changed`.

        A path that is already recorded answers with that record. failed("import").
        """
        self._submit(
            "import",
            self._manager.import_runner,
            path,
            auto_start=auto_start,
            on_finished=self._import_done,
        )

    def _import_done(self, record: RunnerRecord) -> None:
        loaded = self._load_config("import")
        self.runner_imported.emit(record)
        if loaded:
            self._announce_config()
        self.discover()  # the directory is no longer "unregistered"

    def forget(self, record_id: str) -> None:
        """Delete only the record (directory and GitHub registration stay); refused while our
        process runs it or another operation is in progress. `runner_forgotten` then
        `config_changed`; failed("forget")."""
        record = self._record_for("forget", record_id)
        if record is None:
            return
        if self.is_running(record_id):
            self._fail("forget", f"runner “{record.name}” 正在运行，请先停止再取消登记")
            return
        if not self._claim("forget", record):
            return
        self._submit(
            "forget",
            self._manager.forget_runner,
            record_id,
            on_finished=lambda _record: self._forget_done(record_id),
            on_failed=lambda _m, _e: self._release(record_id),
        )

    def _forget_done(self, record_id: str) -> None:
        self._processes.pop(record_id, None)
        self._drop_caches(record_id)
        self._release(record_id)
        loaded = self._load_config("forget")
        self.runner_forgotten.emit(record_id)
        if loaded:
            self._announce_config()
        self.discover()  # a directory under runners_root is unregistered again

    def set_auto_start(self, record_id: str, enabled: bool) -> None:
        """manager.set_auto_start on a worker; `config_changed`. failed("auto_start")."""
        if self._record_for("auto_start", record_id) is None:
            return
        self._submit(
            "auto_start",
            self._manager.set_auto_start,
            record_id,
            enabled,
            on_finished=lambda _record: self._reload_config("auto_start"),
        )

    def start_auto_runners(self) -> None:
        """Start every auto_start record that is not running (called once at launch).

        A local scan (statuses(remote=False)) on a worker first: a record whose directory
        already has a runner process we do not own is skipped and reported as
        failed("autostart", ...). The starts themselves happen on the main thread.
        """
        if not any(r.auto_start for r in self._config.runners):
            return
        self._submit(
            "autostart",
            self._manager.statuses,
            dict(self._processes),
            remote=False,
            on_finished=self._auto_start_scanned,
        )

    def _auto_start_scanned(self, rows: list[RunnerStatusRow]) -> None:
        self._absorb_rows(rows, remote=False)
        skipped: list[str] = []
        for row in rows:
            record = self._config.get(row.record.id)
            if record is None or not record.auto_start:
                continue
            if self.is_running(record.id) or self.is_busy(record.id):
                continue
            if row.local.external:
                skipped.append(record.name)
                continue
            log.info("auto-starting runner %s", record.name)
            self.start(record.id)
        if skipped:
            self._fail("autostart", AUTOSTART_SKIPPED + "、".join(skipped))

    # --- start / stop ---------------------------------------------------------------------------

    def start(self, record_id: str) -> None:
        """Create the RunnerProcess (launch=self.launch) and start it on a worker.

        State changes arrive as `runner_state` from the process callbacks.
        """
        record = self._record_for("start", record_id)
        if record is None:
            return
        if self.is_running(record_id):
            self._fail("start", f"runner “{record.name}” 已在运行")
            return
        if not self._claim("start", record):
            return
        hooks = _ProcessHooks(self, record_id)
        try:
            proc = self._manager.process_for(
                record,
                launch=self._launch,
                on_line=hooks.on_line,
                on_state=hooks.on_state,
                on_job_finished=hooks.on_job_finished,
            )
        except RunnerDeckError as e:
            self._release(record_id)
            self._fail("start", str(e), e)
            return
        hooks.process = proc
        # Registered before start() so every callback finds it current and is_running() is
        # right by the time the first `runner_state` arrives.
        self._processes[record_id] = proc
        self._submit(
            "start",
            proc.start,
            on_finished=lambda _r: self._release(record_id),
            on_failed=lambda _m, _e: self._start_failed(record_id),
        )

    def _start_failed(self, record_id: str) -> None:
        self._release(record_id)
        self._refresh_displays([record_id])

    def stop(self, record_id: str) -> None:
        """proc.stop() on a worker (default timeout: idle 15 s / job 90 s), then the stop mark
        (manager.record_stop, still on the worker); `runner_stopped`."""
        record = self._record_for("stop", record_id)
        if record is None:
            return
        proc = self._processes.get(record_id)
        if proc is None or not proc.is_running():
            self._fail("stop", f"runner “{record.name}” 没有在运行")
            return
        if not self._claim("stop", record):
            return
        self._submit(
            "stop",
            self._do_stop,
            proc,
            on_finished=lambda result: self._stop_done(record_id, result),
            on_failed=lambda _m, _e: self._release(record_id),
        )

    def _do_stop(self, proc: RunnerProcess, timeout: float | None = None) -> StopResult:
        """worker (or a shutdown thread): stop, then remember how for the next status."""
        result = proc.stop(timeout)
        self._manager.record_stop(proc.runner_dir, forced=not result.graceful)
        return result

    def _stop_done(self, record_id: str, result: StopResult) -> None:
        self._release(record_id)
        self.runner_stopped.emit(record_id, result)
        self._refresh_displays([record_id])

    def start_all(self) -> None:
        for record in self._config.runners:
            if not self.is_running(record.id) and not self.is_busy(record.id):
                self.start(record.id)

    def stop_all(self) -> None:
        for record in self._config.runners:
            if self.is_running(record.id) and not self.is_busy(record.id):
                self.stop(record.id)

    # --- remove -------------------------------------------------------------------------------

    def remove(self, record_id: str, *, keep_work: bool = False) -> None:
        """Stop the runner if it runs, then manager.remove_runner; `remove_progress` /
        `runner_removed` + `config_changed`."""
        record = self._record_for("remove", record_id)
        if record is None or not self._claim("remove", record):
            return
        proc = self._processes.get(record_id)
        self._submit(
            "remove",
            self._do_remove,
            record,
            proc,
            keep_work,
            progress=True,
            on_progress=self.remove_progress,
            on_finished=lambda result: self._remove_done(record_id, result),
            on_failed=lambda _m, _e: self._release(record_id),
        )

    def _do_remove(
        self,
        record: RunnerRecord,
        proc: RunnerProcess | None,
        keep_work: bool,
        progress: Callable[[str, str], None],
    ) -> RemoveResult:  # worker
        if proc is not None and proc.is_running():
            progress(STEP_STOP, f"停止 runner {record.name}")
            proc.stop()
        return self._manager.remove_runner(record.id, keep_work=keep_work, progress=progress)

    def _remove_done(self, record_id: str, result: RemoveResult) -> None:
        self._processes.pop(record_id, None)
        self._drop_caches(record_id)
        self._release(record_id)
        self._reload_config("remove")
        self.runner_removed.emit(record_id, result)

    # --- external processes / discovery ---------------------------------------------------------

    def discover(self) -> None:
        """manager.discover on a worker; `discovery_updated(Discovery)`. failed("discover")."""
        self._submit("discover", self._manager.discover, on_finished=self.discovery_updated)

    def kill_external(self, record_id: str) -> None:
        """Hard-kill runner processes in the record's directory that we do not own;
        `external_stopped(record_id, ExternalStop)`. failed("kill")."""
        if self._claim_external("kill", record_id):
            self._submit(
                "kill",
                self._manager.kill_external,
                record_id,
                on_finished=lambda result: self._kill_external_done(record_id, result),
                on_failed=lambda _m, _e: self._release(record_id),
            )

    def _kill_external_done(self, record_id: str, result: ExternalStop) -> None:
        self._release(record_id)
        self._note_external_killed(record_id, result)
        self.external_stopped.emit(record_id, result)

    def take_over(self, record_id: str) -> None:
        """kill_external, then (main thread) start(record_id) once nothing survived.

        The forced kill leaves the old GitHub session behind, so the new run first shows
        "等待旧会话过期" (RECONNECTING). failed("take_over").
        """
        if self._claim_external("take_over", record_id):
            self._submit(
                "take_over",
                self._manager.kill_external,
                record_id,
                on_finished=lambda result: self._take_over_killed(record_id, result),
                on_failed=lambda _m, _e: self._release(record_id),
            )

    def _take_over_killed(self, record_id: str, result: ExternalStop) -> None:
        self._release(record_id)
        self._note_external_killed(record_id, result)
        self.external_stopped.emit(record_id, result)
        if not result.ok:
            pids = "、".join(str(pid) for pid in result.survivors)
            self._fail("take_over", f"还有进程没能结束（PID {pids}），未接管")
            return
        self.start(record_id)

    def kill_stray(self, path: str) -> None:
        """Hard-kill runner processes of an unrecorded directory under runners_root;
        `external_stopped(path, ExternalStop)`. failed("kill")."""
        self._submit(
            "kill",
            self._manager.kill_stray,
            path,
            on_finished=lambda result: self._kill_stray_done(path, result),
        )

    def _kill_stray_done(self, path: str, result: ExternalStop) -> None:
        self.external_stopped.emit(path, result)
        self.discover()  # refresh the dashboard banner

    def _claim_external(self, op: str, record_id: str) -> bool:
        record = self._record_for(op, record_id)
        if record is None:
            return False
        if self.is_running(record_id):
            self._fail(op, f"runner “{record.name}” 由 RunnerDeck 运行，请直接停止")
            return False
        return self._claim(op, record)

    def _note_external_killed(self, record_id: str, result: ExternalStop) -> None:
        """Mirror what manager.kill_external did on disk until the next refresh says it."""
        cached = self._locals.get(record_id)
        if cached is not None and result.ok:
            changes: dict[str, Any] = {"external": False}
            if result.killed:  # recorded as a forced stop: the session lingers ~3 min
                changes.update(last_stop_forced=True, stopped_at=self._clock())
            self._locals[record_id] = dataclasses.replace(cached, **changes)
        self._refresh_displays([record_id])

    # --- diagnostics logs -----------------------------------------------------------------------

    def load_diag_logs(self, record_id: str) -> None:
        """The runner's `_diag/*.log` files on a worker; `diag_logs_loaded(id, list[DiagLog])`.
        failed("diag")."""
        record = self._record_for("diag", record_id)
        if record is None:
            return
        self._submit(
            "diag",
            self._diag.list_logs,
            Path(record.path),
            on_finished=lambda logs: self.diag_logs_loaded.emit(record_id, logs),
        )

    def load_diag_text(self, record_id: str, path: str) -> None:
        """The (redacted) tail of one log on a worker; `diag_text_loaded(id, path, text)`.

        `path` must lie inside the record's `_diag` directory. failed("diag").
        """
        record = self._record_for("diag", record_id)
        if record is None:
            return
        self._submit(
            "diag",
            self._do_load_diag_text,
            record,
            path,
            on_finished=lambda text: self.diag_text_loaded.emit(record_id, path, text),
        )

    def _do_load_diag_text(self, record: RunnerRecord, path: str) -> str:  # worker
        target = Path(path)
        if not _is_inside(target, Path(record.path) / DIAG_DIR):
            raise InvalidInputError(f"只能查看 runner “{record.name}” 的 {DIAG_DIR} 目录下的日志")
        return self._diag.read_tail(target)

    # --- launch on login ------------------------------------------------------------------------

    def load_autostart_state(self) -> None:
        """Read the Run entry on a worker; `autostart_state_loaded(AutostartState)`."""
        self._submit(
            "launch_on_login", self._autostart.state, on_finished=self.autostart_state_loaded
        )

    def set_launch_on_login(self, enabled: bool) -> None:
        """Write / delete the Run entry, then save settings.launch_on_login (worker);
        `autostart_state_loaded` + `config_changed`. failed("launch_on_login")."""
        if not self.autostart_supported:
            self._fail("launch_on_login", AUTOSTART_UNSUPPORTED)
            return
        self._submit(
            "launch_on_login",
            self._do_set_launch_on_login,
            enabled,
            on_finished=self._launch_on_login_done,
        )

    def _do_set_launch_on_login(self, enabled: bool) -> AutostartState:  # worker
        state = self._autostart.enable() if enabled else self._autostart.disable()
        config = self._store.load()
        if config.settings.launch_on_login != enabled:
            config.settings.launch_on_login = enabled
            self._store.save(config)
        return state

    def _launch_on_login_done(self, state: AutostartState) -> None:
        self.autostart_state_loaded.emit(state)
        self._reload_config("launch_on_login")

    def reconcile_autostart(self) -> None:
        """At launch: make the Run entry match settings.launch_on_login (rewrites a stale
        command, e.g. after the folder was moved); `autostart_state_loaded`."""
        self._submit(
            "launch_on_login",
            self._autostart.reconcile,
            self._config.settings.launch_on_login,
            on_finished=self.autostart_state_loaded,
        )

    # --- status / settings / misc ---------------------------------------------------------------

    def refresh_statuses(self, *, remote: bool = True, quiet: bool = False) -> None:
        """manager.statuses on a worker; `statuses_updated(rows)`. Ignored while one runs.

        remote=False: local process scan only; the cached GitHub snapshots are kept.
        quiet: a failure is only logged (no `failed`), for the background poller.
        Each row's `display` is replaced by the live merge (a local state change after the
        refresh was submitted must not be overwritten by the older result).
        """
        if self._refreshing:
            return
        self._set_refreshing(True)
        self._submit(
            "status",
            self._manager.statuses,
            dict(self._processes),
            remote=remote,
            on_finished=lambda rows: self._statuses_done(rows, remote),
            on_failed=lambda _m, _e: self._set_refreshing(False),
            quiet=quiet,
        )

    def _statuses_done(self, rows: list[RunnerStatusRow], remote: bool) -> None:
        merged = self._absorb_rows(rows, remote=remote)
        self._set_refreshing(False)
        self.statuses_updated.emit(merged)

    def _set_refreshing(self, value: bool) -> None:
        if value != self._refreshing:
            self._refreshing = value
            self.refreshing_changed.emit(value)

    def save_settings(self, settings: AppSettings) -> None:
        """Save on a worker; a changed launch_on_login also rewrites the Run entry first
        (`autostart_state_loaded`). `config_changed`; failed("settings").

        Only the fields that differ from the cached config (the base the UI edited) are
        written, onto freshly loaded settings: a copy made from a cache that is stale by one
        in-flight save must not undo that save (e.g. turn launch on login back off).
        """
        base = self._config.settings
        changes = {
            name: getattr(settings, name)
            for name in AppSettings.model_fields
            if getattr(settings, name) != getattr(base, name)
        }
        self._submit(
            "settings",
            self._do_save_settings,
            changes,
            on_finished=self._settings_saved,
        )

    def _do_save_settings(self, changes: dict[str, Any]) -> AutostartState | None:  # worker
        config = self._store.load()
        state: AutostartState | None = None
        wanted = changes.get("launch_on_login")
        if (
            wanted is not None
            and wanted != config.settings.launch_on_login
            and self._autostart.supported
        ):
            state = self._autostart.enable() if wanted else self._autostart.disable()
        config.settings = config.settings.model_copy(update=changes)
        self._store.save(config)
        return state

    def _settings_saved(self, state: AutostartState | None) -> None:
        if state is not None:
            self.autostart_state_loaded.emit(state)
        self._reload_config("settings")

    def open_dir(self, record_id: str) -> None:
        """Open the runner directory in the file manager (main thread, no signal on success)."""
        record = self._config.get(record_id)
        if record is None:
            log.warning("open_dir: unknown runner %s", record_id)
            return
        if not os.path.isdir(record.path):
            self._fail("open_dir", f"目录不存在：{record.path}")
            return
        if not self._open_url(QUrl.fromLocalFile(record.path)):
            self._fail("open_dir", f"无法打开目录：{record.path}")

    def shutdown(self, timeout: float | None = None) -> list[str]:
        """Blocking: stop every running runner in parallel; names of those that did not stop.

        Uses plain threads (not the pool, which may be busy with an add); each also writes its
        stop mark. Meant for the window's close handler, which may call it from the main thread.
        """
        running = [(rid, proc) for rid, proc in self._processes.items() if proc.is_running()]
        if not running:
            return []
        errors: dict[str, BaseException] = {}

        def stop_one(rid: str, proc: RunnerProcess) -> None:
            try:
                self._do_stop(proc, timeout)
            except BaseException as e:  # noqa: BLE001 - report, never lose the other stops
                log.exception("shutdown: stopping %s failed", rid)
                errors[rid] = e

        threads = [
            threading.Thread(
                target=stop_one, args=(rid, proc), name=f"shutdown-{rid[:8]}", daemon=True
            )
            for rid, proc in running
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        failed = [self._name_of(rid) for rid, proc in running if rid in errors or proc.is_running()]
        log.info("shutdown: %d runner(s) stopped, %d still running", len(running), len(failed))
        return failed

    def emergency_stop_all(self) -> int:
        """Windows is logging off / shutting down: ask every running runner to stop
        (RunnerProcess.signal_stop: no waiting, no kill, no disk IO) and return how many were
        asked. Main thread, never blocks."""
        count = 0
        for rid, proc in list(self._processes.items()):
            if not proc.is_running():
                continue
            try:
                if proc.signal_stop():
                    count += 1
            except Exception:  # one runner must not keep the others from being signalled
                log.exception("emergency stop of %s failed", rid)
        log.info("emergency stop: signalled %d runner(s)", count)
        return count

    # --- bridge slots (main thread) ---------------------------------------------------------------

    def _on_bridge_line(self, record_id: str, line: str) -> None:
        self.runner_line.emit(record_id, line)

    def _on_bridge_state(self, record_id: str, state: object, job: object) -> None:
        self.runner_state.emit(record_id, state, job)
        self._refresh_displays([record_id])

    def _on_remerge_timer(self) -> None:
        self._refresh_displays()

    # --- live merge (main thread) -----------------------------------------------------------------

    def _live_local(self, record_id: str, now: datetime) -> LocalSnapshot:
        """Cached row.local with our process's live fields on top (docs/m3-design.md §4.1)."""
        cached = self._locals.get(record_id)
        proc = self._processes.get(record_id)
        if proc is None:
            if cached is not None:
                return cached
            return LocalSnapshot(
                state=LocalState.STOPPED, running=False, external=False, state_since=now
            )
        running = proc.is_running()
        stopped_at, forced = proc.stopped_at, proc.last_stop_forced
        if (
            cached is not None
            and cached.stopped_at is not None
            and (stopped_at is None or cached.stopped_at > stopped_at)
        ):
            # A newer stop mark (e.g. the CLI stopped it from another terminal).
            stopped_at, forced = cached.stopped_at, cached.last_stop_forced
        return LocalSnapshot(
            state=proc.state,
            running=running,
            external=False if running or cached is None else cached.external,
            state_since=proc.state_since,
            last_stop_forced=forced,
            stopped_at=stopped_at,
            current_job=proc.current_job,
            last_error=proc.last_error,
        )

    def _live_display(self, record_id: str, now: datetime) -> DisplayStatus:
        return merge_status(self._live_local(record_id, now), self._remotes.get(record_id), now=now)

    def _refresh_displays(
        self, record_ids: Iterable[str] | None = None, *, now: datetime | None = None
    ) -> None:
        """Re-merge the given records (default: all) and emit `display_changed` on change."""
        now = now or self._clock()
        if record_ids is None:
            ids = [r.id for r in self._config.runners]
        else:
            ids = [rid for rid in record_ids if self._config.get(rid) is not None]
        for rid in ids:
            shown = self._live_display(rid, now)
            if self._displays.get(rid) != shown:
                self._displays[rid] = shown
                self.display_changed.emit(rid, shown)

    def _absorb_rows(self, rows: list[RunnerStatusRow], *, remote: bool) -> list[RunnerStatusRow]:
        """Cache a statuses() result; the rows with their display replaced by the live merge.

        remote=False rows say nothing about GitHub, so the remote cache is kept; in a remote
        refresh a repo whose list failed resets its records to UNKNOWN.
        """
        for row in rows:
            rid = row.record.id
            if self._config.get(rid) is None:
                continue  # removed / forgotten while the refresh ran
            self._locals[rid] = row.local
            if remote:
                self._remotes[rid] = (
                    row.remote if row.remote_error is None else RemoteSnapshot(RemoteStatus.UNKNOWN)
                )
        now = self._clock()
        merged = [
            dataclasses.replace(row, display=self._live_display(row.record.id, now))
            if self._config.get(row.record.id) is not None
            else row
            for row in rows
        ]
        self._refresh_displays(now=now)
        return merged

    def _drop_caches(self, record_id: str) -> None:
        for cache in (self._locals, self._remotes, self._displays):
            cache.pop(record_id, None)

    def _sync_remerge_timer(self) -> None:
        if self._config.runners and not self._remerge_timer.isActive():
            self._remerge_timer.start()
        elif not self._config.runners and self._remerge_timer.isActive():
            self._remerge_timer.stop()

    def _on_bridge_job(self, record_id: str, job: str, result: str) -> None:
        self.runner_job_finished.emit(record_id, job, result)

    # --- helpers --------------------------------------------------------------------------------

    def _client_factory(self) -> GitHubClient:
        """Zero-argument factory for the manager (worker threads): RUNNERDECK_PAT > keyring."""
        config = self._config
        pat, _source = account.resolve_pat(config.account, self._environ, login_hint=LOGIN_HINT)
        return self._new_client(pat, config.settings.proxy)

    def _submit(
        self,
        op: str,
        fn: Callable[..., Any],
        *args: Any,
        on_finished: Callable[[Any], Any],
        on_failed: Callable[[str, BaseException], Any] | None = None,
        on_progress: Callable[[str, str], Any] | None = None,
        progress: bool = False,
        quiet: bool = False,
        **kwargs: Any,
    ) -> Task:
        """quiet: a failure is logged instead of emitting `failed`."""

        def failed(message: str, exc: BaseException) -> None:
            if on_failed is not None:
                on_failed(message, exc)
            if quiet:
                log.warning("%s failed (quiet): %s", op, redact(message))
            else:
                self._fail(op, message, exc)

        return submit(
            self._pool,
            fn,
            *args,
            on_finished=on_finished,
            on_failed=failed,
            on_progress=on_progress,
            progress=progress,
            **kwargs,
        )

    def _fail(self, op: str, message: str, exc: BaseException | None = None) -> None:
        message = redact(message)
        log.warning("%s failed: %s", op, message)
        self.failed.emit(op, message, exc)

    def _record_for(self, op: str, record_id: str) -> RunnerRecord | None:
        record = self._config.get(record_id)
        if record is None:
            self._fail(op, "找不到这个 runner（记录可能已被移除）")
        return record

    def _name_of(self, record_id: str) -> str:
        record = self._config.get(record_id)
        return record.name if record is not None else record_id

    def _claim(self, op: str, record: RunnerRecord) -> bool:
        """Mark the runner busy; False (and `failed`) if another operation is running on it."""
        if record.id in self._busy:
            self._fail(op, f"runner “{record.name}” 正在执行其他操作，请稍候")
            return False
        self._busy.add(record.id)
        self.busy_changed.emit(record.id, True)
        return True

    def _release(self, record_id: str) -> None:
        if record_id in self._busy:
            self._busy.discard(record_id)
            self.busy_changed.emit(record_id, False)

    def _reload_config(self, op: str) -> bool:
        if not self._load_config(op):
            return False
        self._announce_config()
        return True

    def _load_config(self, op: str) -> bool:
        """Re-read config.json after a write (no signal); drop caches of vanished records."""
        try:
            config = self._store.load()
        except RunnerDeckError as e:
            self._fail(op, f"操作已完成，但重新读取配置失败：{e}", e)
            return False
        self._config = config
        known = {r.id for r in config.runners}
        for cache in (self._locals, self._remotes, self._displays):
            for rid in [k for k in cache if k not in known]:
                del cache[rid]
        self._sync_remerge_timer()
        return True

    def _announce_config(self) -> None:
        self.config_changed.emit(self._config)
        self._refresh_displays()
