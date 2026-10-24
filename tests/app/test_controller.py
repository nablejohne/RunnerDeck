"""Tests for runnerdeck.app.controller.RunnerController (docs/m2-design.md §4, m3-design §4.1).

A fake manager / client / process / autostart / diag (tests/app/conftest.py) replace network,
config.cmd, run.cmd and the registry; the ConfigStore and the keyring are real but isolated.
Every operation is checked for its success signal, its `failed(op, ...)` on error, the busy
bookkeeping, and that signals fed from worker or reader threads arrive on the main thread. The
controller's clock is a FakeClock, so the time-dependent merge rules are driven explicitly.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from PyQt5.QtCore import QCoreApplication, QThread, QUrl

from runnerdeck.app.controller import (
    AUTOSTART_SKIPPED,
    AUTOSTART_UNSUPPORTED,
    LOGIN_HINT,
    REMERGE_INTERVAL_MS,
    STEP_STOP,
    CoreAutostart,
    CoreDiag,
    RunnerController,
)
from runnerdeck.core import account, secrets
from runnerdeck.core.errors import (
    AuthError,
    InstallError,
    InvalidInputError,
    ProcessError,
    RunnerDeckError,
    StoreError,
)
from runnerdeck.core.manager import POWERSHELL_POLICY_ENV, RunnerManager, Step
from runnerdeck.core.models import (
    AppSettings,
    DiagLog,
    Discovery,
    DisplayState,
    LocalState,
    RemoteStatus,
    RunnerRecord,
    StrayRunner,
)
from runnerdeck.core.process import ExternalStop, Launch, StopMethod, StopResult
from runnerdeck.core.status import (
    DETAIL_CONNECTING,
    DETAIL_OFFLINE_LAG,
    DETAIL_REMOTE_UNKNOWN,
    DETAIL_STALLED,
)
from runnerdeck.core.store import ConfigStore

from .conftest import (
    FAKE_PAT,
    NOW,
    OTHER_PAT,
    OWNER,
    REPO,
    WAIT_MS,
    FakeProcess,
    Harness,
    plan_model,
    record_model,
    status_row,
)

KEYRING_KEY = (secrets.KEYRING_SERVICE, "octo")


def _on_main_thread() -> bool:
    app = QCoreApplication.instance()
    assert app is not None
    return QThread.currentThread() is app.thread()


class Spy:
    """Collects (args, on_main_thread) for one signal."""

    def __init__(self, signal: Any) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.threads_ok: list[bool] = []
        signal.connect(self._slot)

    def _slot(self, *args: Any) -> None:
        self.calls.append(args)
        self.threads_ok.append(_on_main_thread())

    def wait(self, qtbot: Any, count: int = 1) -> None:
        qtbot.waitUntil(lambda: len(self.calls) >= count, timeout=WAIT_MS)

    @property
    def last(self) -> tuple[Any, ...]:
        return self.calls[-1]


def _wait_failed(qtbot: Any, harness: Harness, action: Any) -> tuple[str, str, Any]:
    with qtbot.waitSignal(harness.controller.failed, timeout=WAIT_MS) as blocker:
        action()
    op, message, exc = blocker.args
    assert _on_main_thread()
    return op, message, exc


def _wait_idle(qtbot: Any, harness: Harness, record_id: str) -> None:
    qtbot.waitUntil(lambda: not harness.controller.is_busy(record_id), timeout=WAIT_MS)


# --- construction / read-only ------------------------------------------------------------------


def test_defaults_and_read_only_state(make_controller: Any) -> None:
    record = record_model()
    h = make_controller(records=[record])
    c = h.controller
    assert c.launch is Launch.HELPER
    assert c.config.runners[0].id == record.id
    assert c.records == [record]
    assert c.records is not c.config.runners  # a copy
    assert not c.is_logged_in
    assert not c.refreshing
    assert c.local_state(record.id) == (LocalState.STOPPED, None)
    assert not c.is_running(record.id)
    assert not c.is_busy(record.id)
    assert c.output(record.id) == []
    assert c.running_ids() == []
    assert c.display(record.id).state is DisplayState.STOPPED
    assert h.manager.store is h.store
    assert h.manager.client_factory == c._client_factory


def test_logged_in_follows_account_or_environment(make_controller: Any) -> None:
    assert make_controller(account="octo").controller.is_logged_in
    assert make_controller(environ={account.ENV_PAT: FAKE_PAT}).controller.is_logged_in
    assert not make_controller(environ={account.ENV_PAT: "  "}).controller.is_logged_in


def test_unreadable_config_raises_in_the_constructor(isolated_home: Path, pool: Any) -> None:
    store = ConfigStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreError):
        RunnerController(store=store, environ={}, pool=pool)


def test_default_factories_build_a_real_manager(isolated_home: Path, pool: Any, qtbot: Any) -> None:
    """No fakes: the real RunnerManager with the controller's client factory, not logged in."""
    c = RunnerController(environ={}, pool=pool)
    assert isinstance(c.manager, RunnerManager)
    assert c.config.runners == []
    with qtbot.waitSignal(c.failed, timeout=WAIT_MS) as blocker:
        c.load_repos()
    op, message, exc = blocker.args
    assert op == "repos"
    assert isinstance(exc, account.NotLoggedInError)
    assert LOGIN_HINT in message


# --- account ----------------------------------------------------------------------------------


def test_login_validates_then_asks_github_then_stores_the_pat(
    make_controller: Any, qtbot: Any
) -> None:
    h = make_controller()
    config = h.store.load()
    config.settings.proxy = "http://127.0.0.1:7890"
    h.store.save(config)
    h.controller._config = h.store.load()
    changed = Spy(h.controller.config_changed)
    with qtbot.waitSignal(h.controller.logged_in, timeout=WAIT_MS) as blocker:
        h.controller.login("  " + FAKE_PAT + "\n")
    (user,) = blocker.args
    assert user.login == "octo"
    assert h.clients.clients[0].pat == FAKE_PAT
    assert h.clients.clients[0].proxy == "http://127.0.0.1:7890"
    assert h.clients.clients[0].calls == ["get_user"]
    assert h.clients.clients[0].closed
    assert h.keyring.passwords[KEYRING_KEY] == FAKE_PAT
    assert h.controller.config.account == "octo"
    assert h.controller.is_logged_in
    assert h.store.load().account == "octo"
    assert changed.calls and changed.calls[0][0].account == "octo"
    assert all(changed.threads_ok)


def test_login_rejects_a_bad_pat_before_touching_the_network(
    make_controller: Any, qtbot: Any
) -> None:
    h = make_controller()
    bad = "my-token-name"  # the token's *name* pasted instead of its value (spike §2)
    op, message, exc = _wait_failed(qtbot, h, lambda: h.controller.login(bad))
    assert op == "login"
    assert isinstance(exc, InvalidInputError)
    assert "PAT 格式不对" in message
    assert bad not in message
    assert h.clients.clients == []  # no client was ever built
    assert h.keyring.passwords == {}
    assert h.controller.config.account is None


def test_login_failure_message_never_contains_the_pat(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    h.clients.errors["get_user"] = AuthError(f"GitHub 拒绝了 {FAKE_PAT}", status=401)
    op, message, exc = _wait_failed(qtbot, h, lambda: h.controller.login(FAKE_PAT))
    assert op == "login"
    assert isinstance(exc, AuthError)
    assert FAKE_PAT not in message
    assert message == "GitHub 拒绝了 ***"
    assert h.keyring.passwords == {}


def test_login_with_another_account_deletes_the_previous_pat(
    make_controller: Any, qtbot: Any
) -> None:
    h = make_controller(account="old")
    h.keyring.passwords[(secrets.KEYRING_SERVICE, "old")] = OTHER_PAT
    with qtbot.waitSignal(h.controller.logged_in, timeout=WAIT_MS):
        h.controller.login(FAKE_PAT)
    assert (secrets.KEYRING_SERVICE, "old") not in h.keyring.passwords
    assert h.keyring.passwords[KEYRING_KEY] == FAKE_PAT
    assert h.controller.config.account == "octo"


def test_logout_deletes_the_pat_and_clears_the_account(make_controller: Any, qtbot: Any) -> None:
    h = make_controller(account="octo")
    h.keyring.passwords[KEYRING_KEY] = FAKE_PAT
    changed = Spy(h.controller.config_changed)
    with qtbot.waitSignal(h.controller.logged_out, timeout=WAIT_MS):
        h.controller.logout()
    assert h.keyring.passwords == {}
    assert h.controller.config.account is None
    assert not h.controller.is_logged_in
    assert changed.calls


def test_logout_when_not_logged_in_is_a_no_op(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    with qtbot.waitSignal(h.controller.logged_out, timeout=WAIT_MS):
        h.controller.logout()
    assert h.store.load().account is None


def test_load_account_reports_user_and_source(make_controller: Any, qtbot: Any) -> None:
    h = make_controller(environ={account.ENV_PAT: FAKE_PAT})
    with qtbot.waitSignal(h.controller.account_loaded, timeout=WAIT_MS) as blocker:
        h.controller.load_account()
    user, source = blocker.args
    assert user.login == "octo"
    assert source == account.SOURCE_ENV
    assert h.clients.clients[0].pat == FAKE_PAT

    h2 = make_controller(account="octo")
    h2.keyring.passwords[KEYRING_KEY] = FAKE_PAT
    with qtbot.waitSignal(h2.controller.account_loaded, timeout=WAIT_MS) as blocker:
        h2.controller.load_account()
    assert blocker.args[1] == account.SOURCE_KEYRING


def test_load_account_not_logged_in_fails(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    op, message, exc = _wait_failed(qtbot, h, h.controller.load_account)
    assert op == "account"
    assert isinstance(exc, account.NotLoggedInError)
    assert LOGIN_HINT in message


def test_load_repos(make_controller: Any, qtbot: Any) -> None:
    h = make_controller(account="octo")
    h.keyring.passwords[KEYRING_KEY] = FAKE_PAT
    with qtbot.waitSignal(h.controller.repos_loaded, timeout=WAIT_MS) as blocker:
        h.controller.load_repos()
    (repos,) = blocker.args
    assert [r.full_name for r in repos] == [f"{OWNER}/demo", f"{OWNER}/other"]
    assert h.clients.clients[0].closed


def test_load_repos_failure(make_controller: Any, qtbot: Any) -> None:
    h = make_controller(environ={account.ENV_PAT: FAKE_PAT})
    h.clients.errors["list_repos"] = AuthError("401", status=401)
    op, message, exc = _wait_failed(qtbot, h, h.controller.load_repos)
    assert (op, message) == ("repos", "401")


# --- plan / add ---------------------------------------------------------------------------------


def test_plan_add_passes_the_powershell_policy(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    with qtbot.waitSignal(h.controller.plan_ready, timeout=WAIT_MS) as blocker:
        h.controller.plan_add(OWNER, REPO, name="box", labels=["gpu"])
    (plan,) = blocker.args
    assert plan.name == "box"
    assert plan.runner_env == tuple(POWERSHELL_POLICY_ENV.items())
    name, kw = h.manager.calls[-1]
    assert name == "plan_add"
    assert kw == {
        "owner": OWNER,
        "repo": REPO,
        "name": "box",
        "labels": ["gpu"],
        "runner_env": POWERSHELL_POLICY_ENV,
    }
    assert h.manager.threads[-1] is not threading.main_thread()

    with qtbot.waitSignal(h.controller.plan_ready, timeout=WAIT_MS) as blocker:
        h.controller.plan_add(OWNER, REPO, ps_policy=False)
    assert blocker.args[0].runner_env == ()
    assert h.manager.calls[-1][1]["runner_env"] is None


def test_plan_add_failure(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    h.manager.errors["plan_add"] = InvalidInputError("本机已有同名 runner")
    op, message, _ = _wait_failed(qtbot, h, lambda: h.controller.plan_add(OWNER, REPO))
    assert (op, message) == ("plan_add", "本机已有同名 runner")


def test_add_runner_reports_progress_then_the_record(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    progress = Spy(h.controller.add_progress)
    changed = Spy(h.controller.config_changed)
    plan = plan_model()
    with qtbot.waitSignal(h.controller.runner_added, timeout=WAIT_MS) as blocker:
        h.controller.add_runner(plan)
    (record,) = blocker.args
    assert record.name == plan.name
    assert [s for s, _ in progress.calls] == [Step.RELEASE, Step.DOWNLOAD, Step.DONE]
    assert all(type(s) is str for s, _ in progress.calls)
    assert all(progress.threads_ok)
    assert [r.id for r in h.controller.records] == [record.id]
    assert changed.calls and changed.calls[-1][0].runners[0].id == record.id
    assert h.manager.calls[-1] == ("add_runner", {"plan": plan, "auto_start": False})
    assert not h.manager.processes  # start_after=False


def test_add_runner_start_after_starts_the_new_runner(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    state = Spy(h.controller.runner_state)
    with qtbot.waitSignal(h.controller.runner_added, timeout=WAIT_MS) as blocker:
        h.controller.add_runner(plan_model(), start_after=True)
    record = blocker.args[0]
    qtbot.waitUntil(lambda: h.controller.is_running(record.id), timeout=WAIT_MS)
    _wait_idle(qtbot, h, record.id)
    proc = h.process(record.id)
    assert proc.calls == ["start"]
    assert proc.launch is Launch.HELPER
    state.wait(qtbot)
    assert state.calls[0] == (record.id, LocalState.STARTING, None)


def test_add_runner_failure_carries_the_install_output(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    h.manager.errors["add_runner"] = InstallError(
        "config.cmd 失败", output="Http response code: 404"
    )
    op, message, exc = _wait_failed(qtbot, h, lambda: h.controller.add_runner(plan_model()))
    assert (op, message) == ("add", "config.cmd 失败")
    assert isinstance(exc, InstallError)
    assert exc.output == "Http response code: 404"
    assert h.controller.records == []


def test_failed_reaches_a_two_argument_slot(make_controller: Any, qtbot: Any) -> None:
    """The UI may connect `def on_failed(op, message)`; PyQt drops the trailing exception."""
    h = make_controller()
    seen: list[tuple[str, str]] = []
    h.controller.failed.connect(lambda op, message: seen.append((op, message)))
    h.manager.errors["plan_add"] = RunnerDeckError("x")
    h.controller.plan_add(OWNER, REPO)
    qtbot.waitUntil(lambda: seen == [("plan_add", "x")], timeout=WAIT_MS)


# --- start / stop ----------------------------------------------------------------------------


def test_start_creates_a_helper_process_and_runs_it_on_a_worker(
    make_controller: Any, qtbot: Any
) -> None:
    record = record_model()
    h = make_controller(records=[record])
    busy = Spy(h.controller.busy_changed)
    state = Spy(h.controller.runner_state)
    h.controller.start(record.id)
    assert h.controller.is_busy(record.id)
    assert busy.calls == [(record.id, True)]
    name, kw = h.manager.calls[-1]
    assert name == "process_for"
    assert kw["record"] == record
    assert kw["launch"] is Launch.HELPER
    assert h.manager.threads[-1] is threading.main_thread()  # process_for: a config read only

    busy.wait(qtbot, 2)
    assert busy.calls[1] == (record.id, False)
    assert all(busy.threads_ok)
    proc = h.process(record.id)
    assert proc.calls == ["start"]
    assert proc.threads[0] is not threading.main_thread()
    assert h.controller.is_running(record.id)
    assert h.controller.running_ids() == [record.id]
    assert h.controller.local_state(record.id) == (LocalState.STARTING, None)
    state.wait(qtbot)
    assert state.calls == [(record.id, LocalState.STARTING, None)]
    assert state.threads_ok == [True]


def test_start_uses_the_configured_launch_mode(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = make_controller(records=[record], launch=Launch.CONSOLE)
    h.controller.start(record.id)
    _wait_idle(qtbot, h, record.id)
    assert h.process(record.id).launch is Launch.CONSOLE


def test_runner_state_is_right_when_the_first_state_arrives(
    make_controller: Any, qtbot: Any
) -> None:
    """The process is registered before start(), so a slot can already ask is_running()."""
    record = record_model()
    h = make_controller(records=[record])
    seen: list[bool] = []
    h.controller.runner_state.connect(lambda rid, *_: seen.append(h.controller.is_running(rid)))
    h.controller.start(record.id)
    qtbot.waitUntil(lambda: seen == [True], timeout=WAIT_MS)


def test_second_start_while_busy_is_refused(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = make_controller(records=[record])
    gate = threading.Event()
    # Hold the first start inside start() by making process_for hand out a blocked process.
    original = h.manager.process_for

    def blocked_process_for(rec: RunnerRecord, **kw: Any) -> FakeProcess:
        proc = original(rec, **kw)
        proc.block_start = gate
        return proc

    h.manager.process_for = blocked_process_for  # type: ignore[method-assign]
    h.controller.start(record.id)
    op, message, exc = _wait_failed(qtbot, h, lambda: h.controller.start(record.id))
    assert op == "start"
    assert "正在执行其他操作" in message
    assert exc is None
    assert len(h.manager.processes) == 1
    gate.set()
    _wait_idle(qtbot, h, record.id)
    assert h.process(record.id).calls == ["start"]


def test_start_of_a_running_runner_is_refused(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = make_controller(records=[record])
    h.controller.start(record.id)
    _wait_idle(qtbot, h, record.id)
    op, message, _ = _wait_failed(qtbot, h, lambda: h.controller.start(record.id))
    assert (op, message) == ("start", f"runner “{record.name}” 已在运行")
    assert not h.controller.is_busy(record.id)


def test_start_unknown_runner(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    op, message, _ = _wait_failed(qtbot, h, lambda: h.controller.start("nope"))
    assert op == "start"
    assert "找不到" in message


def test_start_when_process_for_fails(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = make_controller(records=[record])
    h.manager.errors["process_for"] = StoreError("配置文件不是有效的 UTF-8 JSON")
    busy = Spy(h.controller.busy_changed)
    op, message, exc = _wait_failed(qtbot, h, lambda: h.controller.start(record.id))
    assert (op, message) == ("start", "配置文件不是有效的 UTF-8 JSON")
    assert isinstance(exc, StoreError)
    assert busy.calls == [(record.id, True), (record.id, False)]
    assert not h.controller.is_running(record.id)


def test_start_failure_clears_busy(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = make_controller(records=[record])
    original = h.manager.process_for

    def failing_process_for(rec: RunnerRecord, **kw: Any) -> FakeProcess:
        proc = original(rec, **kw)
        proc.start_error = ProcessError("找不到 run.cmd")
        return proc

    h.manager.process_for = failing_process_for  # type: ignore[method-assign]
    op, message, exc = _wait_failed(qtbot, h, lambda: h.controller.start(record.id))
    assert (op, message) == ("start", "找不到 run.cmd")
    assert isinstance(exc, ProcessError)
    _wait_idle(qtbot, h, record.id)
    assert not h.controller.is_running(record.id)
    assert h.controller.local_state(record.id) == (LocalState.STOPPED, None)


def _started(make_controller: Any, qtbot: Any, *records: RunnerRecord) -> Harness:
    h = make_controller(records=list(records))
    for record in records:
        h.controller.start(record.id)
    for record in records:
        _wait_idle(qtbot, h, record.id)
        assert h.controller.is_running(record.id)
    return h


def test_stop_reports_result_and_final_state(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = _started(make_controller, qtbot, record)
    state = Spy(h.controller.runner_state)
    busy = Spy(h.controller.busy_changed)
    proc = h.process(record.id)
    proc.stop_result = StopResult(False, StopMethod.KILL, 1, [10])
    with qtbot.waitSignal(h.controller.runner_stopped, timeout=WAIT_MS) as blocker:
        h.controller.stop(record.id)
    rid, result = blocker.args
    assert rid == record.id
    assert result == proc.stop_result
    assert proc.calls == ["start", "stop"]
    assert proc.stop_timeouts == [None]  # RunnerProcess picks idle / busy timeout
    assert proc.threads[-1] is not threading.main_thread()
    assert busy.calls == [(record.id, True), (record.id, False)]
    assert not h.controller.is_running(record.id)
    assert h.controller.running_ids() == []
    state.wait(qtbot, 2)
    assert [s for _, s, _ in state.calls] == [LocalState.STOPPING, LocalState.STOPPED]


def test_stop_when_not_running(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = make_controller(records=[record])
    op, message, _ = _wait_failed(qtbot, h, lambda: h.controller.stop(record.id))
    assert (op, message) == ("stop", f"runner “{record.name}” 没有在运行")
    op, _, _ = _wait_failed(qtbot, h, lambda: h.controller.stop("nope"))
    assert op == "stop"


def test_stop_failure_clears_busy(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = _started(make_controller, qtbot, record)
    h.process(record.id).stop_error = ProcessError("无法结束 runner 进程（PID 7）")
    op, message, exc = _wait_failed(qtbot, h, lambda: h.controller.stop(record.id))
    assert (op, message) == ("stop", "无法结束 runner 进程（PID 7）")
    _wait_idle(qtbot, h, record.id)


def test_start_stop_remove_are_mutually_exclusive_per_runner(
    make_controller: Any, qtbot: Any
) -> None:
    record = record_model()
    h = _started(make_controller, qtbot, record)
    gate = threading.Event()
    h.process(record.id).block_stop = gate
    h.controller.stop(record.id)
    assert h.controller.is_busy(record.id)
    op, _, _ = _wait_failed(qtbot, h, lambda: h.controller.remove(record.id))
    assert op == "remove"
    op, _, _ = _wait_failed(qtbot, h, lambda: h.controller.stop(record.id))
    assert op == "stop"
    gate.set()
    _wait_idle(qtbot, h, record.id)
    assert "remove_runner" not in [name for name, _ in h.manager.calls]


def test_start_all_and_stop_all(make_controller: Any, qtbot: Any) -> None:
    a, b = record_model("a", repo="one"), record_model("b", repo="two")
    h = make_controller(records=[a, b])
    h.controller.start_all()
    for record in (a, b):
        _wait_idle(qtbot, h, record.id)
    assert sorted(h.controller.running_ids()) == sorted([a.id, b.id])
    h.controller.start_all()  # all running: nothing submitted, no failure
    assert not h.controller.is_busy(a.id)
    stopped = Spy(h.controller.runner_stopped)
    h.controller.stop_all()
    stopped.wait(qtbot, 2)
    assert h.controller.running_ids() == []
    h.controller.stop_all()  # nothing running: no-op
    assert h.process(a.id).calls == ["start", "stop"]


def test_stop_all_skips_busy_runners(make_controller: Any, qtbot: Any) -> None:
    a = record_model("a")
    h = _started(make_controller, qtbot, a)
    gate = threading.Event()
    h.process(a.id).block_stop = gate
    h.controller.stop(a.id)
    failed = Spy(h.controller.failed)
    h.controller.stop_all()
    assert failed.calls == []
    gate.set()
    _wait_idle(qtbot, h, a.id)


# --- process callbacks -> main thread ------------------------------------------------------------


def test_process_callbacks_are_forwarded_on_the_main_thread(
    make_controller: Any, qtbot: Any
) -> None:
    record = record_model()
    h = _started(make_controller, qtbot, record)
    proc = h.process(record.id)
    state = Spy(h.controller.runner_state)
    line = Spy(h.controller.runner_line)
    job = Spy(h.controller.runner_job_finished)

    proc.drive_state(LocalState.IDLE)  # from a non-main thread
    proc.push_line("Listening for Jobs")
    proc.drive_state(LocalState.BUSY, "build")
    proc.finish_job("build", "Succeeded")

    state.wait(qtbot, 2)
    line.wait(qtbot)
    job.wait(qtbot)
    assert state.calls == [
        (record.id, LocalState.IDLE, None),
        (record.id, LocalState.BUSY, "build"),
    ]
    assert line.calls == [(record.id, "Listening for Jobs")]
    assert job.calls == [(record.id, "build", "Succeeded")]
    assert all(state.threads_ok) and all(line.threads_ok) and all(job.threads_ok)
    assert h.controller.local_state(record.id) == (LocalState.BUSY, "build")
    assert h.controller.output(record.id) == ["Listening for Jobs"]


def test_callbacks_of_a_replaced_process_are_dropped(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = _started(make_controller, qtbot, record)
    old = h.process(record.id)
    with qtbot.waitSignal(h.controller.runner_stopped, timeout=WAIT_MS):
        h.controller.stop(record.id)
    h.controller.start(record.id)
    _wait_idle(qtbot, h, record.id)
    new = h.process(record.id)
    assert new is not old
    state = Spy(h.controller.runner_state)
    old.drive_state(LocalState.ERROR)  # late notification of the previous run
    new.drive_state(LocalState.IDLE)
    state.wait(qtbot)
    assert state.calls == [(record.id, LocalState.IDLE, None)]


# --- remove -------------------------------------------------------------------------------------


def test_remove_stops_a_running_runner_first(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = _started(make_controller, qtbot, record)
    proc = h.process(record.id)
    progress = Spy(h.controller.remove_progress)
    changed = Spy(h.controller.config_changed)
    busy = Spy(h.controller.busy_changed)
    with qtbot.waitSignal(h.controller.runner_removed, timeout=WAIT_MS) as blocker:
        h.controller.remove(record.id, keep_work=True)
    rid, result = blocker.args
    assert rid == record.id
    assert result.record.id == record.id
    assert proc.calls == ["start", "stop"]
    assert h.manager.calls[-1] == (
        "remove_runner",
        {"key": record.id, "keep_work": True, "local_only": False},
    )
    assert [s for s, _ in progress.calls] == [STEP_STOP, Step.CHECK, Step.DONE]
    assert h.controller.records == []
    assert changed.calls
    assert busy.calls == [(record.id, True), (record.id, False)]
    assert not h.controller.is_running(record.id)
    assert h.controller.output(record.id) == []


def test_remove_a_stopped_runner_does_not_stop(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = make_controller(records=[record])
    progress = Spy(h.controller.remove_progress)
    with qtbot.waitSignal(h.controller.runner_removed, timeout=WAIT_MS):
        h.controller.remove(record.id)
    assert [s for s, _ in progress.calls] == [Step.CHECK, Step.DONE]
    assert h.manager.calls[-1][1]["keep_work"] is False


def test_remove_failure_keeps_the_record_and_clears_busy(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = make_controller(records=[record])
    h.manager.errors["remove_runner"] = ProcessError("请先停止再移除")
    op, message, exc = _wait_failed(qtbot, h, lambda: h.controller.remove(record.id))
    assert (op, message) == ("remove", "请先停止再移除")
    assert isinstance(exc, ProcessError)
    _wait_idle(qtbot, h, record.id)
    assert [r.id for r in h.controller.records] == [record.id]


def test_remove_unknown_runner(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    op, _, _ = _wait_failed(qtbot, h, lambda: h.controller.remove("nope"))
    assert op == "remove"


# --- status / settings / open_dir ----------------------------------------------------------------


def test_refresh_statuses_caches_the_display(make_controller: Any, qtbot: Any) -> None:
    a, b = record_model("a", repo="one"), record_model("b", repo="two")
    h = _started(make_controller, qtbot, a)
    h.controller.config.upsert(b)
    h.manager.rows = [
        status_row(a, LocalState.IDLE, remote=RemoteStatus.ONLINE),
        status_row(b, remote_error="网络错误"),
    ]
    refreshing = Spy(h.controller.refreshing_changed)
    with qtbot.waitSignal(h.controller.statuses_updated, timeout=WAIT_MS) as blocker:
        h.controller.refresh_statuses()
        assert h.controller.refreshing
        h.controller.refresh_statuses()  # ignored while one is in flight
    (rows,) = blocker.args
    assert [r.record.id for r in rows] == [a.id, b.id]
    assert len(h.manager.statuses_seen) == 1
    assert h.manager.statuses_seen[0] == {a.id: h.process(a.id)}
    assert h.manager.calls[-1] == ("statuses", {"remote": True})
    assert not h.controller.refreshing
    assert refreshing.calls == [(True,), (False,)]
    # Our process says STARTING (live), GitHub says online -> idle.
    assert h.controller.display(a.id) == rows[0].display
    assert rows[0].display.state is DisplayState.IDLE
    assert h.controller.display(b.id).state is DisplayState.STOPPED
    assert rows[1].remote_error == "网络错误"
    # A local state change is merged with the cached GitHub snapshot of that runner.
    h.process(a.id).drive_state(LocalState.BUSY, "build")
    qtbot.waitUntil(lambda: h.controller.display(a.id).state is DisplayState.BUSY, timeout=WAIT_MS)
    assert h.controller.display(a.id).label == "运行中：build"
    assert h.controller.display(b.id) == rows[1].display


def test_refresh_statuses_failure(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    h.manager.errors["statuses"] = StoreError("配置文件不可读")
    op, message, _ = _wait_failed(qtbot, h, h.controller.refresh_statuses)
    assert (op, message) == ("status", "配置文件不可读")
    assert not h.controller.refreshing


def test_display_without_a_refresh_merges_local_state(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = _started(make_controller, qtbot, record)
    assert h.controller.display(record.id).state is DisplayState.STARTING
    h.process(record.id).drive_state(LocalState.IDLE)
    qtbot.waitUntil(
        lambda: h.controller.display(record.id).state is DisplayState.IDLE, timeout=WAIT_MS
    )
    with qtbot.waitSignal(h.controller.runner_stopped, timeout=WAIT_MS):
        h.controller.stop(record.id)
    assert h.controller.display(record.id).state is DisplayState.STOPPED
    assert h.controller.display("unknown").state is DisplayState.STOPPED


def test_save_settings(make_controller: Any, qtbot: Any) -> None:
    h = make_controller(account="octo")
    settings = AppSettings(
        runners_root="/tmp/r", proxy="http://127.0.0.1:1", default_labels=["gpu"]
    )
    with qtbot.waitSignal(h.controller.config_changed, timeout=WAIT_MS) as blocker:
        h.controller.save_settings(settings)
    (config,) = blocker.args
    assert config.settings == settings
    assert config.account == "octo"  # untouched
    assert h.store.load().settings.proxy == "http://127.0.0.1:1"
    assert h.controller.config.settings.default_labels == ["gpu"]


def test_save_settings_failure(
    make_controller: Any, qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_controller()

    def broken(config: Any) -> None:
        raise StoreError("无法写入配置文件")

    monkeypatch.setattr(h.store, "save", broken)
    op, message, _ = _wait_failed(qtbot, h, lambda: h.controller.save_settings(AppSettings()))
    assert (op, message) == ("settings", "无法写入配置文件")


def test_reload_failure_after_a_write_is_reported(
    make_controller: Any, qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_controller()
    real_load = h.store.load
    calls = {"n": 0}

    def flaky_load() -> Any:
        calls["n"] += 1
        if calls["n"] == 2:  # the worker's load succeeds, the controller's reload fails
            raise StoreError("磁盘错误")
        return real_load()

    monkeypatch.setattr(h.store, "load", flaky_load)
    op, message, _ = _wait_failed(qtbot, h, lambda: h.controller.save_settings(AppSettings()))
    assert op == "settings"
    assert "磁盘错误" in message


def test_open_dir(make_controller: Any, qtbot: Any, tmp_path: Path) -> None:
    present = record_model("here", path=str(tmp_path / "here"))
    missing = record_model("gone", repo="other", path=str(tmp_path / "gone"))
    (tmp_path / "here").mkdir()
    h = make_controller(records=[present, missing])
    failed = Spy(h.controller.failed)
    h.controller.open_dir(present.id)
    assert h.opened == [QUrl.fromLocalFile(str(tmp_path / "here"))]
    assert failed.calls == []
    h.controller.open_dir("nope")
    assert h.opened[1:] == [] and failed.calls == []
    h.controller.open_dir(missing.id)
    assert failed.calls[-1][:2] == ("open_dir", f"目录不存在：{missing.path}")


def test_open_dir_reports_a_refused_open(make_controller: Any, tmp_path: Path) -> None:
    record = record_model("here", path=str(tmp_path))
    h = make_controller(records=[record], open_ok=False)
    failed = Spy(h.controller.failed)
    h.controller.open_dir(record.id)
    assert failed.calls[-1][:2] == ("open_dir", f"无法打开目录：{tmp_path}")


# --- shutdown -----------------------------------------------------------------------------------


def test_shutdown_stops_all_runners_in_parallel(make_controller: Any, qtbot: Any) -> None:
    a, b = record_model("a", repo="one"), record_model("b", repo="two")
    h = _started(make_controller, qtbot, a, b)
    barrier = threading.Barrier(2, timeout=5)  # both stops must be inside stop() at once
    for record in (a, b):
        h.process(record.id).stop_hook = lambda _p: barrier.wait()
    assert h.controller.shutdown(timeout=3.0) == []
    for record in (a, b):
        proc = h.process(record.id)
        assert proc.calls == ["start", "stop"]
        assert proc.stop_timeouts == [3.0]
        assert proc.threads[-1] is not threading.main_thread()
    assert h.controller.running_ids() == []
    assert not h.controller.is_busy(a.id)


def test_shutdown_names_runners_that_did_not_stop(make_controller: Any, qtbot: Any) -> None:
    a, b, c = (
        record_model("a", repo="one"),
        record_model("b", repo="two"),
        record_model("c", repo="x"),
    )
    h = _started(make_controller, qtbot, a, b, c)
    h.process(a.id).stop_error = ProcessError("无法结束 runner 进程")
    survivor = h.process(b.id)

    original_stop = survivor.stop

    def stop_but_stay(timeout: float | None = None) -> StopResult:
        result = original_stop(timeout)
        survivor._running = True
        return result

    survivor.stop = stop_but_stay  # type: ignore[method-assign]
    assert h.controller.shutdown() == ["a", "b"]
    assert h.process(c.id).calls == ["start", "stop"]
    assert h.process(c.id).stop_timeouts == [None]


def test_shutdown_without_running_runners(make_controller: Any) -> None:
    h = make_controller(records=[record_model()])
    assert h.controller.shutdown() == []


# --- M3: live merge (docs/m3-design.md §4.1) ----------------------------------------------------


def _refresh(qtbot: Any, h: Harness, rows: list[Any], **kw: Any) -> list[Any]:
    """Run one refresh_statuses(**kw) answering `rows`; the rows statuses_updated carried."""
    h.manager.rows = rows
    with qtbot.waitSignal(h.controller.statuses_updated, timeout=WAIT_MS) as blocker:
        h.controller.refresh_statuses(**kw)
    return blocker.args[0]


def test_state_change_during_a_refresh_is_not_overwritten(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = _started(make_controller, qtbot, record)
    proc = h.process(record.id)
    proc.drive_state(LocalState.IDLE)
    qtbot.waitUntil(lambda: h.controller.display(record.id).state is DisplayState.IDLE)
    gate = threading.Event()
    h.manager.block_statuses = gate
    # The manager scanned while the runner was idle, GitHub said online and not busy.
    h.manager.rows = [
        status_row(record, LocalState.IDLE, remote=RemoteStatus.ONLINE, fetched_at=NOW)
    ]
    changed = Spy(h.controller.display_changed)
    with qtbot.waitSignal(h.controller.statuses_updated, timeout=WAIT_MS) as blocker:
        h.controller.refresh_statuses()
        proc.drive_state(LocalState.BUSY, "build")  # after the refresh was submitted
        qtbot.waitUntil(lambda: h.controller.local_state(record.id)[0] is LocalState.BUSY)
        gate.set()
    (rows,) = blocker.args
    assert rows[0].display.state is DisplayState.BUSY
    assert rows[0].display.label == "运行中：build"
    assert rows[0].local.state is LocalState.IDLE  # only display is replaced
    assert h.controller.display(record.id) == rows[0].display
    assert changed.calls[-1] == (record.id, rows[0].display)
    assert all(changed.threads_ok)


def test_remote_snapshot_older_than_the_local_state_shows_the_local_state(
    make_controller: Any, qtbot: Any
) -> None:
    record = record_model()
    h = _started(make_controller, qtbot, record)
    proc = h.process(record.id)
    h.clock.advance(10)
    _refresh(qtbot, h, [status_row(record, remote=RemoteStatus.OFFLINE, fetched_at=h.clock.now)])
    assert h.controller.display(record.id).state is DisplayState.CONNECTING
    proc.state_since = h.clock.now + timedelta(seconds=1)  # went idle after that fetch
    proc.drive_state(LocalState.IDLE)
    qtbot.waitUntil(lambda: h.controller.display(record.id).state is DisplayState.IDLE)
    assert h.controller.display(record.id).detail is None


def test_display_changed_only_on_change(make_controller: Any, qtbot: Any) -> None:
    a, b = record_model("a", repo="one"), record_model("b", repo="two")
    h = make_controller(records=[a, b])
    changed = Spy(h.controller.display_changed)
    rows = [status_row(a, remote=RemoteStatus.OFFLINE), status_row(b, remote=RemoteStatus.OFFLINE)]
    _refresh(qtbot, h, rows)
    # Both were STOPPED + "remote unknown" before; now plain STOPPED.
    assert sorted(rid for rid, _ in changed.calls) == sorted([a.id, b.id])
    assert all(d.detail is None for _, d in changed.calls)
    _refresh(qtbot, h, rows)
    assert len(changed.calls) == 2  # nothing changed the second time
    h.controller._on_remerge_timer()
    assert len(changed.calls) == 2
    assert h.controller.all_displays() == {a.id: changed.calls[0][1], b.id: changed.calls[1][1]}


def test_runner_state_is_followed_by_display_changed(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = _started(make_controller, qtbot, record)
    order: list[str] = []
    h.controller.runner_state.connect(lambda *_: order.append("state"))
    h.controller.display_changed.connect(lambda rid, d: order.append(f"display:{d.state}"))
    h.process(record.id).drive_state(LocalState.IDLE)
    qtbot.waitUntil(lambda: len(order) == 2, timeout=WAIT_MS)
    assert order == ["state", "display:idle"]


def test_graceful_stop_shows_stopped_while_github_lags(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = _started(make_controller, qtbot, record)
    _refresh(qtbot, h, [status_row(record, LocalState.IDLE, remote=RemoteStatus.ONLINE)])
    with qtbot.waitSignal(h.controller.runner_stopped, timeout=WAIT_MS):
        h.controller.stop(record.id)  # graceful; FakeProcess.stopped_at = NOW
    h.clock.advance(30)
    shown = h.controller.display(record.id)
    assert (shown.state, shown.detail) == (DisplayState.STOPPED, DETAIL_OFFLINE_LAG)
    # A local-only refresh keeps GitHub's (stale) "online" and the grace period still holds.
    rows = _refresh(qtbot, h, [status_row(record, remote=RemoteStatus.UNKNOWN)], remote=False)
    assert h.manager.calls[-1] == ("statuses", {"remote": False})
    assert rows[0].display.detail == DETAIL_OFFLINE_LAG
    changed = Spy(h.controller.display_changed)
    h.clock.advance(70)  # 100 s after the stop: GitHub should have noticed by now
    h.controller._on_remerge_timer()
    assert [d.state for _, d in changed.calls] == [DisplayState.EXTERNAL]


def test_a_newer_stop_mark_beats_the_process_stop_info(make_controller: Any, qtbot: Any) -> None:
    """The CLI hard-killed the runner after our own graceful stop: the cached stop mark wins."""
    record = record_model()
    h = _started(make_controller, qtbot, record)
    with qtbot.waitSignal(h.controller.runner_stopped, timeout=WAIT_MS):
        h.controller.stop(record.id)
    h.clock.advance(20)
    newer = status_row(
        record,
        remote=RemoteStatus.ONLINE,
        stopped_at=NOW + timedelta(seconds=10),
        forced=True,
    )
    rows = _refresh(qtbot, h, [newer])
    assert rows[0].display.state is DisplayState.SESSION_LINGERING
    older = status_row(
        record,
        remote=RemoteStatus.ONLINE,
        stopped_at=NOW - timedelta(seconds=10),
        forced=True,
    )
    rows = _refresh(qtbot, h, [older])
    assert (rows[0].display.state, rows[0].display.detail) == (
        DisplayState.STOPPED,
        DETAIL_OFFLINE_LAG,
    )


def test_remerge_timer_turns_connecting_into_stalled(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = _started(make_controller, qtbot, record)
    timer = h.controller._remerge_timer
    assert timer.isActive() and timer.interval() == REMERGE_INTERVAL_MS
    _refresh(qtbot, h, [status_row(record, LocalState.STARTING, remote=RemoteStatus.OFFLINE)])
    assert h.controller.display(record.id).detail == DETAIL_CONNECTING
    changed = Spy(h.controller.display_changed)
    h.clock.advance(30)
    h.controller._on_remerge_timer()
    assert changed.calls == []
    h.clock.advance(31)
    h.controller._on_remerge_timer()
    assert [(rid, d.state, d.detail) for rid, d in changed.calls] == [
        (record.id, DisplayState.STALLED, DETAIL_STALLED)
    ]


def test_remerge_timer_runs_only_with_records(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    assert not h.controller._remerge_timer.isActive()
    with qtbot.waitSignal(h.controller.runner_imported, timeout=WAIT_MS):
        h.controller.import_runner("/runners/imported")
    assert h.controller._remerge_timer.isActive()
    (record,) = h.controller.records
    with qtbot.waitSignal(h.controller.runner_forgotten, timeout=WAIT_MS):
        h.controller.forget(record.id)
    assert not h.controller._remerge_timer.isActive()


def test_a_failed_repo_resets_its_remote_cache(make_controller: Any, qtbot: Any) -> None:
    a, b = record_model("a", repo="one"), record_model("b", repo="two")
    h = make_controller(records=[a, b])
    _refresh(
        qtbot,
        h,
        [
            status_row(a, remote=RemoteStatus.ONLINE, busy=True),
            status_row(b, remote=RemoteStatus.ONLINE),
        ],
    )
    assert h.controller.remote_busy(a.id) and not h.controller.remote_busy(b.id)
    assert h.controller.display(a.id).state is DisplayState.EXTERNAL
    # remote=False: the GitHub cache survives.
    _refresh(
        qtbot,
        h,
        [status_row(a, remote=RemoteStatus.UNKNOWN), status_row(b, remote=RemoteStatus.UNKNOWN)],
        remote=False,
    )
    assert h.controller.remote_busy(a.id)
    assert h.controller.display(a.id).state is DisplayState.EXTERNAL
    # a's repo fails, b's answers: only a falls back to "unknown".
    _refresh(
        qtbot,
        h,
        [status_row(a, remote_error="网络错误"), status_row(b, remote=RemoteStatus.OFFLINE)],
    )
    assert not h.controller.remote_busy(a.id)
    assert h.controller.display(a.id).detail == DETAIL_REMOTE_UNKNOWN
    assert h.controller.display(b.id) == h.controller.all_displays()[b.id]
    assert h.controller.display(b.id).detail is None
    assert not h.controller.remote_busy("unknown")


def test_quiet_refresh_failure_only_logs(make_controller: Any, qtbot: Any) -> None:
    h = make_controller(records=[record_model()])
    h.manager.errors["statuses"] = StoreError("配置文件不可读")
    failed = Spy(h.controller.failed)
    refreshing = Spy(h.controller.refreshing_changed)
    h.controller.refresh_statuses(remote=False, quiet=True)
    refreshing.wait(qtbot, 2)
    assert refreshing.calls == [(True,), (False,)]
    assert failed.calls == []
    assert not h.controller.refreshing


def test_rows_of_records_removed_during_a_refresh_are_not_cached(
    make_controller: Any, qtbot: Any
) -> None:
    a, b = record_model("a", repo="one"), record_model("b", repo="two")
    h = make_controller(records=[a])
    rows = _refresh(qtbot, h, [status_row(a, remote=RemoteStatus.ONLINE), status_row(b)])
    assert rows[1] is h.manager.rows[1]  # unknown record: passed through untouched
    assert b.id not in h.controller._locals and b.id not in h.controller._remotes


# --- M3: stop marks ----------------------------------------------------------------------------


def test_stop_records_the_stop_on_the_worker(make_controller: Any, qtbot: Any) -> None:
    a, b = record_model("a", repo="one"), record_model("b", repo="two")
    h = _started(make_controller, qtbot, a, b)
    h.process(b.id).stop_result = StopResult(False, StopMethod.KILL, 1, [10])
    stopped = Spy(h.controller.runner_stopped)
    h.controller.stop(a.id)
    h.controller.stop(b.id)
    stopped.wait(qtbot, 2)
    assert sorted(h.manager.stop_marks, key=lambda m: str(m[0])) == [
        (Path(a.path), False),
        (Path(b.path), True),
    ]
    marks = [i for i, (name, _) in enumerate(h.manager.calls) if name == "record_stop"]
    assert all(h.manager.threads[i] is not threading.main_thread() for i in marks)


def test_shutdown_records_the_stops(make_controller: Any, qtbot: Any) -> None:
    a = record_model("a", repo="one")
    h = _started(make_controller, qtbot, a)
    assert h.controller.shutdown() == []
    assert h.manager.stop_marks == [(Path(a.path), False)]
    assert h.manager.threads[-1] is not threading.main_thread()


def test_remove_does_not_record_a_stop_mark(make_controller: Any, qtbot: Any) -> None:
    """remove_runner clears the mark anyway; the stop inside remove() writes none."""
    record = record_model()
    h = _started(make_controller, qtbot, record)
    with qtbot.waitSignal(h.controller.runner_removed, timeout=WAIT_MS):
        h.controller.remove(record.id)
    assert h.manager.stop_marks == []


# --- M3: auto start ------------------------------------------------------------------------------


def test_start_auto_runners_skips_external_processes(make_controller: Any, qtbot: Any) -> None:
    auto = record_model("auto", repo="one").model_copy(update={"auto_start": True})
    taken = record_model("taken", repo="two").model_copy(update={"auto_start": True})
    manual = record_model("manual", repo="three")
    h = make_controller(records=[auto, taken, manual])
    h.manager.rows = [
        status_row(auto, remote=RemoteStatus.UNKNOWN),
        status_row(taken, remote=RemoteStatus.UNKNOWN, external=True),
        status_row(manual, remote=RemoteStatus.UNKNOWN),
    ]
    statuses = Spy(h.controller.statuses_updated)
    changed = Spy(h.controller.display_changed)
    op, message, exc = _wait_failed(qtbot, h, h.controller.start_auto_runners)
    assert (op, message, exc) == ("autostart", AUTOSTART_SKIPPED + "taken", None)
    assert ("statuses", {"remote": False}) in h.manager.calls
    assert h.manager.statuses_seen == [{}]
    _wait_idle(qtbot, h, auto.id)
    assert list(h.manager.processes) == [auto.id]
    assert h.process(auto.id).calls == ["start"]
    process_for = [i for i, (name, _) in enumerate(h.manager.calls) if name == "process_for"]
    assert h.manager.threads[process_for[0]] is threading.main_thread()
    assert statuses.calls == []  # a private scan, not a refresh
    assert (taken.id, h.controller.display(taken.id)) in changed.calls
    assert h.controller.display(taken.id).state is DisplayState.EXTERNAL


def test_start_auto_runners_skips_running_ones_and_needs_auto_records(
    make_controller: Any, qtbot: Any
) -> None:
    h = make_controller(records=[record_model()])
    h.controller.start_auto_runners()  # no auto_start record: nothing scanned
    assert h.manager.calls == []

    auto = record_model("auto").model_copy(update={"auto_start": True})
    h2 = _started(make_controller, qtbot, auto)
    h2.manager.rows = [status_row(auto, LocalState.STARTING, remote=RemoteStatus.UNKNOWN)]
    failed = Spy(h2.controller.failed)
    h2.controller.start_auto_runners()
    qtbot.waitUntil(lambda: ("statuses", {"remote": False}) in h2.manager.calls)
    qtbot.wait(50)
    assert h2.process(auto.id).calls == ["start"]  # not started twice
    assert failed.calls == []


def test_start_auto_runners_failure(make_controller: Any, qtbot: Any) -> None:
    auto = record_model("auto").model_copy(update={"auto_start": True})
    h = make_controller(records=[auto])
    h.manager.errors["statuses"] = ProcessError("无法扫描进程")
    op, message, _ = _wait_failed(qtbot, h, h.controller.start_auto_runners)
    assert (op, message) == ("autostart", "无法扫描进程")


def test_set_auto_start(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = make_controller(records=[record])
    with qtbot.waitSignal(h.controller.config_changed, timeout=WAIT_MS) as blocker:
        h.controller.set_auto_start(record.id, True)
    assert blocker.args[0].runners[0].auto_start
    assert h.controller.records[0].auto_start
    assert h.manager.calls[-1] == ("set_auto_start", {"record_id": record.id, "enabled": True})
    h.manager.errors["set_auto_start"] = StoreError("无法写入配置文件")
    op, message, _ = _wait_failed(qtbot, h, lambda: h.controller.set_auto_start(record.id, False))
    assert (op, message) == ("auto_start", "无法写入配置文件")
    op, _, _ = _wait_failed(qtbot, h, lambda: h.controller.set_auto_start("nope", True))
    assert op == "auto_start"


def test_add_runner_passes_auto_start(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    with qtbot.waitSignal(h.controller.runner_added, timeout=WAIT_MS) as blocker:
        h.controller.add_runner(plan_model(), auto_start=True)
    assert blocker.args[0].auto_start
    assert h.manager.calls[-1][1]["auto_start"] is True


# --- M3: import / forget / discover --------------------------------------------------------------


def test_import_runner_emits_imported_then_config_changed(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    order: list[str] = []
    h.controller.runner_imported.connect(lambda r: order.append(f"imported:{r.name}"))
    h.controller.config_changed.connect(lambda c: order.append(f"config:{len(c.runners)}"))
    changed = Spy(h.controller.display_changed)
    with (
        qtbot.waitSignal(h.controller.discovery_updated, timeout=WAIT_MS),
        qtbot.waitSignal(h.controller.config_changed, timeout=WAIT_MS),
    ):
        h.controller.import_runner("/runners/imported", auto_start=True)
    assert order == ["imported:box-imported", "config:1"]
    (record,) = h.controller.records
    assert record.auto_start
    # The discover() that refreshes the banner runs after the import, on its own worker.
    names = [name for name, _ in h.manager.calls]
    index = names.index("import_runner")
    assert h.manager.calls[index] == (
        "import_runner",
        {"path": "/runners/imported", "auto_start": True},
    )
    assert names.index("discover") > index
    assert h.manager.threads[index] is not threading.main_thread()
    assert changed.calls == [(record.id, h.controller.display(record.id))]


def test_import_runner_failure(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    h.manager.errors["import_runner"] = InvalidInputError("不是 runner 目录")
    op, message, _ = _wait_failed(qtbot, h, lambda: h.controller.import_runner("/x"))
    assert (op, message) == ("import", "不是 runner 目录")
    assert h.controller.records == []


def test_forget_deletes_only_the_record(make_controller: Any, qtbot: Any) -> None:
    keep, gone = record_model("keep", repo="one"), record_model("gone", repo="two")
    h = make_controller(records=[keep, gone])
    _refresh(qtbot, h, [status_row(keep), status_row(gone, remote=RemoteStatus.ONLINE)])
    order: list[str] = []
    h.controller.runner_forgotten.connect(lambda rid: order.append(f"forgotten:{rid}"))
    h.controller.config_changed.connect(lambda c: order.append(f"config:{len(c.runners)}"))
    busy = Spy(h.controller.busy_changed)
    with (
        qtbot.waitSignal(h.controller.discovery_updated, timeout=WAIT_MS),
        qtbot.waitSignal(h.controller.config_changed, timeout=WAIT_MS),
    ):
        h.controller.forget(gone.id)
    assert order == [f"forgotten:{gone.id}", "config:1"]
    assert [r.id for r in h.controller.records] == [keep.id]
    names = [name for name, _ in h.manager.calls]
    assert ("forget_runner", {"record_id": gone.id}) in h.manager.calls
    assert names.index("discover") > names.index("forget_runner")  # banner refreshed after
    assert busy.calls == [(gone.id, True), (gone.id, False)]
    assert gone.id not in h.controller._remotes and gone.id not in h.controller._displays
    assert not h.controller.remote_busy(gone.id)


def test_forget_is_refused_while_running_or_busy(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = _started(make_controller, qtbot, record)
    op, message, exc = _wait_failed(qtbot, h, lambda: h.controller.forget(record.id))
    assert (op, exc) == ("forget", None)
    assert "正在运行" in message
    gate = threading.Event()
    h.process(record.id).block_stop = gate
    h.controller.stop(record.id)
    h.process(record.id)._running = False  # stopped, but the stop operation still runs
    op, message, _ = _wait_failed(qtbot, h, lambda: h.controller.forget(record.id))
    assert op == "forget" and "正在执行其他操作" in message
    gate.set()
    _wait_idle(qtbot, h, record.id)
    assert "forget_runner" not in [name for name, _ in h.manager.calls]
    op, _, _ = _wait_failed(qtbot, h, lambda: h.controller.forget("nope"))
    assert op == "forget"


def test_forget_failure_keeps_the_record(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = make_controller(records=[record])
    h.manager.errors["forget_runner"] = StoreError("无法写入配置文件")
    op, message, _ = _wait_failed(qtbot, h, lambda: h.controller.forget(record.id))
    assert (op, message) == ("forget", "无法写入配置文件")
    _wait_idle(qtbot, h, record.id)
    assert [r.id for r in h.controller.records] == [record.id]


def test_discover(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    stray = StrayRunner(path=Path("/runners/old"), pids=(5,), configured=True, service=False)
    h.manager.discovery = Discovery(
        root=Path("/runners"), unregistered=(Path("/runners/new"),), strays=(stray,)
    )
    with qtbot.waitSignal(h.controller.discovery_updated, timeout=WAIT_MS) as blocker:
        h.controller.discover()
    assert blocker.args == [h.manager.discovery]
    assert h.manager.threads[-1] is not threading.main_thread()
    h.manager.errors["discover"] = ProcessError("无法扫描进程")
    op, message, _ = _wait_failed(qtbot, h, h.controller.discover)
    assert (op, message) == ("discover", "无法扫描进程")


# --- M3: external processes ----------------------------------------------------------------------


def test_kill_external_clears_the_cached_external(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = make_controller(records=[record])
    _refresh(qtbot, h, [status_row(record, remote=RemoteStatus.ONLINE, external=True)])
    assert h.controller.display(record.id).state is DisplayState.EXTERNAL
    busy = Spy(h.controller.busy_changed)
    with qtbot.waitSignal(h.controller.external_stopped, timeout=WAIT_MS) as blocker:
        h.controller.kill_external(record.id)
    assert blocker.args == [record.id, h.manager.external_result]
    assert h.manager.calls[-1] == ("kill_external", {"record_id": record.id})
    assert h.manager.threads[-1] is not threading.main_thread()
    assert busy.calls == [(record.id, True), (record.id, False)]
    # Killed = a forced stop just now: GitHub's lingering session, not "someone else's".
    assert h.controller.display(record.id).state is DisplayState.SESSION_LINGERING


def test_kill_external_with_survivors_keeps_external(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = make_controller(records=[record])
    _refresh(qtbot, h, [status_row(record, remote=RemoteStatus.ONLINE, external=True)])
    h.manager.external_result = ExternalStop(killed=[1], survivors=[2])
    with qtbot.waitSignal(h.controller.external_stopped, timeout=WAIT_MS):
        h.controller.kill_external(record.id)
    assert h.controller.display(record.id).state is DisplayState.EXTERNAL


def test_kill_external_refusals_and_failure(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = _started(make_controller, qtbot, record)
    op, message, exc = _wait_failed(qtbot, h, lambda: h.controller.kill_external(record.id))
    assert (op, exc) == ("kill", None)
    assert "由 RunnerDeck 运行" in message
    op, _, _ = _wait_failed(qtbot, h, lambda: h.controller.kill_external("nope"))
    assert op == "kill"
    other = record_model("other", repo="two")
    h2 = make_controller(records=[other])
    h2.manager.errors["kill_external"] = InvalidInputError("这个 runner 以 Windows 服务方式运行")
    op, message, _ = _wait_failed(qtbot, h2, lambda: h2.controller.kill_external(other.id))
    assert (op, message) == ("kill", "这个 runner 以 Windows 服务方式运行")
    _wait_idle(qtbot, h2, other.id)


def test_take_over_kills_then_starts_on_the_main_thread(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = make_controller(records=[record])
    _refresh(qtbot, h, [status_row(record, remote=RemoteStatus.ONLINE, external=True)])
    order: list[str] = []
    h.controller.external_stopped.connect(lambda *_: order.append("external_stopped"))
    h.controller.runner_state.connect(lambda _rid, s, _j: order.append(f"state:{s}"))
    h.controller.take_over(record.id)
    qtbot.waitUntil(lambda: h.controller.is_running(record.id), timeout=WAIT_MS)
    _wait_idle(qtbot, h, record.id)
    qtbot.waitUntil(lambda: len(order) == 2, timeout=WAIT_MS)
    assert order == ["external_stopped", "state:starting"]
    names = [name for name, _ in h.manager.calls]
    assert names[-2:] == ["kill_external", "process_for"]
    assert h.manager.threads[-2] is not threading.main_thread()
    assert h.manager.threads[-1] is threading.main_thread()
    assert h.process(record.id).calls == ["start"]
    assert h.controller.display(record.id).state is not DisplayState.EXTERNAL


def test_take_over_with_survivors_does_not_start(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = make_controller(records=[record])
    h.manager.external_result = ExternalStop(killed=[], survivors=[7, 8])
    stopped = Spy(h.controller.external_stopped)
    op, message, _ = _wait_failed(qtbot, h, lambda: h.controller.take_over(record.id))
    assert op == "take_over"
    assert "7、8" in message
    assert stopped.calls == [(record.id, h.manager.external_result)]
    assert h.manager.processes == {}
    assert not h.controller.is_busy(record.id)


def test_take_over_failure_and_refusal(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = make_controller(records=[record])
    h.manager.errors["kill_external"] = ProcessError("无法结束进程")
    op, message, _ = _wait_failed(qtbot, h, lambda: h.controller.take_over(record.id))
    assert (op, message) == ("take_over", "无法结束进程")
    _wait_idle(qtbot, h, record.id)
    assert h.manager.processes == {}
    h2 = _started(make_controller, qtbot, record_model("mine", repo="two"))
    (mine,) = h2.controller.records
    op, _, exc = _wait_failed(qtbot, h2, lambda: h2.controller.take_over(mine.id))
    assert (op, exc) == ("take_over", None)


def test_kill_stray(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    with (
        qtbot.waitSignal(h.controller.discovery_updated, timeout=WAIT_MS),
        qtbot.waitSignal(h.controller.external_stopped, timeout=WAIT_MS) as blocker,
    ):
        h.controller.kill_stray("/runners/old")
    assert blocker.args == ["/runners/old", h.manager.stray_result]
    names = [name for name, _ in h.manager.calls]
    assert ("kill_stray", {"path": "/runners/old"}) in h.manager.calls
    assert names.index("discover") > names.index("kill_stray")  # the banner is refreshed
    h.manager.errors["kill_stray"] = InvalidInputError("这个目录已登记")
    op, message, _ = _wait_failed(qtbot, h, lambda: h.controller.kill_stray("/runners/x"))
    assert (op, message) == ("kill", "这个目录已登记")


# --- M3: diagnostics logs ------------------------------------------------------------------------


def _diag_record(tmp_path: Path) -> tuple[RunnerRecord, Path]:
    runner_dir = tmp_path / "runner"
    diag_dir = runner_dir / "_diag"
    diag_dir.mkdir(parents=True)
    log_file = diag_dir / "Runner_20261023-120000-utc.log"
    log_file.write_text("x", encoding="utf-8")
    return record_model(path=str(runner_dir)), log_file


def test_load_diag_logs(make_controller: Any, qtbot: Any, tmp_path: Path) -> None:
    record, log_file = _diag_record(tmp_path)
    h = make_controller(records=[record])
    h.diag.logs = [DiagLog(path=log_file, kind="runner", modified=NOW, size=1)]
    with qtbot.waitSignal(h.controller.diag_logs_loaded, timeout=WAIT_MS) as blocker:
        h.controller.load_diag_logs(record.id)
    assert blocker.args == [record.id, h.diag.logs]
    assert h.diag.calls == [("list_logs", Path(record.path))]
    assert h.diag.threads[0] is not threading.main_thread()
    h.diag.error = RunnerDeckError("无法读取")
    op, message, _ = _wait_failed(qtbot, h, lambda: h.controller.load_diag_logs(record.id))
    assert (op, message) == ("diag", "无法读取")
    op, _, _ = _wait_failed(qtbot, h, lambda: h.controller.load_diag_logs("nope"))
    assert op == "diag"


def test_load_diag_text(make_controller: Any, qtbot: Any, tmp_path: Path) -> None:
    record, log_file = _diag_record(tmp_path)
    h = make_controller(records=[record])
    h.diag.texts[log_file] = "Listening for Jobs"
    with qtbot.waitSignal(h.controller.diag_text_loaded, timeout=WAIT_MS) as blocker:
        h.controller.load_diag_text(record.id, str(log_file))
    assert blocker.args == [record.id, str(log_file), "Listening for Jobs"]
    assert h.diag.threads[0] is not threading.main_thread()


@pytest.mark.parametrize(
    "where",
    ["outside", "dotdot", "diag_dir_itself", "other_runner", "relative"],
)
def test_load_diag_text_refuses_paths_outside_diag(
    make_controller: Any, qtbot: Any, tmp_path: Path, where: str
) -> None:
    record, log_file = _diag_record(tmp_path)
    secret = tmp_path / "runner" / ".credentials"
    secret.write_text("{}", encoding="utf-8")
    other = tmp_path / "other" / "_diag" / "Runner_1.log"
    other.parent.mkdir(parents=True)
    other.write_text("x", encoding="utf-8")
    path = {
        "outside": str(secret),
        "dotdot": str(log_file.parent / ".." / ".credentials"),
        "diag_dir_itself": str(log_file.parent),
        "other_runner": str(other),
        "relative": "_diag/Runner_1.log",
    }[where]
    h = make_controller(records=[record])
    op, message, exc = _wait_failed(qtbot, h, lambda: h.controller.load_diag_text(record.id, path))
    assert op == "diag"
    assert isinstance(exc, InvalidInputError)
    assert "_diag" in message
    assert h.diag.calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need a privilege on Windows")
def test_load_diag_text_refuses_a_symlink_out_of_diag(
    make_controller: Any, qtbot: Any, tmp_path: Path
) -> None:
    record, log_file = _diag_record(tmp_path)
    secret = tmp_path / "runner" / ".credentials"
    secret.write_text("{}", encoding="utf-8")
    link = log_file.parent / "Runner_link.log"
    os.symlink(secret, link)
    h = make_controller(records=[record])
    op, _, _ = _wait_failed(qtbot, h, lambda: h.controller.load_diag_text(record.id, str(link)))
    assert op == "diag"
    assert h.diag.calls == []


# --- M3: launch on login -------------------------------------------------------------------------


def test_autostart_supported_follows_the_ops(make_controller: Any) -> None:
    assert make_controller().controller.autostart_supported
    assert not make_controller(autostart_supported=False).controller.autostart_supported


def test_load_autostart_state(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    with qtbot.waitSignal(h.controller.autostart_state_loaded, timeout=WAIT_MS) as blocker:
        h.controller.load_autostart_state()
    (state,) = blocker.args
    assert state.supported and not state.registered
    assert h.autostart.threads[0] is not threading.main_thread()
    h.autostart.error = RunnerDeckError("无法读取注册表")
    op, message, _ = _wait_failed(qtbot, h, h.controller.load_autostart_state)
    assert (op, message) == ("launch_on_login", "无法读取注册表")


def test_set_launch_on_login_writes_registry_then_settings(
    make_controller: Any, qtbot: Any
) -> None:
    h = make_controller()
    order: list[str] = []
    h.controller.autostart_state_loaded.connect(lambda s: order.append(f"state:{s.registered}"))
    h.controller.config_changed.connect(
        lambda c: order.append(f"config:{c.settings.launch_on_login}")
    )
    with qtbot.waitSignal(h.controller.config_changed, timeout=WAIT_MS):
        h.controller.set_launch_on_login(True)
    assert order == ["state:True", "config:True"]
    assert h.autostart.calls == [("enable",)]
    assert h.autostart.threads[0] is not threading.main_thread()
    assert h.store.load().settings.launch_on_login
    with qtbot.waitSignal(h.controller.config_changed, timeout=WAIT_MS):
        h.controller.set_launch_on_login(False)
    assert h.autostart.calls[-1] == ("disable",)
    assert not h.controller.config.settings.launch_on_login


def test_set_launch_on_login_failure_keeps_settings(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    h.autostart.error = RunnerDeckError("无法写入注册表 HKCU\\...\\Run")
    op, message, _ = _wait_failed(qtbot, h, lambda: h.controller.set_launch_on_login(True))
    assert op == "launch_on_login"
    assert "无法写入注册表" in message
    assert not h.store.load().settings.launch_on_login


def test_set_launch_on_login_unsupported(make_controller: Any, qtbot: Any) -> None:
    h = make_controller(autostart_supported=False)
    op, message, exc = _wait_failed(qtbot, h, lambda: h.controller.set_launch_on_login(True))
    assert (op, message, exc) == ("launch_on_login", AUTOSTART_UNSUPPORTED, None)
    assert h.autostart.calls == []


def test_reconcile_autostart_uses_the_saved_setting(make_controller: Any, qtbot: Any) -> None:
    h = make_controller(settings=AppSettings(launch_on_login=True))
    with qtbot.waitSignal(h.controller.autostart_state_loaded, timeout=WAIT_MS) as blocker:
        h.controller.reconcile_autostart()
    assert blocker.args[0].registered
    assert h.autostart.calls == [("reconcile", True)]


def test_save_settings_applies_a_changed_launch_on_login(make_controller: Any, qtbot: Any) -> None:
    h = make_controller()
    loaded = Spy(h.controller.autostart_state_loaded)
    with qtbot.waitSignal(h.controller.config_changed, timeout=WAIT_MS):
        h.controller.save_settings(AppSettings(poll_interval_s=60))
    assert h.autostart.calls == []  # launch_on_login unchanged
    with qtbot.waitSignal(h.controller.config_changed, timeout=WAIT_MS):
        h.controller.save_settings(AppSettings(poll_interval_s=60, launch_on_login=True))
    assert h.autostart.calls == [("enable",)]
    assert h.autostart.threads[0] is not threading.main_thread()
    assert loaded.calls and loaded.calls[-1][0].registered
    h.autostart.error = RunnerDeckError("无法写入注册表")
    op, _, _ = _wait_failed(
        qtbot, h, lambda: h.controller.save_settings(AppSettings(launch_on_login=False))
    )
    assert op == "settings"
    assert h.store.load().settings.launch_on_login  # nothing saved


def test_save_settings_ignores_launch_on_login_when_unsupported(
    make_controller: Any, qtbot: Any
) -> None:
    h = make_controller(autostart_supported=False)
    with qtbot.waitSignal(h.controller.config_changed, timeout=WAIT_MS):
        h.controller.save_settings(AppSettings(launch_on_login=True))
    assert h.autostart.calls == []
    assert h.store.load().settings.launch_on_login


# --- M3: session end ------------------------------------------------------------------------------


def test_emergency_stop_all_signals_running_runners(make_controller: Any, qtbot: Any) -> None:
    a, b, c = (
        record_model("a", repo="one"),
        record_model("b", repo="two"),
        record_model("c", repo="three"),
    )
    h = _started(make_controller, qtbot, a, b, c)
    with qtbot.waitSignal(h.controller.runner_stopped, timeout=WAIT_MS):
        h.controller.stop(c.id)
    h.process(a.id).signal_error = OSError("pipe closed")
    assert h.controller.emergency_stop_all() == 1
    assert h.process(a.id).calls[-1] == "signal_stop"
    assert h.process(b.id).calls == ["start", "signal_stop"]
    assert h.process(b.id).threads[-1] is threading.main_thread()
    assert "signal_stop" not in h.process(c.id).calls
    assert h.manager.stop_marks == [(Path(c.path), False)]  # no disk IO at session end
    assert make_controller().controller.emergency_stop_all() == 0


# --- M3: defaults ---------------------------------------------------------------------------------


def test_default_ops(isolated_home: Path, pool: Any) -> None:
    c = RunnerController(environ={}, pool=pool)
    assert isinstance(c._autostart, CoreAutostart)
    assert isinstance(c._diag, CoreDiag)
    assert c.autostart_supported is (sys.platform == "win32")


def test_core_diag_lists_nothing_without_a_diag_dir(tmp_path: Path) -> None:
    pytest.importorskip("runnerdeck.core.diag")
    assert CoreDiag().list_logs(tmp_path) == []


@pytest.mark.skipif(sys.platform == "win32", reason="would touch the real HKCU registry")
def test_core_autostart_is_unsupported_off_windows() -> None:
    pytest.importorskip("runnerdeck.core.autostart")
    ops = CoreAutostart()
    assert not ops.supported
    assert not ops.reconcile(True).supported
    assert not ops.state().supported


def test_status_rows_keep_their_other_fields(make_controller: Any, qtbot: Any) -> None:
    record = record_model()
    h = make_controller(records=[record])
    row = status_row(record, remote=RemoteStatus.OFFLINE)
    (merged,) = _refresh(qtbot, h, [row])
    assert dataclasses.replace(merged, display=row.display) == row


def test_save_settings_from_a_stale_copy_keeps_the_other_save(
    make_controller: Any, qtbot: Any
) -> None:
    # Review M3 #6: launch on login was switched on (and saved) after the UI made its copy;
    # that stale copy still says False and must not switch it off again or drop other fields.
    h = make_controller()
    stale = h.controller.config.settings  # launch_on_login=False, poll 30
    config = h.store.load()
    config.settings = config.settings.model_copy(update={"launch_on_login": True})
    h.store.save(config)  # e.g. set_launch_on_login landed on disk in the meantime
    with qtbot.waitSignal(h.controller.config_changed, timeout=WAIT_MS):
        h.controller.save_settings(stale.model_copy(update={"poll_interval_s": 120}))
    saved = h.store.load().settings
    assert saved.poll_interval_s == 120
    assert saved.launch_on_login  # not undone
    assert h.autostart.calls == []  # and the Run entry was not touched
