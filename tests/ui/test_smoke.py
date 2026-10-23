"""MainWindow builds offscreen, switches pages, prompts for login and closes cleanly."""

from __future__ import annotations

import pytest
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
