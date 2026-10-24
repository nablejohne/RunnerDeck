"""Tests for runnerdeck.app.poller.StatusPoller (docs/m3-design.md §4.2).

Most tests drive a small recording stand-in for the controller (only the signals and members the
poller uses) and call the timer slots directly with a fake monotonic clock, so nothing sleeps.
The debounce test uses a subclass with a short KICK_DELAY_MS and real Qt timers; the last tests
wire the poller to the real RunnerController with the fake manager.
"""

from __future__ import annotations

from typing import Any

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

from runnerdeck.app.poller import StatusPoller
from runnerdeck.core.errors import StoreError
from runnerdeck.core.models import AppConfig, AppSettings, LocalState, RemoteStatus
from runnerdeck.core.process import ExternalStop

from .conftest import WAIT_MS, record_model, status_row


class FakeClock:
    """time.monotonic stand-in."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class PollTarget(QObject):
    """The part of RunnerController the poller uses; records refresh_statuses calls."""

    runner_state = pyqtSignal(str, object, object)
    external_stopped = pyqtSignal(str, object)
    runner_added = pyqtSignal(object)
    runner_imported = pyqtSignal(object)
    config_changed = pyqtSignal(object)
    logged_in = pyqtSignal(object)
    statuses_updated = pyqtSignal(object)

    def __init__(self, *, records: int = 2, logged_in: bool = True, interval: int = 30) -> None:
        super().__init__()
        self.config = AppConfig(
            account="octo" if logged_in else None,
            settings=AppSettings(poll_interval_s=interval),
            runners=[record_model(f"r{i}", repo=f"repo{i}") for i in range(records)],
        )
        self.is_logged_in = logged_in
        self.refreshing = False
        self.refreshes: list[tuple[bool, bool]] = []  # (remote, quiet)

    @property
    def records(self) -> list[Any]:
        return list(self.config.runners)

    def refresh_statuses(self, *, remote: bool = True, quiet: bool = False) -> None:
        self.refreshes.append((remote, quiet))

    def remote_rows(self, *, failing: int) -> list[Any]:
        """Rows of a GitHub poll where the first `failing` records' repos failed."""
        return [
            status_row(r, remote_error="网络错误")
            if i < failing
            else status_row(r, remote=RemoteStatus.ONLINE)
            for i, r in enumerate(self.records)
        ]

    def local_rows(self) -> list[Any]:
        return [status_row(r, remote=RemoteStatus.UNKNOWN) for r in self.records]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _poller(target: PollTarget, clock: FakeClock, **kw: Any) -> StatusPoller:
    return StatusPoller(target, clock=clock, **kw)  # type: ignore[arg-type]


# --- start / interval ----------------------------------------------------------------------------


def test_start_polls_now_then_every_interval(qapp: Any, clock: FakeClock) -> None:
    target = PollTarget(interval=45)
    poller = _poller(target, clock)
    assert poller.interval_s == 45 and not poller.slow and not poller.active
    poller.start()
    assert poller.active
    assert target.refreshes == [(True, True)]
    assert poller._timer.isActive() and poller._timer.interval() == 45_000
    poller._on_tick()
    assert target.refreshes == [(True, True), (True, True)]


def test_not_logged_in_polls_only_locally(qapp: Any, clock: FakeClock) -> None:
    target = PollTarget(logged_in=False)
    poller = _poller(target, clock)
    poller.start()
    poller._on_kick()
    assert target.refreshes == [(False, True), (False, True)]


def test_no_records_no_poll(qapp: Any, clock: FakeClock) -> None:
    target = PollTarget(records=0)
    poller = _poller(target, clock)
    poller.start()
    poller._on_tick()
    poller._on_kick()
    assert target.refreshes == []


def test_skips_while_a_refresh_runs(qapp: Any, clock: FakeClock) -> None:
    target = PollTarget()
    poller = _poller(target, clock)
    target.refreshing = True
    poller.start()
    poller._on_tick()
    assert target.refreshes == []
    target.refreshing = False
    poller._on_tick()
    assert target.refreshes == [(True, True)]


def test_config_change_updates_the_interval(qapp: Any, clock: FakeClock) -> None:
    target = PollTarget(interval=30)
    poller = _poller(target, clock)
    target.config = target.config.model_copy(update={"settings": AppSettings(poll_interval_s=120)})
    target.config_changed.emit(target.config)
    assert poller.interval_s == 120
    assert not poller._timer.isActive()  # not started: nothing to restart
    poller.start()
    assert poller._timer.interval() == 120_000
    target.config_changed.emit(
        target.config.model_copy(update={"settings": AppSettings(poll_interval_s=15)})
    )
    assert poller._timer.isActive() and poller._timer.interval() == 15_000


def test_stop_stops_both_timers_and_ignores_kicks(qapp: Any, clock: FakeClock) -> None:
    target = PollTarget()
    poller = _poller(target, clock)
    poller.start()
    poller.kick()
    assert poller._kick_timer.isActive()
    poller.stop()
    assert not poller.active
    assert not poller._timer.isActive() and not poller._kick_timer.isActive()
    poller.kick()
    assert not poller._kick_timer.isActive()


def test_parent_defaults_to_the_controller(qapp: Any, clock: FakeClock) -> None:
    target = PollTarget()
    assert _poller(target, clock).parent() is target
    other = QObject()
    assert _poller(target, clock, parent=other).parent() is other


# --- kick ----------------------------------------------------------------------------------------


class FastPoller(StatusPoller):
    KICK_DELAY_MS = 30


def test_kicks_are_debounced_into_one_remote_poll(qapp: Any, qtbot: Any, clock: FakeClock) -> None:
    target = PollTarget()
    poller = FastPoller(target, clock=clock)  # type: ignore[arg-type]
    poller.start()
    target.refreshes.clear()
    poller.kick()
    qtbot.wait(10)
    poller.kick()  # restarts the delay
    qtbot.wait(10)
    poller.kick()
    assert target.refreshes == []
    qtbot.waitUntil(lambda: target.refreshes == [(True, True)], timeout=WAIT_MS)
    qtbot.wait(80)
    assert target.refreshes == [(True, True)]


def test_kick_delay_is_the_contract_value(qapp: Any, clock: FakeClock) -> None:
    poller = _poller(PollTarget(), clock)
    assert StatusPoller.KICK_DELAY_MS == 3000
    assert poller._kick_timer.isSingleShot() and poller._kick_timer.interval() == 3000


def test_kick_during_a_refresh_is_retried(qapp: Any, clock: FakeClock) -> None:
    target = PollTarget()
    poller = _poller(target, clock)
    poller.start()
    target.refreshes.clear()
    target.refreshing = True
    poller._on_kick()
    assert target.refreshes == []
    assert poller._kick_timer.isActive()  # re-armed
    target.refreshing = False
    poller._on_kick()
    assert target.refreshes == [(True, True)]


@pytest.mark.parametrize(
    ("state", "kicks"),
    [
        (LocalState.IDLE, True),
        (LocalState.STOPPED, True),
        (LocalState.ERROR, True),
        (LocalState.BUSY, False),
        (LocalState.STARTING, False),
        (LocalState.STOPPING, False),
        (LocalState.RECONNECTING, False),
    ],
)
def test_runner_state_kicks(qapp: Any, clock: FakeClock, state: LocalState, kicks: bool) -> None:
    target = PollTarget()
    poller = _poller(target, clock)
    poller.start()
    target.runner_state.emit("id", state, None)
    assert poller._kick_timer.isActive() is kicks


def test_external_stop_added_and_imported_kick(qapp: Any, clock: FakeClock) -> None:
    target = PollTarget()
    poller = _poller(target, clock)
    poller.start()
    for emit in (
        lambda: target.external_stopped.emit("id", ExternalStop([1], [])),
        lambda: target.runner_added.emit(target.records[0]),
        lambda: target.runner_imported.emit(target.records[0]),
    ):
        poller._kick_timer.stop()
        emit()
        assert poller._kick_timer.isActive()


# --- slow mode -----------------------------------------------------------------------------------


def test_all_repos_failing_slows_github_polling(qapp: Any, clock: FakeClock) -> None:
    target = PollTarget()
    poller = _poller(target, clock)
    poller.start()  # remote poll at t=1000
    target.statuses_updated.emit(target.remote_rows(failing=1))  # one repo answered
    assert not poller.slow
    target.statuses_updated.emit(target.remote_rows(failing=2))
    assert poller.slow
    target.refreshes.clear()
    clock.now += 30
    poller._on_tick()
    clock.now += StatusPoller.SLOW_INTERVAL_S - 31
    poller._on_tick()
    assert target.refreshes == [(False, True), (False, True)]  # local scans keep going
    clock.now += 1  # 300 s after the last GitHub poll
    poller._on_tick()
    clock.now += 30
    poller._on_tick()
    assert target.refreshes[2:] == [(True, True), (False, True)]


def test_kick_asks_github_even_when_slow(qapp: Any, clock: FakeClock) -> None:
    target = PollTarget()
    poller = _poller(target, clock)
    poller.start()
    target.statuses_updated.emit(target.remote_rows(failing=2))
    target.refreshes.clear()
    poller._on_kick()
    assert target.refreshes == [(True, True)]


def test_local_rows_say_nothing_about_github(qapp: Any, clock: FakeClock) -> None:
    target = PollTarget()
    poller = _poller(target, clock)
    target.statuses_updated.emit(target.remote_rows(failing=2))
    assert poller.slow
    target.statuses_updated.emit(target.local_rows())
    assert poller.slow
    target.statuses_updated.emit([])
    assert poller.slow
    target.statuses_updated.emit(target.remote_rows(failing=0))
    assert not poller.slow
    target.statuses_updated.emit(target.local_rows())
    assert not poller.slow


@pytest.mark.parametrize("reset", ["logged_in", "config_changed"])
def test_slow_mode_resets(qapp: Any, clock: FakeClock, reset: str) -> None:
    target = PollTarget()
    poller = _poller(target, clock)
    poller.start()
    target.statuses_updated.emit(target.remote_rows(failing=2))
    assert poller.slow
    if reset == "logged_in":
        target.logged_in.emit(object())
    else:
        target.config_changed.emit(target.config)
    assert not poller.slow
    target.refreshes.clear()
    clock.now += 1
    poller._on_tick()
    assert target.refreshes == [(True, True)]


# --- with the real controller -------------------------------------------------------------------


def test_drives_the_real_controller(make_controller: Any, qtbot: Any, clock: FakeClock) -> None:
    record = record_model()
    h = make_controller(records=[record], account="octo")
    h.manager.rows = [status_row(record, remote_error="网络错误")]
    poller = StatusPoller(h.controller, clock=clock)
    assert poller.parent() is h.controller
    with qtbot.waitSignal(h.controller.statuses_updated, timeout=WAIT_MS):
        poller.start()
    assert h.manager.calls[-1] == ("statuses", {"remote": True})
    assert poller.slow
    h.manager.rows = [status_row(record, remote=RemoteStatus.UNKNOWN)]
    with qtbot.waitSignal(h.controller.statuses_updated, timeout=WAIT_MS):
        poller._on_tick()
    assert h.manager.calls[-1] == ("statuses", {"remote": False})
    # The real controller's signals reach the poller.
    with qtbot.waitSignal(h.controller.external_stopped, timeout=WAIT_MS):
        h.controller.kill_external(record.id)
    assert poller._kick_timer.isActive()
    with qtbot.waitSignal(h.controller.config_changed, timeout=WAIT_MS):
        h.controller.save_settings(AppSettings(poll_interval_s=60))
    assert poller.interval_s == 60 and not poller.slow
    poller.stop()


def test_quiet_polls_never_emit_failed(make_controller: Any, qtbot: Any, clock: FakeClock) -> None:
    h = make_controller(records=[record_model()])
    h.manager.errors["statuses"] = StoreError("配置文件不可读")
    failed: list[Any] = []
    h.controller.failed.connect(lambda *args: failed.append(args))
    poller = StatusPoller(h.controller, clock=clock)
    poller.start()
    qtbot.waitUntil(lambda: ("statuses", {"remote": False}) in h.manager.calls, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: not h.controller.refreshing, timeout=WAIT_MS)
    assert failed == []
    poller.stop()
