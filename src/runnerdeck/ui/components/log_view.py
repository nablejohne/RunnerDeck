"""Read-only log text shared by the detail page's live output and `_diag` tabs.

Monospace PlainTextEdit, an "自动滚动" checkbox (on by default) and a "复制全部" button. The
widget never talks to the controller: the page feeds it lines or a whole text.
"""

from __future__ import annotations

from collections.abc import Iterable

from PyQt5.QtGui import QFont, QFontDatabase
from PyQt5.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CheckBox, PlainTextEdit, PushButton
from qfluentwidgets import FluentIcon as FIF


class LogView(QWidget):
    """Monospace, read-only text view with auto-scroll and copy-all.

    max_lines > 0 keeps only the newest lines (live output); 0 keeps everything (a `_diag`
    file, already tail-truncated by core/diag.py).
    """

    def __init__(self, parent: QWidget | None = None, *, max_lines: int = 0) -> None:
        super().__init__(parent)
        self.textEdit = PlainTextEdit(self)
        self.textEdit.setReadOnly(True)
        self.textEdit.setLineWrapMode(PlainTextEdit.NoWrap)
        self.textEdit.setMaximumBlockCount(max(0, max_lines))
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        font.setStyleHint(QFont.Monospace)
        self.textEdit.setFont(font)
        self.autoScrollBox = CheckBox("自动滚动", self)
        self.autoScrollBox.setChecked(True)
        self.copyButton = PushButton(FIF.COPY, "复制全部", self)
        self.copyButton.clicked.connect(self.copy_all)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.textEdit, 1)
        bottom = QHBoxLayout()
        bottom.addWidget(self.autoScrollBox)
        bottom.addStretch(1)
        bottom.addWidget(self.copyButton)
        layout.addLayout(bottom)

    def text(self) -> str:
        return self.textEdit.toPlainText()

    def set_lines(self, lines: Iterable[str]) -> None:
        self.set_text("\n".join(lines))

    def set_text(self, text: str) -> None:
        self.textEdit.setPlainText(text)
        self._follow()

    def append_line(self, line: str) -> None:
        self.textEdit.appendPlainText(line)
        self._follow()

    def clear(self) -> None:
        self.textEdit.clear()

    def copy_all(self) -> None:
        QApplication.clipboard().setText(self.text())

    def _follow(self) -> None:
        if self.autoScrollBox.isChecked():
            bar = self.textEdit.verticalScrollBar()
            bar.setValue(bar.maximum())
