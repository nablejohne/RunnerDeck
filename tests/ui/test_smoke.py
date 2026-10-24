"""MainWindow builds offscreen, switches pages, prompts for login, closes / quits cleanly,
hides to the tray and routes feedback to tray notifications while hidden."""

from __future__ import annotations

import pytest
from PyQt5.QtWidgets import QApplication
from pytestqt.qtbot import QtBot
from qfluentwidgets import MessageBox

from runnerdeck.core.models import AppConfig, LocalState
from runnerdeck.ui import main_window
from runnerdeck.ui.components.login_dialog import LoginDialog
from runnerdeck.ui.main_window import FAILED_TITLES, MainWindow, exit_question
from ui.conftest import FakeController, make_record


@pytest.fixture
def window(qtbot: QtBot, fake: FakeController) -> MainWindow:
    win = MainWindow(fake)
    qtbot.addWidget(win)
    return win


def test_window_has_three_pages_and_switches(window: MainWindow) -> None:
    assert window.windowTitle() == "RunnerDeck"
    names = {
        page.objectName() for page in (window.dashboardPage, window.addPage, window.settingsPage)
    }
    assert names == {"dashboard", "add", "settings"}
    for page in (window.addPage, window.settingsPage, window.dashboardPage):
        window.switchTo(page)
        assert window.stackedWidget.currentWidget() is page


def test_close_without_running_runners_accepts(window: MainWindow, fake: FakeController) -> None:
    window.show()
    assert window.close()
    assert not fake.calls_of("shutdown")


def test_close_cancelled_keeps_window(
    qtbot: QtBot, fake_with_records: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_with_records
    fake.set_state(fake.records[0].id, LocalState.IDLE)
    window = MainWindow(fake)
    qtbot.addWidget(window)
    window.show()
    questions: list[str] = []
    original_init = MessageBox.__init__

    def _init(self: MessageBox, title: str, content: str, parent=None) -> None:  # type: ignore[no-untyped-def]
        questions.append(content)
        original_init(self, title, content, parent)

    monkeypatch.setattr(MessageBox, "__init__", _init)
    monkeypatch.setattr(MessageBox, "exec", lambda self: False)
    assert not window.close()
    assert window.isVisible()
    assert questions == [exit_question(1)]
    assert not fake.calls_of("shutdown")


def test_close_confirmed_shuts_down_then_accepts(
    qtbot: QtBot, fake_with_records: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_with_records
    fake.set_state(fake.records[0].id, LocalState.BUSY, "build")
    fake.shutdown_leftovers = ["pc-alpha"]
    window = MainWindow(fake)
    qtbot.addWidget(window)
    window.show()
    monkeypatch.setattr(MessageBox, "exec", lambda self: True)
    assert window.close()
    assert fake.calls_of("shutdown") == [((), {"timeout": None})]


def test_prompts_login_on_first_show_when_logged_out(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeController(AppConfig(), logged_in=False)
    asked: list[object] = []
    monkeypatch.setattr(
        LoginDialog, "ask", staticmethod(lambda controller, parent: asked.append(parent) or True)
    )
    window = MainWindow(fake)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: len(asked) == 1)
    window.hide()
    window.show()  # only the first show prompts
    qtbot.wait(20)
    assert asked == [window]


def test_logged_in_window_does_not_prompt(window: MainWindow, qtbot: QtBot, monkeypatch) -> None:
    asked: list[object] = []
    monkeypatch.setattr(LoginDialog, "ask", staticmethod(lambda *a: asked.append(a) or True))
    window.show()
    qtbot.wait(20)
    assert not asked


def test_failed_shows_error_infobar_with_mapped_title(
    window: MainWindow, fake: FakeController, no_infobars: list[tuple[str, str, str]]
) -> None:
    fake.failed.emit("start", "启动失败：目录不存在")
    fake.failed.emit("something-new", "内部错误")
    assert no_infobars == [
        ("error", FAILED_TITLES["start"], "启动失败：目录不存在"),
        ("error", main_window.DEFAULT_FAILED_TITLE, "内部错误"),
    ]


def test_wizard_finished_switches_to_dashboard(window: MainWindow) -> None:
    window.switchTo(window.addPage)
    window.addPage.finished.emit()
    assert window.stackedWidget.currentWidget() is window.dashboardPage


def test_runner_removed_and_forced_stop_feedback(
    window: MainWindow, fake: FakeController, no_infobars: list[tuple[str, str, str]]
) -> None:
    class Result:
        record = make_record("pc-gone", "gone")
        warnings = ("目录还在",)

    class Stop:
        graceful = False

    fake.runner_removed.emit("id", Result())
    fake.runner_stopped.emit("id", Stop())
    levels = [level for level, _t, _c in no_infobars]
    assert levels == ["warning", "warning"]
    assert "pc-gone" in no_infobars[0][1]


def test_prompt_login_can_be_disabled(qtbot: QtBot, monkeypatch) -> None:
    from runnerdeck.ui.components import login_dialog

    calls: list[object] = []
    monkeypatch.setattr(login_dialog.LoginDialog, "ask", staticmethod(lambda *a: calls.append(a)))
    fake = FakeController(AppConfig(), logged_in=False)
    window = MainWindow(fake, prompt_login=False)
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(20)
    assert calls == []


# --- M3: detail page, tray, close-to-tray, quit, session end, theme ---------------------------


@pytest.fixture
def tray_window(qtbot: QtBot, fake_with_records: FakeController) -> MainWindow:
    win = MainWindow(fake_with_records, prompt_login=False, tray=True)
    qtbot.addWidget(win)
    return win


@pytest.fixture
def tray_messages(
    tray_window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, str, object]]:
    shown: list[tuple[str, str, object]] = []
    assert tray_window.tray is not None
    monkeypatch.setattr(
        tray_window.tray,
        "showMessage",
        lambda title, text, icon, _ms=0: shown.append((title, text, icon)),
    )
    return shown


def test_detail_page_has_no_navigation_item(window: MainWindow, fake: FakeController) -> None:
    assert window.detailPage.objectName() == "detail"
    assert window.stackedWidget.indexOf(window.detailPage) >= 0
    assert "detail" not in window.navigationInterface.panel.items
    window.switchTo(window.detailPage)  # empty state, no record: must not crash
    assert window.stackedWidget.currentWidget() is window.detailPage
    assert window.detailPage.emptyLabel.isVisibleTo(window.detailPage)


def test_open_detail_from_card_and_back(qtbot: QtBot, fake_with_records: FakeController) -> None:
    fake = fake_with_records
    window = MainWindow(fake, prompt_login=False)
    qtbot.addWidget(window)
    rid = fake.records[1].id
    window.switchTo(window.settingsPage)
    window.dashboardPage.cards[rid].clicked.emit()
    assert window.stackedWidget.currentWidget() is window.detailPage
    assert window.navigationInterface.panel.currentItem().property("routeKey") == "dashboard"
    assert window.detailPage.record_id == rid
    window.detailPage.backButton.click()
    assert window.stackedWidget.currentWidget() is window.dashboardPage


def test_show_and_raise_brings_back_a_hidden_window(tray_window: MainWindow) -> None:
    assert not tray_window.isVisible()
    tray_window.show_and_raise()
    assert tray_window.isVisible() and not tray_window.in_background
    tray_window.showMinimized()
    assert tray_window.in_background
    tray_window.show_and_raise()
    assert not tray_window.isMinimized()


def test_close_to_tray_hides_and_hints_once(
    tray_window: MainWindow,
    fake_with_records: FakeController,
    tray_messages: list[tuple[str, str, object]],
    app_quits: list[bool],
) -> None:
    fake_with_records.set_state(fake_with_records.records[0].id, LocalState.IDLE)
    tray_window.show()
    assert not tray_window.close()  # ignored: only hidden
    assert not tray_window.isVisible()
    assert [title for title, _t, _i in tray_messages] == [main_window.BACKGROUND_TITLE]
    tray_window.show()
    tray_window.close()
    assert len(tray_messages) == 1  # first time only
    assert not fake_with_records.calls_of("shutdown") and not app_quits


def test_close_without_close_to_tray_quits(
    tray_window: MainWindow,
    fake_with_records: FakeController,
    monkeypatch: pytest.MonkeyPatch,
    app_quits: list[bool],
) -> None:
    fake = fake_with_records
    fake.config.settings = fake.config.settings.model_copy(update={"close_to_tray": False})
    fake.set_state(fake.records[0].id, LocalState.BUSY, "build")
    questions: list[str] = []
    original_init = MessageBox.__init__

    def _init(self: MessageBox, title: str, content: str, parent=None) -> None:  # type: ignore[no-untyped-def]
        questions.append(content)
        original_init(self, title, content, parent)

    monkeypatch.setattr(MessageBox, "__init__", _init)
    monkeypatch.setattr(MessageBox, "exec", lambda self: True)
    assert tray_window.tray is not None
    tray_window.tray.show()
    tray_window.show()
    assert tray_window.close()
    assert questions == [exit_question(1, ["pc-alpha（build）"])]
    assert "正在执行 job：pc-alpha（build）" in questions[0]
    assert fake.calls_of("shutdown") == [((), {"timeout": None})]
    assert app_quits == [True]
    assert not tray_window.tray.isVisible()
    assert tray_window.quit_app()  # already quitting: no second shutdown
    assert len(fake.calls_of("shutdown")) == 1


def test_quit_app_cancelled_from_hidden_window_shows_it_first(
    tray_window: MainWindow,
    fake_with_records: FakeController,
    monkeypatch: pytest.MonkeyPatch,
    app_quits: list[bool],
) -> None:
    fake_with_records.set_state(fake_with_records.records[0].id, LocalState.IDLE)
    visible_when_asked: list[bool] = []

    def _exec(box: MessageBox) -> bool:
        visible_when_asked.append(tray_window.isVisible())
        return False

    monkeypatch.setattr(MessageBox, "exec", _exec)
    assert not tray_window.quit_app()
    assert visible_when_asked == [True]
    assert not fake_with_records.calls_of("shutdown") and not app_quits


def test_session_end_accepts_close_and_signals_runners(
    tray_window: MainWindow,
    fake_with_records: FakeController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = fake_with_records
    fake.set_state(fake.records[0].id, LocalState.IDLE)
    fake.emergency_count = 1
    tray_window.on_session_end(object())
    assert fake.calls_of("emergency_stop_all") == [((), {})]
    asked: list[object] = []
    monkeypatch.setattr(MessageBox, "exec", lambda self: asked.append(self) or False)
    monkeypatch.setattr(QApplication, "isSavingSession", lambda self: True)
    tray_window.show()
    assert tray_window.close()  # no question, no hide-to-tray
    assert not asked and not fake.calls_of("shutdown")


def test_session_end_survives_controller_errors(
    window: MainWindow, fake: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> int:
        raise RuntimeError("bug")

    monkeypatch.setattr(fake, "emergency_stop_all", _boom)
    window.on_session_end()  # logged, never raised


def test_failures_go_to_the_tray_while_hidden(
    tray_window: MainWindow,
    fake_with_records: FakeController,
    tray_messages: list[tuple[str, str, object]],
    no_infobars: list[tuple[str, str, str]],
) -> None:
    from PyQt5.QtWidgets import QSystemTrayIcon

    class Stop:
        graceful = False

    fake = fake_with_records
    fake.failed.emit("import", "不是 runner 目录")  # hidden
    fake.runner_stopped.emit(fake.records[0].id, Stop())
    assert tray_messages == [
        (FAILED_TITLES["import"], "不是 runner 目录", QSystemTrayIcon.Critical),
        ("已强制停止", main_window.FORCED_STOP_TEXT, QSystemTrayIcon.Warning),
    ]
    assert no_infobars == []

    tray_window.show()
    fake.failed.emit("kill", "拒绝访问")
    assert no_infobars == [("error", FAILED_TITLES["kill"], "拒绝访问")]
    tray_window.showMinimized()
    fake.failed.emit("take_over", "失败")
    assert tray_messages[-1][0] == FAILED_TITLES["take_over"]
    assert len(no_infobars) == 1


def test_m3_failed_titles() -> None:
    expected = {
        "import": "导入失败",
        "forget": "取消登记失败",
        "discover": "扫描失败",
        "kill": "结束进程失败",
        "take_over": "接管失败",
        "diag": "读取日志失败",
        "autostart": "自动启动",
        "launch_on_login": "自启设置失败",
        "auto_start": "保存失败",
    }
    assert {op: FAILED_TITLES[op] for op in expected} == expected


def test_other_m3_feedback(
    window: MainWindow, fake: FakeController, no_infobars: list[tuple[str, str, str]]
) -> None:
    from runnerdeck.core.process import ExternalStop

    window.show()
    fake.runner_imported.emit(make_record("pc-old", "old"))
    fake.runner_forgotten.emit("id")
    fake.external_stopped.emit("id", ExternalStop(killed=[1, 2], survivors=[]))
    fake.external_stopped.emit("id", ExternalStop(killed=[], survivors=[3]))
    assert [(level, title) for level, title, _c in no_infobars] == [
        ("success", "已导入"),
        ("success", "已取消登记"),
        ("success", "已结束进程"),
        ("warning", "有进程没有结束"),
    ]
    assert "3" in no_infobars[-1][2]


def test_theme_change_is_applied(
    window: MainWindow, fake: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    applied: list[str] = []
    monkeypatch.setattr(main_window, "apply_theme", applied.append)
    fake.save_settings(fake.config.settings.model_copy(update={"proxy": "http://h:1"}))
    assert applied == []  # theme unchanged
    fake.save_settings(fake.config.settings.model_copy(update={"theme": "dark"}))
    fake.save_settings(fake.config.settings.model_copy(update={"theme": "dark"}))
    assert applied == ["dark"]


def test_apply_theme_sets_qconfig() -> None:
    from qfluentwidgets import Theme, qconfig

    from runnerdeck.ui.theme import THEMES, apply_theme

    assert set(THEMES) == {"auto", "light", "dark"}
    before = qconfig.themeMode.value
    try:
        apply_theme("dark")
        assert qconfig.themeMode.value == Theme.DARK
        apply_theme("light")
        assert qconfig.themeMode.value == Theme.LIGHT
        apply_theme("bogus")
        assert qconfig.themeMode.value == Theme.AUTO
    finally:
        apply_theme({Theme.DARK: "dark", Theme.AUTO: "auto"}.get(before, "light"))
