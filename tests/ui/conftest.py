"""Fixtures for the ui tests: a recording FakeController with the controller's surface
(docs/m2-design.md §4 + docs/m3-design.md §4.1).

The real controller (runnerdeck.app.controller) is replaced by FakeController so these tests
never touch the network, keyring or subprocesses. Tests call `fake.<signal>.emit(...)` to play
the controller's answers. Helpers build core models with obviously fake values.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

from runnerdeck.core.manager import AddPlan, RunnerStatusRow
from runnerdeck.core.models import (
    AppConfig,
    AppSettings,
    AutostartState,
    DisplayState,
    GitHubUser,
    LocalState,
    RemoteStatus,
    Repo,
    RunnerRecord,
)
from runnerdeck.core.status import DisplayStatus, LocalSnapshot, RemoteSnapshot, display_label
from runnerdeck.ui.compat import (
    disable_mac_native_frameless,
    tolerate_offscreen_window_handles,
)

disable_mac_native_frameless()
tolerate_offscreen_window_handles()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every RunnerDeck app dir (config, cache, logs, runners root) to tmp_path."""
    monkeypatch.setenv("RUNNERDECK_HOME", str(tmp_path))
    monkeypatch.delenv("RUNNERDECK_PAT", raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def app_quits(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Record QApplication.quit() instead of calling it (MainWindow.quit_app does).

    A real quit() outside exec() sets Qt's sticky quitNow flag: every later event loop
    (qtbot.wait / waitUntil / waitSignal) would then return at once and later tests time out.
    """
    from PyQt5.QtWidgets import QApplication

    quits: list[bool] = []
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: quits.append(True)))
    return quits


class FakeController(QObject):
    """Recording stand-in for RunnerController (docs/m2-design.md §4).

    Every method appends ("name", args, kwargs) to `calls` and returns None; tests emit the
    signals themselves. `auto_plan`, `auto_add_steps` / `auto_added` make plan_add / add_runner
    answer synchronously, which is how the wizard tests walk through the three steps.
    """

    config_changed = pyqtSignal(object)
    logged_in = pyqtSignal(object)
    logged_out = pyqtSignal()
    account_loaded = pyqtSignal(object, str)
    repos_loaded = pyqtSignal(object)
    plan_ready = pyqtSignal(object)
    add_progress = pyqtSignal(str, str)
    runner_added = pyqtSignal(object)
    runner_state = pyqtSignal(str, object, object)
    runner_stopped = pyqtSignal(str, object)
    remove_progress = pyqtSignal(str, str)
    runner_removed = pyqtSignal(str, object)
    statuses_updated = pyqtSignal(object)
    failed = pyqtSignal(str, str)
    busy_changed = pyqtSignal(str, bool)
    runner_line = pyqtSignal(str, str)
    runner_job_finished = pyqtSignal(str, str, str)
    refreshing_changed = pyqtSignal(bool)
    # M3 (docs/m3-design.md §4.1)
    display_changed = pyqtSignal(str, object)  # record id, DisplayStatus
    discovery_updated = pyqtSignal(object)  # Discovery
    runner_imported = pyqtSignal(object)  # RunnerRecord
    runner_forgotten = pyqtSignal(str)  # record id
    external_stopped = pyqtSignal(str, object)  # record id or stray path, ExternalStop
    diag_logs_loaded = pyqtSignal(str, object)  # record id, list[DiagLog]
    diag_text_loaded = pyqtSignal(str, str, str)  # record id, path, text
    autostart_state_loaded = pyqtSignal(object)  # AutostartState

    def __init__(self, config: AppConfig | None = None, *, logged_in: bool = True) -> None:
        super().__init__()
        self.config: AppConfig = config or AppConfig(account="octocat" if logged_in else None)
        self.is_logged_in = logged_in
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.states: dict[str, tuple[LocalState, str | None]] = {}
        self.busy: set[str] = set()
        self.outputs: dict[str, list[str]] = {}
        self.displays: dict[str, DisplayStatus] = {}
        self.shutdown_leftovers: list[str] = []
        self.auto_plan: AddPlan | None = None
        self.auto_add_steps: list[tuple[str, str]] = []
        self.auto_added: RunnerRecord | None = None
        self.refreshing = False
        self.remote_busy_ids: set[str] = set()
        self.autostart_supported = True
        self.emergency_count = 0

    # --- read-only -----------------------------------------------------------------

    @property
    def records(self) -> list[RunnerRecord]:
        return list(self.config.runners)

    def local_state(self, record_id: str) -> tuple[LocalState, str | None]:
        return self.states.get(record_id, (LocalState.STOPPED, None))

    def is_running(self, record_id: str) -> bool:
        return self.local_state(record_id)[0] not in (LocalState.STOPPED, LocalState.ERROR)

    def is_busy(self, record_id: str) -> bool:
        return record_id in self.busy

    def output(self, record_id: str) -> list[str]:
        return list(self.outputs.get(record_id, []))

    def running_ids(self) -> list[str]:
        return [r.id for r in self.records if self.is_running(r.id)]

    def display(self, record_id: str) -> DisplayStatus:
        if record_id in self.displays:
            return self.displays[record_id]
        state, job = self.local_state(record_id)
        return make_display(DisplayState(state.value), job=job)

    def all_displays(self) -> dict[str, DisplayStatus]:
        return {r.id: self.display(r.id) for r in self.records}

    def remote_busy(self, record_id: str) -> bool:
        return record_id in self.remote_busy_ids

    # --- recorded operations ---------------------------------------------------------------

    def _record(self, _name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((_name, args, kwargs))

    def calls_of(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(a, k) for n, a, k in self.calls if n == name]

    def login(self, pat: str) -> None:
        self._record("login", pat)

    def logout(self) -> None:
        self._record("logout")

    def load_account(self) -> None:
        self._record("load_account")

    def load_repos(self) -> None:
        self._record("load_repos")

    def plan_add(
        self,
        owner: str,
        repo: str,
        *,
        name: str | None = None,
        labels: list[str] | None = None,
        ps_policy: bool = True,
    ) -> None:
        self._record("plan_add", owner, repo, name=name, labels=labels, ps_policy=ps_policy)
        if self.auto_plan is not None:
            self.plan_ready.emit(self.auto_plan)

    def add_runner(
        self, plan: AddPlan, *, start_after: bool = False, auto_start: bool = False
    ) -> None:
        self._record("add_runner", plan, start_after=start_after, auto_start=auto_start)
        for step, text in self.auto_add_steps:
            self.add_progress.emit(step, text)
        if self.auto_added is not None:
            self.config.upsert(self.auto_added)
            self.runner_added.emit(self.auto_added)
            self.config_changed.emit(self.config)

    def start(self, record_id: str) -> None:
        self._record("start", record_id)

    def stop(self, record_id: str) -> None:
        self._record("stop", record_id)

    def start_all(self) -> None:
        self._record("start_all")

    def stop_all(self) -> None:
        self._record("stop_all")

    def remove(self, record_id: str, *, keep_work: bool = False) -> None:
        self._record("remove", record_id, keep_work=keep_work)

    def refresh_statuses(self, *, remote: bool = True, quiet: bool = False) -> None:
        self._record("refresh_statuses", remote=remote, quiet=quiet)

    def save_settings(self, settings: AppSettings) -> None:
        self._record("save_settings", settings)
        self.config.settings = settings
        self.config_changed.emit(self.config)

    def open_dir(self, record_id: str) -> None:
        self._record("open_dir", record_id)

    def shutdown(self, timeout: float | None = None) -> list[str]:
        self._record("shutdown", timeout=timeout)
        return list(self.shutdown_leftovers)

    # M3 (docs/m3-design.md §4.1)

    def start_auto_runners(self) -> None:
        self._record("start_auto_runners")

    def set_auto_start(self, record_id: str, enabled: bool) -> None:
        self._record("set_auto_start", record_id, enabled)

    def import_runner(self, path: str, *, auto_start: bool = False) -> None:
        self._record("import_runner", path, auto_start=auto_start)

    def forget(self, record_id: str) -> None:
        self._record("forget", record_id)

    def discover(self) -> None:
        self._record("discover")

    def kill_external(self, record_id: str) -> None:
        self._record("kill_external", record_id)

    def take_over(self, record_id: str) -> None:
        self._record("take_over", record_id)

    def kill_stray(self, path: str) -> None:
        self._record("kill_stray", path)

    def load_diag_logs(self, record_id: str) -> None:
        self._record("load_diag_logs", record_id)

    def load_diag_text(self, record_id: str, path: str) -> None:
        self._record("load_diag_text", record_id, path)

    def load_autostart_state(self) -> None:
        self._record("load_autostart_state")

    def set_launch_on_login(self, enabled: bool) -> None:
        self._record("set_launch_on_login", enabled)
        self.config.settings = self.config.settings.model_copy(update={"launch_on_login": enabled})
        self.autostart_state_loaded.emit(
            AutostartState(supported=True, registered=enabled, command="x", expected="x")
        )
        self.config_changed.emit(self.config)

    def reconcile_autostart(self) -> None:
        self._record("reconcile_autostart")

    def emergency_stop_all(self) -> int:
        self._record("emergency_stop_all")
        return self.emergency_count

    # --- test helpers ---------------------------------------------------------------------

    def set_state(self, record_id: str, state: LocalState, job: str | None = None) -> None:
        """Change the local state; emit runner_state and display_changed like the controller.

        The display follows the local state unless a test pinned one in `displays`.
        """
        self.states[record_id] = (state, job)
        self.runner_state.emit(record_id, state, job)
        self.display_changed.emit(record_id, self.display(record_id))

    def set_display(self, record_id: str, display: DisplayStatus) -> None:
        """Pin the merged display of a runner and emit display_changed."""
        self.displays[record_id] = display
        self.display_changed.emit(record_id, display)


# --- model helpers --------------------------------------------------------------------------


def make_record(name: str = "pc-demo", repo: str = "demo", **overrides: Any) -> RunnerRecord:
    values: dict[str, Any] = {
        "owner": "octocat",
        "repo": repo,
        "name": name,
        "path": f"/tmp/runners/{repo}",
        "github_runner_id": 42,
    }
    values.update(overrides)
    return RunnerRecord(**values)


def make_repo(full_name: str = "octocat/demo", **overrides: Any) -> Repo:
    owner, _, name = full_name.partition("/")
    values: dict[str, Any] = {
        "name": name,
        "full_name": full_name,
        "owner": owner,
        "private": True,
        "html_url": f"https://github.com/{full_name}",
    }
    values.update(overrides)
    return Repo(**values)


def make_user(login: str = "octocat", **overrides: Any) -> GitHubUser:
    values: dict[str, Any] = {"login": login, "id": 1, "token_kind": "fine-grained"}
    values.update(overrides)
    return GitHubUser(**values)


def make_plan(full_name: str = "octocat/demo", **overrides: Any) -> AddPlan:
    owner, _, repo = full_name.partition("/")
    values: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "name": f"pc-{repo}",
        "labels": ("gpu",),
        "target_dir": Path("/tmp/runners") / repo,
        "private": True,
        "runner_env": (("PSExecutionPolicyPreference", "RemoteSigned"),),
    }
    values.update(overrides)
    return AddPlan(**values)


def make_display(
    state: DisplayState, *, job: str | None = None, detail: str | None = None
) -> DisplayStatus:
    return DisplayStatus(state=state, label=display_label(state, job), detail=detail)


def make_status_row(
    record: RunnerRecord, state: DisplayState, *, detail: str | None = None
) -> RunnerStatusRow:
    now = datetime.now(UTC)
    local = LocalSnapshot(
        state=LocalState.IDLE if state is DisplayState.IDLE else LocalState.STOPPED,
        running=state is DisplayState.IDLE,
        external=False,
        state_since=now,
    )
    remote = RemoteSnapshot(
        RemoteStatus.ONLINE if state is DisplayState.IDLE else RemoteStatus.OFFLINE
    )
    return RunnerStatusRow(
        record=record, display=make_display(state, detail=detail), local=local, remote=remote
    )


@pytest.fixture
def fake() -> FakeController:
    return FakeController()


@pytest.fixture
def two_records() -> list[RunnerRecord]:
    return [make_record("pc-alpha", "alpha"), make_record("pc-beta", "beta")]


@pytest.fixture
def fake_with_records(two_records: list[RunnerRecord]) -> FakeController:
    return FakeController(AppConfig(account="octocat", runners=two_records))


@pytest.fixture
def auto_accept(monkeypatch: pytest.MonkeyPatch) -> Callable[[type, bool], None]:
    """Stub `exec` of a modal dialog class to return a fixed answer without blocking."""

    def _stub(dialog_cls: type, answer: bool) -> None:
        monkeypatch.setattr(dialog_cls, "exec", lambda self: answer)

    return _stub


@pytest.fixture
def no_infobars(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, str, str]]]:
    """Capture InfoBar.error/warning/success calls instead of animating them."""
    from qfluentwidgets import InfoBar

    shown: list[tuple[str, str, str]] = []

    def _make(level: str) -> Callable[..., None]:
        def _show(title: str, content: str = "", *args: Any, **kwargs: Any) -> None:
            shown.append((level, title, content))

        return staticmethod(_show)  # type: ignore[return-value]

    for level in ("error", "warning", "success", "info"):
        monkeypatch.setattr(InfoBar, level, _make(level))
    yield shown
