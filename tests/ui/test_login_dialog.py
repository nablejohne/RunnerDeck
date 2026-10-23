"""LoginDialog: empty input, controller-side format error, success closes the dialog."""

from __future__ import annotations

import pytest
from PyQt5.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from runnerdeck.ui.components.login_dialog import EMPTY_INPUT, NEW_TOKEN_URL, LoginDialog
from ui.conftest import FakeController, make_user

FAKE_PAT = "ghp_" + "x" * 40  # obviously fake; never a real token
FORMAT_ERROR = "PAT 格式不对：应以 github_pat_、ghp_、gho_ 或 ghu_ 开头"


@pytest.fixture
def host(qtbot: QtBot) -> QWidget:
    """MaskDialogBase dialogs need a parent to cover; a plain widget stands in for the window."""
    widget = QWidget()
    qtbot.addWidget(widget)
    return widget


@pytest.fixture
def dialog(fake: FakeController, host: QWidget) -> LoginDialog:
    return LoginDialog(fake, host)


def test_empty_input_shows_message_without_calling_controller(
    dialog: LoginDialog, fake: FakeController
) -> None:
    dialog.yesButton.click()
    assert dialog.errorLabel.text() == EMPTY_INPUT
    assert dialog.errorLabel.isVisibleTo(dialog)
    assert not fake.calls
    assert dialog.linkLabel.getUrl().toString() == NEW_TOKEN_URL


def test_bad_format_is_rejected_locally_without_echoing(
    dialog: LoginDialog, fake: FakeController
) -> None:
    dialog.patEdit.setText("my token name")
    dialog.validate()
    assert fake.calls_of("login") == []  # never reaches the controller / the network
    assert dialog.errorLabel.text().startswith(FORMAT_ERROR)
    assert "my token name" not in dialog.errorLabel.text()
    assert not dialog.pending and dialog.yesButton.isEnabled()


def test_controller_failure_is_shown_and_other_ops_are_ignored(
    dialog: LoginDialog, fake: FakeController
) -> None:
    dialog.patEdit.setText(FAKE_PAT)
    dialog.validate()
    assert fake.calls_of("login") == [((FAKE_PAT,), {})]
    assert dialog.pending and not dialog.yesButton.isEnabled()

    fake.failed.emit("login", "PAT 无效或已过期")
    assert dialog.errorLabel.text() == "PAT 无效或已过期"
    assert not dialog.pending and dialog.yesButton.isEnabled()
    assert dialog.yesButton.text() == "登录"

    fake.failed.emit("start", "unrelated")  # other ops are ignored
    assert dialog.errorLabel.text() == "PAT 无效或已过期"


def test_success_accepts_and_clears_input(
    qtbot: QtBot, dialog: LoginDialog, fake: FakeController
) -> None:
    dialog.patEdit.setText(FAKE_PAT)
    dialog.patEdit.returnPressed.emit()
    assert fake.calls_of("login") == [((FAKE_PAT,), {})]
    with qtbot.waitSignal(dialog.accepted):
        fake.logged_in.emit(make_user())
    assert dialog.patEdit.text() == ""
    assert dialog.result() == LoginDialog.Accepted
    fake.logged_in.emit(make_user())  # disconnected after close: no error, no second accept


def test_typing_clears_error(dialog: LoginDialog) -> None:
    dialog.show_error("boom")
    dialog.patEdit.setText("g")
    assert dialog.errorLabel.text() == ""


def test_ask_returns_dialog_result(
    fake: FakeController, host: QWidget, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LoginDialog, "exec", lambda self: 1)
    assert LoginDialog.ask(fake, host) is True
    monkeypatch.setattr(LoginDialog, "exec", lambda self: 0)
    assert LoginDialog.ask(fake, host) is False
