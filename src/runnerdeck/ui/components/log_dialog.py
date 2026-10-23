"""Live output of one runner process (docs/m2-design.md §5).

Starts with `controller.output(record_id)` and appends every `runner_line` for the same id
while open. The dialog is modeless in spirit: the controller keeps running either way.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from PyQt5.QtGui import QFont, QFontDatabase
from PyQt5.QtWidgets import QApplication, QHBoxLayout, QWidget
from qfluentwidgets import CheckBox, MessageBoxBase, PlainTextEdit, PushButton, SubtitleLabel

if TYPE_CHECKING:
    from runnerdeck.app.controller import RunnerController


class LogDialog(MessageBoxBase):
    """Read-only, monospace view of a runner's output with auto-scroll."""

    def __init__(
        self,
        controller: RunnerController,
        record_id: str,
        runner_name: str,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self.record_id = record_id

        self.titleLabel = SubtitleLabel(f"{runner_name} 的输出", self)
        self.textEdit = PlainTextEdit(self)
        self.textEdit.setReadOnly(True)
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        font.setStyleHint(QFont.Monospace)
        self.textEdit.setFont(font)
        self.textEdit.setPlainText("\n".join(controller.output(record_id)))
        self.textEdit.setMinimumSize(640, 360)
        self.autoScrollBox = CheckBox("自动滚动", self)
        self.autoScrollBox.setChecked(True)

        self.copyButton = PushButton("复制全部", self)
        self.copyButton.clicked.connect(self.copy_all)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.textEdit, 1)
        bottom = QHBoxLayout()
        bottom.addWidget(self.autoScrollBox)
        bottom.addStretch(1)
        self.viewLayout.addLayout(bottom)

        self.hideYesButton()
        self.cancelButton.setText("关闭")
        self.buttonLayout.insertWidget(1, self.copyButton, 1)

        controller.runner_line.connect(self._on_line)
        self._scroll_to_end()

    def _on_line(self, record_id: str, line: str) -> None:
        if record_id != self.record_id:
            return
        self.textEdit.appendPlainText(line)
        if self.autoScrollBox.isChecked():
            self._scroll_to_end()

    def _scroll_to_end(self) -> None:
        bar = self.textEdit.verticalScrollBar()
        bar.setValue(bar.maximum())

    def copy_all(self) -> None:
        QApplication.clipboard().setText(self.textEdit.toPlainText())

    def done(self, code: int) -> None:
        with contextlib.suppress(TypeError):  # already disconnected
            self._controller.runner_line.disconnect(self._on_line)
        super().done(code)
