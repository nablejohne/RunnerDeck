"""Edit one runner's `.env` (runner card menu → 编辑环境变量)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    MessageBoxBase,
    PlainTextEdit,
    PushButton,
    SubtitleLabel,
)

from runnerdeck.core.errors import InvalidInputError

ERROR_COLOR = ("#c42b1c", "#ff99a4")  # light, dark
HINT_TEXT = "每行一个 KEY=VALUE，不支持注释；runner 启动时读取，改完要重启 runner 才生效"
PLACEHOLDER = "例如\nGIT_CONFIG_COUNT=1\nGIT_CONFIG_KEY_0=core.fsmonitor\nGIT_CONFIG_VALUE_0=false"


def managed_note(keys: Sequence[str]) -> str:
    return (
        f"由 RunnerDeck 管理：{'、'.join(keys)}。改成别的值后，设置里的代理 / pip 镜像源"
        "不再覆盖它；删掉的行会在下次启动时按设置补回，要去掉请在设置里清空"
    )


class EnvEditDialog(MessageBoxBase):
    """`check(text)` raises InvalidInputError for a bad edit (the dialog then stays open).
    After exec(): `text()` and `restart` (True when 保存并重启 was used)."""

    def __init__(
        self,
        runner_name: str,
        env_path: Path,
        text: str,
        managed_keys: Sequence[str],
        *,
        can_restart: bool,
        check: Callable[[str], None],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._check = check
        self.restart = False

        self.titleLabel = SubtitleLabel(f"{runner_name} 的环境变量", self)
        self.pathLabel = CaptionLabel(str(env_path), self)
        self.pathLabel.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.hintLabel = BodyLabel(HINT_TEXT, self)
        self.hintLabel.setWordWrap(True)
        self.editor = PlainTextEdit(self)
        self.editor.setPlainText(text)
        self.editor.setPlaceholderText(PLACEHOLDER)
        self.editor.setLineWrapMode(PlainTextEdit.NoWrap)
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        self.editor.setFont(font)
        self.editor.setMinimumHeight(220)
        self.managedLabel = CaptionLabel(managed_note(managed_keys), self)
        self.managedLabel.setWordWrap(True)
        self.errorLabel = CaptionLabel("", self)
        self.errorLabel.setTextColor(*ERROR_COLOR)
        self.errorLabel.setWordWrap(True)
        self.errorLabel.hide()

        for widget in (
            self.titleLabel,
            self.pathLabel,
            self.hintLabel,
            self.editor,
            self.managedLabel,
            self.errorLabel,
        ):
            self.viewLayout.addWidget(widget)
        self.widget.setMinimumWidth(560)

        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.restartButton: PushButton | None = None
        if can_restart:
            self.restartButton = PushButton("保存并重启", self.buttonGroup)
            self.buttonLayout.insertWidget(1, self.restartButton, 1, Qt.AlignVCenter)
            self.restartButton.clicked.connect(self._save_and_restart)

    def text(self) -> str:
        return self.editor.toPlainText()

    def validate(self) -> bool:
        try:
            self._check(self.text())
        except InvalidInputError as e:
            self.errorLabel.setText(str(e))
            self.errorLabel.show()
            return False
        self.errorLabel.hide()
        return True

    def _save_and_restart(self) -> None:
        if self.validate():
            self.restart = True
            self.accept()
