"""TrayIcon: colour level, tooltip, menu rebuilt on aboutToShow, actions, notification filters.

The offscreen platform reports no system tray, so the window is built with tray=True and the
icon is exercised directly; showMessage is recorded instead of shown.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QSystemTrayIcon
from pytestqt.qtbot import QtBot
from qfluentwidgets import MessageBox

from runnerdeck.core.models import (
    AppConfig,
    AppSettings,
    Discovery,
    DisplayState,
    LocalState,
    StrayRunner,
)
from runnerdeck.core.status import TrayLevel
from runnerdeck.ui.main_window import MainWindow
from runnerdeck.ui.tray import (
    ICON_SIZE,
    MESSAGE_MAX,
    TITLE_MAX,
    TOOLTIP_MAX,
    TRAY_COLORS,
    TrayIcon,
    job_result_text,
    status_icon,
    tooltip_text,
    truncate,
)
from ui.conftest import FakeController, make_display, make_record

Message = tuple[str, str, object]


@pytest.fixture
def window(qtbot: QtBot, fake_with_records: FakeController) -> MainWindow:
    win = MainWindow(fake_with_records, prompt_login=False, tray=True)
    qtbot.addWidget(win)
    return win


@pytest.fixture
def tray(window: MainWindow) -> TrayIcon:
    assert window.tray is not None
    return window.tray


@pytest.fixture
def messages(tray: TrayIcon, monkeypatch: pytest.MonkeyPatch) -> list[Message]:
    shown: list[Message] = []
    monkeypatch.setattr(
        tray, "showMessage", lambda title, text, icon, _ms=0: shown.append((title, text, icon))
    )
    return shown


def _texts(tray: TrayIcon) -> list[str]:
    return [action.text() for action in tray.menu_actions]


def _action(tray: TrayIcon, text: str):  # type: ignore[no-untyped-def]
    return next(a for a in tray.menu_actions if a.text() == text)


def _settings(fake: FakeController, **changes: object) -> None:
    fake.config.settings = fake.config.settings.model_copy(update=changes)


def test_window_without_tray(qtbot: QtBot, fake: FakeController) -> None:
    win = MainWindow(fake, prompt_login=False)  # offscreen: no system tray available
    qtbot.addWidget(win)
    assert win.tray is None
    forced = MainWindow(fake, prompt_login=False, tray=False)
    qtbot.addWidget(forced)
    assert forced.tray is None


def test_level_follows_display_changes(tray: TrayIcon, fake_with_records: FakeController) -> None:
    fake = fake_with_records
    alpha, beta = (r.id for r in fake.records)
    assert tray.level is TrayLevel.STOPPED
    assert not tray.icon().isNull()
    assert tray.toolTip() == "RunnerDeck\n运行中 0 · 空闲 0 · 异常 0 · 已停止 2"

    fake.set_display(alpha, make_display(DisplayState.IDLE))
    assert tray.level is TrayLevel.ONLINE
    fake.set_display(beta, make_display(DisplayState.BUSY, job="build"))
    assert tray.level is TrayLevel.BUSY
    assert tray.toolTip() == "RunnerDeck\n运行中 1 · 空闲 1 · 异常 0 · 已停止 0"
    fake.set_display(alpha, make_display(DisplayState.EXTERNAL))
    assert tray.level is TrayLevel.ERROR
    fake.set_display(alpha, make_display(DisplayState.SESSION_LINGERING))
    fake.set_display(beta, make_display(DisplayState.STOPPED))
    assert tray.level is TrayLevel.STOPPED

    fake.config = AppConfig(account="octocat")
    fake.config_changed.emit(fake.config)
    assert tray.toolTip() == "RunnerDeck\n还没有 runner"


@pytest.mark.parametrize("level", list(TrayLevel))
def test_status_icon_draws_the_level_colour(qtbot: QtBot, level: TrayLevel) -> None:
    image = status_icon(QIcon(), level).pixmap(ICON_SIZE, ICON_SIZE).toImage()
    dot = image.pixelColor(ICON_SIZE - ICON_SIZE // 5, ICON_SIZE - ICON_SIZE // 5)
    assert dot.name() == TRAY_COLORS[level]
    assert image.pixelColor(2, 2).alpha() == 0  # no base icon: transparent elsewhere


def test_tooltip_and_message_truncation() -> None:
    assert truncate("abc", 3) == "abc"
    assert truncate("abcdef", 4) == "abc…"
    assert len(tooltip_text([DisplayState.IDLE] * 3)) <= TOOLTIP_MAX
    assert job_result_text("SucceededWithIssues") == "成功（有警告）"
    assert job_result_text("Weird") == "Weird"


def test_menu_rebuilt_on_about_to_show(
    tray: TrayIcon, fake_with_records: FakeController, window: MainWindow
) -> None:
    fake = fake_with_records
    alpha, beta = (r.id for r in fake.records)
    assert _texts(tray) == [
        "打开主界面",
        "全部启动",
        "全部停止",
        "启动 pc-alpha",
        "启动 pc-beta",
        "退出",
    ]
    assert not _action(tray, "全部停止").isEnabled()  # nothing runs
    rows = tray.menu.view.count()

    fake.set_state(alpha, LocalState.IDLE)
    fake.busy.add(beta)
    tray.menu.aboutToShow.emit()
    assert "停止 pc-alpha（空闲）" in _texts(tray)
    assert not _action(tray, "启动 pc-beta").isEnabled()
    assert _action(tray, "全部停止").isEnabled()
    assert tray.menu.view.count() == rows  # separators do not pile up

    _action(tray, "启动 pc-beta").trigger()  # disabled actions still trigger programmatically
    _action(tray, "停止 pc-alpha（空闲）").trigger()
    _action(tray, "全部启动").trigger()
    _action(tray, "全部停止").trigger()
    assert [name for name, _a, _k in fake.calls] == ["start", "stop", "start_all", "stop_all"]

    assert not window.isVisible()
    _action(tray, "打开主界面").trigger()
    assert window.isVisible()


def test_stopping_a_busy_runner_from_the_tray_asks_in_the_window(
    tray: TrayIcon,
    window: MainWindow,
    fake_with_records: FakeController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = fake_with_records
    alpha = fake.records[0].id
    fake.set_state(alpha, LocalState.BUSY, "build")
    asked: list[bool] = []

    def _exec(box: MessageBox) -> bool:
        asked.append(window.isVisible())  # the overlay's window is shown first
        return False

    monkeypatch.setattr(MessageBox, "exec", _exec)
    tray.rebuild_menu()
    stop = next(a for a in tray.menu_actions if a.text().startswith("停止 pc-alpha"))
    stop.trigger()
    _action(tray, "全部停止").trigger()
    assert asked == [True, True]
    assert not fake.calls_of("stop") and not fake.calls_of("stop_all")

    monkeypatch.setattr(MessageBox, "exec", lambda self: True)
    tray.stop(alpha)
    tray.stop_all()
    assert fake.calls_of("stop") == [((alpha,), {})]
    assert fake.calls_of("stop_all") == [((), {})]


def test_quit_from_the_tray(
    tray: TrayIcon, window: MainWindow, app_quits: list[bool], fake_with_records: FakeController
) -> None:
    tray.show()
    _action(tray, "退出").trigger()
    assert app_quits == [True]
    assert not tray.isVisible()
    assert not fake_with_records.calls_of("shutdown")  # nothing was running


def test_left_click_shows_window(tray: TrayIcon, window: MainWindow) -> None:
    tray.activated.emit(QSystemTrayIcon.Context)
    assert not window.isVisible()
    tray.activated.emit(QSystemTrayIcon.Trigger)
    assert window.isVisible()
    window.hide()
    tray.activated.emit(QSystemTrayIcon.DoubleClick)
    assert window.isVisible()


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("all", ["pc-alpha：job 成功", "pc-alpha：job 失败", "pc-alpha：job 已取消"]),
        ("failed", ["pc-alpha：job 失败"]),
        ("off", []),
    ],
)
def test_job_notifications_follow_settings(
    tray: TrayIcon,
    fake_with_records: FakeController,
    messages: list[Message],
    mode: str,
    expected: list[str],
) -> None:
    fake = fake_with_records
    fake.is_logged_in = False  # no GitHub follow-up: failures are notified right away
    _settings(fake, notify_jobs=mode)
    rid = fake.records[0].id
    fake.runner_job_finished.emit(rid, "build", "Succeeded")
    fake.runner_job_finished.emit(rid, "build", "Failed")
    fake.runner_job_finished.emit(rid, "build", "Canceled")
    assert [title for title, _t, _i in messages] == expected
    for title, text, icon in messages:
        assert text == "build"
        failed = "失败" in title
        assert icon == (QSystemTrayIcon.Critical if failed else QSystemTrayIcon.Information)


def test_abandoned_counts_as_failure(
    tray: TrayIcon, fake_with_records: FakeController, messages: list[Message]
) -> None:
    fake_with_records.is_logged_in = False
    rid = fake_with_records.records[0].id
    fake_with_records.runner_job_finished.emit(rid, "deploy", "abandoned")
    assert messages == [("pc-alpha：job 已放弃", "deploy", QSystemTrayIcon.Critical)]


def test_error_state_notifies_once(
    tray: TrayIcon, fake_with_records: FakeController, messages: list[Message]
) -> None:
    fake = fake_with_records
    rid = fake.records[0].id
    fake.set_display(rid, make_display(DisplayState.ERROR, detail="run.cmd 退出码 1"))
    fake.set_display(rid, make_display(DisplayState.ERROR, detail="run.cmd 退出码 1"))
    assert messages == [("pc-alpha 异常", "run.cmd 退出码 1", QSystemTrayIcon.Critical)]
    fake.set_display(rid, make_display(DisplayState.STOPPED))
    _settings(fake, notify_jobs="off")
    fake.set_display(rid, make_display(DisplayState.ERROR))
    assert len(messages) == 1


def test_notify_truncates(tray: TrayIcon, messages: list[Message]) -> None:
    tray.notify("t" * 100, "m" * 400)
    title, text, icon = messages[0]
    assert len(title) == TITLE_MAX and title.endswith("…")
    assert len(text) == MESSAGE_MAX and text.endswith("…")
    assert icon == QSystemTrayIcon.Information


def test_discovery_notifies_only_while_hidden_and_only_new(
    tray: TrayIcon, window: MainWindow, fake_with_records: FakeController, messages: list[Message]
) -> None:
    fake = fake_with_records
    root = Path("/tmp/runners")
    stray = StrayRunner(path=root / "ghost", pids=(7,), configured=True, service=False)
    found = Discovery(root=root, unregistered=(root / "old",), strays=(stray,))
    fake.discovery_updated.emit(found)  # hidden window
    assert len(messages) == 1
    assert "1 个未登记的 runner 目录" in messages[0][1]
    assert "1 个不属于 RunnerDeck 的 runner 进程" in messages[0][1]

    fake.discovery_updated.emit(found)  # nothing new
    fake.discovery_updated.emit(Discovery(root=root))
    assert len(messages) == 1

    window.show()
    fake.discovery_updated.emit(Discovery(root=root, unregistered=(root / "new",)))
    assert len(messages) == 1  # visible: the dashboard banner shows it
    window.hide()
    fake.discovery_updated.emit(Discovery(root=root, unregistered=(root / "new",)))
    assert len(messages) == 1  # already seen while visible


def test_tray_icon_standalone(qtbot: QtBot, window: MainWindow) -> None:
    """TrayIcon(controller, window) can be built on its own (lane D's smoke test)."""
    fake = FakeController(
        AppConfig(account="octocat", settings=AppSettings(), runners=[make_record()])
    )
    icon = TrayIcon(fake, window)
    assert icon.level is TrayLevel.STOPPED
    assert _texts(icon)[-1] == "退出"


def test_external_runner_offers_take_over_via_the_detail_page(
    tray: TrayIcon, window: MainWindow, fake_with_records: FakeController
) -> None:
    record = fake_with_records.records[0]
    fake_with_records.set_display(record.id, make_display(DisplayState.EXTERNAL))
    tray.rebuild_menu()
    assert f"启动 {record.name}" not in _texts(tray)
    _action(tray, f"接管 {record.name}…").trigger()
    assert window.stackedWidget.currentWidget() is window.detailPage
    assert fake_with_records.calls_of("start") == []  # nothing happens without confirmation


def test_failed_job_notification_waits_for_the_failed_step(
    tray: TrayIcon,
    fake_with_records: FakeController,
    messages: list[Message],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from runnerdeck.core.models import JobSnapshot, JobStep, WorkflowJob
    from runnerdeck.ui import tray as tray_module

    fake = fake_with_records
    first, second = (r.id for r in fake.records)
    step = JobStep(number=5, name="Install dependencies", status="completed", conclusion="failure")
    start = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)
    job = WorkflowJob(
        id=1, run_id=1, name="build", status="completed", conclusion="failure",
        started_at=start, completed_at=start.replace(minute=2), steps=[step],
    )  # fmt: skip
    fake.runner_job_finished.emit(first, "build", "Failed")
    assert messages == []  # waiting for GitHub
    fake.job_summarized.emit(first, JobSnapshot(job=job, errors=("exit code 1",)))
    assert messages == [
        (
            "pc-alpha：job 失败",
            "job build 失败（Install dependencies，耗时 2m00s）：exit code 1",
            QSystemTrayIcon.Critical,
        )
    ]
    fake.job_summarized.emit(first, JobSnapshot(job=job))  # nothing pending: no repeat
    assert len(messages) == 1

    monkeypatch.setattr(tray_module, "FAILURE_WAIT_MS", 10)
    fake.runner_job_finished.emit(second, "deploy", "Failed")
    qtbot.waitUntil(lambda: len(messages) == 2, timeout=2000)
    assert messages[-1][1] == "deploy"  # GitHub never answered: the job name alone
