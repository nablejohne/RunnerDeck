"""PAT login dialog (docs/m2-design.md §5).

The dialog never inspects the token beyond "is it empty": format validation and the GitHub
round-trip happen in `controller.login`, whose `failed("login", message)` already comes
redacted and never echoes the input. `logged_in` closes the dialog.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from PyQt5.QtCore import QUrl
from PyQt5.QtWidgets import QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    HyperlinkLabel,
    MessageBoxBase,
    PasswordLineEdit,
    SubtitleLabel,
)

from runnerdeck.core.account import validate_pat_format
from runnerdeck.core.errors import InvalidInputError

if TYPE_CHECKING:
    from runnerdeck.app.controller import RunnerController

NEW_TOKEN_URL = "https://github.com/settings/personal-access-tokens/new"
PERMISSION_HELP = (
    "需要的权限：\n"
    "• fine-grained PAT：Repository access 选中要添加 runner 的仓库，"
    "Repository permissions 里 Administration: Read and write（推荐）\n"
    "• classic PAT：勾选 repo"
)
EMPTY_INPUT = "请输入 PAT"
ERROR_COLOR = ("#c42b1c", "#ff99a4")


class LoginDialog(MessageBoxBase):
    """Ask for a PAT and log in through the controller."""

    def __init__(self, controller: RunnerController, parent: QWidget) -> None:
        super().__init__(parent)
        self._controller = controller
        self._pending = False

        self.titleLabel = SubtitleLabel("登录 GitHub", self)
        self.hintLabel = BodyLabel(
            "粘贴一个 Personal Access Token（只保存在系统凭据管理器里）", self
        )
        self.hintLabel.setWordWrap(True)
        self.patEdit = PasswordLineEdit(self)
        self.patEdit.setPlaceholderText("github_pat_… 或 ghp_…")
        self.patEdit.setClearButtonEnabled(True)
        self.linkLabel = HyperlinkLabel(self)
        self.linkLabel.setText("在 GitHub 创建新的 token")
        self.linkLabel.setUrl(QUrl(NEW_TOKEN_URL))
        self.permissionLabel = BodyLabel(PERMISSION_HELP, self)
        self.permissionLabel.setWordWrap(True)
        self.errorLabel = CaptionLabel("", self)
        self.errorLabel.setTextColor(*ERROR_COLOR)
        self.errorLabel.setWordWrap(True)
        self.errorLabel.hide()

        for widget in (
            self.titleLabel,
            self.hintLabel,
            self.patEdit,
            self.linkLabel,
            self.permissionLabel,
            self.errorLabel,
        ):
            self.viewLayout.addWidget(widget)
        self.widget.setMinimumWidth(480)

        self.yesButton.setText("登录")
        self.cancelButton.setText("取消")
        self.patEdit.returnPressed.connect(self.yesButton.click)
        self.patEdit.textChanged.connect(lambda _text: self.show_error(""))

        controller.logged_in.connect(self._on_logged_in)
        controller.failed.connect(self._on_failed)

    @staticmethod
    def ask(controller: RunnerController, parent: QWidget) -> bool:
        """Modal login; True when the user logged in, False when they cancelled.

        `parent` is the window the mask covers (MessageBoxBase requires one).
        """
        dialog = LoginDialog(controller, parent)
        return bool(dialog.exec())

    @property
    def pending(self) -> bool:
        """A login request is in flight."""
        return self._pending

    def show_error(self, message: str) -> None:
        self.errorLabel.setText(message)
        self.errorLabel.setVisible(bool(message))

    def validate(self) -> bool:
        """Yes button: submit instead of closing; the dialog closes on `logged_in`."""
        if self._pending:
            return False
        pat = self.patEdit.text().strip()
        if not pat:
            self.show_error(EMPTY_INPUT)
            return False
        try:
            pat = validate_pat_format(pat)  # never echoes the input (secrets.validate_pat_format)
        except InvalidInputError as e:
            self.show_error(str(e))
            return False
        self._set_pending(True)
        self._controller.login(pat)
        return False

    def _set_pending(self, pending: bool) -> None:
        self._pending = pending
        self.yesButton.setEnabled(not pending)
        self.patEdit.setEnabled(not pending)
        self.yesButton.setText("登录中…" if pending else "登录")

    def _on_logged_in(self, _user: object) -> None:
        self._set_pending(False)
        self.patEdit.clear()
        self.accept()

    def _on_failed(self, op: str, message: str, _exc: object = None) -> None:
        if op != "login":
            return
        self._set_pending(False)
        self.show_error(message)

    def done(self, code: int) -> None:
        for signal, slot in (
            (self._controller.logged_in, self._on_logged_in),
            (self._controller.failed, self._on_failed),
        ):
            with contextlib.suppress(TypeError):
                signal.disconnect(slot)
        self.patEdit.clear()  # do not keep the secret around in a hidden widget
        super().done(code)
