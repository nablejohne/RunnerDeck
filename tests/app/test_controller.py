"""Tests for runnerdeck.app.controller.RunnerController (docs/m2-design.md §4).

A fake manager / client / process (tests/app/conftest.py) replace network, config.cmd and
run.cmd; the ConfigStore and the keyring are real but isolated. Every operation is checked for
its success signal, its `failed(op, ...)` on error, the busy bookkeeping, and that signals
fed from worker or reader threads arrive on the main thread.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from PyQt5.QtCore import QCoreApplication, QThread, QUrl

from runnerdeck.app.controller import LOGIN_HINT, STEP_STOP, RunnerController
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
from runnerdeck.core.models import AppSettings, DisplayState, LocalState, RunnerRecord
from runnerdeck.core.process import Launch, StopMethod, StopResult
from runnerdeck.core.store import ConfigStore

from .conftest import (
    FAKE_PAT,
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
    assert h.manager.calls[-1] == ("add_runner", {"plan": plan})
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
    assert h.manager.calls[-1][0] == "process_for"  # remove_runner never ran


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
    h.manager.rows = [status_row(a, LocalState.IDLE), status_row(b, remote_error="网络错误")]
    refreshing = Spy(h.controller.refreshing_changed)
    with qtbot.waitSignal(h.controller.statuses_updated, timeout=WAIT_MS) as blocker:
        h.controller.refresh_statuses()
        assert h.controller.refreshing
        h.controller.refresh_statuses()  # ignored while one is in flight
    (rows,) = blocker.args
    assert [r.record.id for r in rows] == [a.id, b.id]
    assert len(h.manager.statuses_seen) == 1
    assert h.manager.statuses_seen[0] == {a.id: h.process(a.id)}
    assert not h.controller.refreshing
    assert refreshing.calls == [(True,), (False,)]
    assert h.controller.display(a.id) is rows[0].display
    assert h.controller.display(b.id).state is DisplayState.STOPPED
    assert rows[1].remote_error == "网络错误"
    # A local state change invalidates the cached merge for that runner only.
    h.process(a.id).drive_state(LocalState.BUSY, "build")
    qtbot.waitUntil(lambda: h.controller.display(a.id).state is DisplayState.BUSY, timeout=WAIT_MS)
    assert h.controller.display(a.id).label == "运行中：build"
    assert h.controller.display(b.id) is rows[1].display


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
