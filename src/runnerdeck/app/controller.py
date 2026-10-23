"""RunnerController: the UI's only entry point to the core layer (docs/m2-design.md §4).

Every blocking RunnerManager / GitHubClient / RunnerProcess call is submitted to a QThreadPool
through app.workers; results come back as signals emitted on the main thread. RunnerProcess
callbacks arrive on the process's reader thread and are forwarded through an internal _Bridge
whose signals are queued to the main thread.

Threading model: all controller state (`_processes`, `_busy`, cached config and displays) is
owned by the main thread; workers only get the objects they need as arguments. Per runner only
one of start / stop / remove runs at a time (`busy_changed`); add_runner is serialized by the
manager's own lock.

Errors: `failed(op, message, exc)` is always emitted from the main thread with a redacted,
Chinese `message`; the controller never shows dialogs.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from PyQt5.QtCore import QObject, Qt, QThreadPool, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices

from runnerdeck.app.workers import Task, submit
from runnerdeck.core import account, secrets
from runnerdeck.core.errors import RunnerDeckError
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
    GitHubUser,
    LocalState,
    Repo,
    RunnerRecord,
)
from runnerdeck.core.process import Launch, RunnerProcess, StopResult
from runnerdeck.core.redact import redact
from runnerdeck.core.status import DisplayStatus, LocalSnapshot, merge_status
from runnerdeck.core.store import ConfigStore

log = logging.getLogger(__name__)

LOGIN_HINT = "请先登录"
# Progress step id emitted by remove() before manager.remove_runner's own steps when the
# runner had to be stopped first (not a manager.Step).
STEP_STOP = "stop"

ManagerFactory = Callable[[ConfigStore, ClientFactory], RunnerManager]
PatClientFactory = Callable[[str, str | None], GitHubClient]  # (pat, proxy) -> client
OpenUrlFn = Callable[[QUrl], bool]


def _default_client(pat: str, proxy: str | None) -> GitHubClient:
    return GitHubClient(pat, proxy=proxy)


def _default_manager(store: ConfigStore, client_factory: ClientFactory) -> RunnerManager:
    return RunnerManager(store, client_factory)


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
        parent: QObject | None = None,
    ) -> None:
        """Raises StoreError when config.json exists but cannot be read (never resets it).

        client_factory: builds the GitHubClient from (pat, proxy); tests inject a fake.
        open_url: what open_dir() calls (QDesktopServices.openUrl); tests inject a recorder.
        """
        super().__init__(parent)
        self._store = store if store is not None else ConfigStore()
        self._environ = environ
        self._pool = pool if pool is not None else QThreadPool.globalInstance()
        self._launch = Launch(launch)
        self._new_client: PatClientFactory = client_factory or _default_client
        self._open_url: OpenUrlFn = open_url or QDesktopServices.openUrl
        self._manager = (manager_factory or _default_manager)(self._store, self._client_factory)

        self._processes: dict[str, RunnerProcess] = {}
        self._busy: set[str] = set()
        self._displays: dict[str, DisplayStatus] = {}
        self._refreshing = False

        self._bridge = _Bridge(self)
        self._bridge.line.connect(self._on_bridge_line, Qt.QueuedConnection)
        self._bridge.state.connect(self._on_bridge_state, Qt.QueuedConnection)
        self._bridge.job.connect(self._on_bridge_job, Qt.QueuedConnection)

        self._config: AppConfig = self._store.load()

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
        """Last refresh_statuses result for the runner; else merged from local state alone."""
        cached = self._displays.get(record_id)
        if cached is not None:
            return cached
        now = _utcnow()
        proc = self._processes.get(record_id)
        if proc is None:
            local = LocalSnapshot(
                state=LocalState.STOPPED, running=False, external=False, state_since=now
            )
        else:
            local = LocalSnapshot(
                state=proc.state,
                running=proc.is_running(),
                external=False,
                state_since=proc.state_since,
                last_stop_forced=proc.last_stop_forced,
                stopped_at=proc.stopped_at,
                current_job=proc.current_job,
                last_error=proc.last_error,
            )
        return merge_status(local, None, now=now)

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

    def add_runner(self, plan: AddPlan, *, start_after: bool = False) -> None:
        """manager.add_runner with `add_progress`; `runner_added` + `config_changed`.

        On failure `failed("add", message, exc)`: an InstallError carries `.output`.
        """
        self._submit(
            "add",
            self._manager.add_runner,
            plan,
            progress=True,
            on_progress=self.add_progress,
            on_finished=lambda record: self._add_done(record, start_after),
        )

    def _add_done(self, record: RunnerRecord, start_after: bool) -> None:
        self._reload_config("add")
        self.runner_added.emit(record)
        if start_after:
            self.start(record.id)

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
        self._displays.pop(record_id, None)
        self._submit(
            "start",
            proc.start,
            on_finished=lambda _r: self._release(record_id),
            on_failed=lambda _m, _e: self._release(record_id),
        )

    def stop(self, record_id: str) -> None:
        """proc.stop() on a worker (default timeout: idle 15 s / job 90 s); `runner_stopped`."""
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
            proc.stop,
            on_finished=lambda result: self._stop_done(record_id, result),
            on_failed=lambda _m, _e: self._release(record_id),
        )

    def _stop_done(self, record_id: str, result: StopResult) -> None:
        self._release(record_id)
        self.runner_stopped.emit(record_id, result)

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
        self._displays.pop(record_id, None)
        self._release(record_id)
        self._reload_config("remove")
        self.runner_removed.emit(record_id, result)

    # --- status / settings / misc ---------------------------------------------------------------

    def refresh_statuses(self) -> None:
        """manager.statuses on a worker; `statuses_updated(rows)`. Ignored while one runs."""
        if self._refreshing:
            return
        self._set_refreshing(True)
        self._submit(
            "status",
            self._manager.statuses,
            dict(self._processes),
            on_finished=self._statuses_done,
            on_failed=lambda _m, _e: self._set_refreshing(False),
        )

    def _statuses_done(self, rows: list[RunnerStatusRow]) -> None:
        self._displays = {row.record.id: row.display for row in rows}
        self._set_refreshing(False)
        self.statuses_updated.emit(rows)

    def _set_refreshing(self, value: bool) -> None:
        if value != self._refreshing:
            self._refreshing = value
            self.refreshing_changed.emit(value)

    def save_settings(self, settings: AppSettings) -> None:
        self._submit(
            "settings",
            self._do_save_settings,
            settings,
            on_finished=lambda _r: self._reload_config("settings"),
        )

    def _do_save_settings(self, settings: AppSettings) -> None:  # worker
        config = self._store.load()
        config.settings = settings
        self._store.save(config)

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

        Uses plain threads (not the pool, which may be busy with an add). Meant for the window's
        close handler, which may call it from the main thread.
        """
        running = [(rid, proc) for rid, proc in self._processes.items() if proc.is_running()]
        if not running:
            return []
        errors: dict[str, BaseException] = {}

        def stop_one(rid: str, proc: RunnerProcess) -> None:
            try:
                proc.stop(timeout)
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

    # --- bridge slots (main thread) ---------------------------------------------------------------

    def _on_bridge_line(self, record_id: str, line: str) -> None:
        self.runner_line.emit(record_id, line)

    def _on_bridge_state(self, record_id: str, state: object, job: object) -> None:
        self._displays.pop(record_id, None)  # the merged status is stale now
        self.runner_state.emit(record_id, state, job)

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
        **kwargs: Any,
    ) -> Task:
        def failed(message: str, exc: BaseException) -> None:
            if on_failed is not None:
                on_failed(message, exc)
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
        try:
            self._config = self._store.load()
        except RunnerDeckError as e:
            self._fail(op, f"操作已完成，但重新读取配置失败：{e}", e)
            return False
        self.config_changed.emit(self._config)
        return True
