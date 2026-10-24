"""LogView: read-only monospace text with auto-scroll, copy-all and an optional line cap."""

from __future__ import annotations

from PyQt5.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from runnerdeck.ui.components.log_view import LogView


def test_set_append_copy(qtbot: QtBot) -> None:
    view = LogView()
    qtbot.addWidget(view)
    assert view.textEdit.isReadOnly()
    assert view.autoScrollBox.isChecked()
    view.set_lines(["line 1", "line 2"])
    view.append_line("line 3")
    assert view.text() == "line 1\nline 2\nline 3"
    view.copy_all()
    assert QApplication.clipboard().text() == "line 1\nline 2\nline 3"
    view.set_text("other")
    assert view.text() == "other"
    view.clear()
    assert view.text() == ""


def test_auto_scroll_follows_the_end_only_when_checked(qtbot: QtBot) -> None:
    view = LogView()
    qtbot.addWidget(view)
    view.resize(400, 120)
    view.show()
    view.set_lines([f"line {i}" for i in range(500)])
    bar = view.textEdit.verticalScrollBar()
    assert bar.maximum() > 0 and bar.value() == bar.maximum()

    view.autoScrollBox.setChecked(False)
    bar.setValue(0)
    view.append_line("more")
    assert bar.value() == 0


def test_max_lines_keeps_the_newest(qtbot: QtBot) -> None:
    view = LogView(max_lines=3)
    qtbot.addWidget(view)
    for i in range(5):
        view.append_line(f"line {i}")
    assert view.text() == "line 2\nline 3\nline 4"
    unlimited = LogView()
    qtbot.addWidget(unlimited)
    unlimited.set_lines(str(i) for i in range(10))
    assert unlimited.text().count("\n") == 9
