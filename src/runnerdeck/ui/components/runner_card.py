"""One runner on the dashboard: name, repo, status badge, start/stop button and a menu.

The card never talks to the controller itself: it emits `*_requested` signals carrying the
record id and the dashboard page forwards them (docs/m2-design.md §5).
"""

from __future__ import annotations

from PyQt5.QtCore import QPoint, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    IndeterminateProgressRing,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    RoundMenu,
    StrongBodyLabel,
    SubtitleLabel,
    TransparentToolButton,
)
from qfluentwidgets import FluentIcon as FIF

from runnerdeck.core.models import DisplayState, LocalState, RunnerRecord
from runnerdeck.core.status import DisplayStatus
from runnerdeck.ui.components.status_badge import StatusBadge, local_to_display

# Local states in which our process is alive (the button then offers "停止").
RUNNING_STATES = frozenset(
    {
        LocalState.STARTING,
        LocalState.RECONNECTING,
        LocalState.IDLE,
        LocalState.BUSY,
        LocalState.STOPPING,
    }
)
RUNNERS_SETTINGS_PATH = "/settings/actions/runners"


class RemoveRunnerDialog(MessageBoxBase):
    """Confirm removing a runner; offers to keep the `_work` directory."""

    def __init__(self, record: RunnerRecord, *, running: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel(f"移除 runner {record.name}", self)
        lines = [
            f"将从 GitHub 仓库 {record.full_name} 注销这个 runner，并删除本地目录：",
            record.path,
        ]
        if running:
            lines.append("它正在运行，移除前会先停止（执行中的 job 会被取消）。")
        self.bodyLabel = BodyLabel("\n".join(lines), self)
        self.bodyLabel.setWordWrap(True)
        self.keepWorkBox = CheckBox("保留 _work 目录（检出的代码和构建产物）", self)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.bodyLabel)
        self.viewLayout.addWidget(self.keepWorkBox)
        self.yesButton.setText("移除")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(420)

    @property
    def keep_work(self) -> bool:
        return self.keepWorkBox.isChecked()


class RunnerCard(CardWidget):
    """Dashboard card for one RunnerRecord.

    Signals carry the record id; `remove_requested` also carries keep_work.
    """

    start_requested = pyqtSignal(str)
    stop_requested = pyqtSignal(str)
    remove_requested = pyqtSignal(str, bool)
    logs_requested = pyqtSignal(str)
    open_dir_requested = pyqtSignal(str)

    def __init__(self, record: RunnerRecord, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.record = record
        self._running = False
        self._busy = False
        self._state = LocalState.STOPPED
        self._job: str | None = None

        self.nameLabel = StrongBodyLabel(record.name, self)
        self.repoLabel = CaptionLabel(record.full_name, self)
        self.badge = StatusBadge(self)
        self.detailLabel = CaptionLabel("", self)
        self.detailLabel.setWordWrap(True)
        self.detailLabel.hide()

        self.startButton = PrimaryPushButton("启动", self)
        self.stopButton = PushButton("停止", self)
        self.stopButton.hide()
        self.spinner = IndeterminateProgressRing(self, start=False)
        self.spinner.setFixedSize(16, 16)
        self.spinner.setStrokeWidth(2)
        self.spinner.hide()
        self.moreButton = TransparentToolButton(FIF.MORE, self)
        self.moreButton.setToolTip("更多操作")

        self.startButton.clicked.connect(lambda: self.start_requested.emit(self.record.id))
        self.stopButton.clicked.connect(lambda: self.stop_requested.emit(self.record.id))
        self.moreButton.clicked.connect(self._show_menu)

        self._build_layout()
        self.update_state(LocalState.STOPPED, None)

    def _build_layout(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 12, 12, 12)
        outer.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(self.nameLabel)
        head.addWidget(self.badge)
        head.addStretch(1)
        left.addLayout(head)
        left.addWidget(self.repoLabel)
        left.addWidget(self.detailLabel)
        outer.addLayout(left, 1)

        outer.addWidget(self.spinner)
        outer.addWidget(self.startButton)
        outer.addWidget(self.stopButton)
        outer.addWidget(self.moreButton)

    # --- state ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def display_state(self) -> DisplayState:
        return self.badge.state

    def update_state(self, state: LocalState, job: str | None) -> None:
        """Local process state changed (controller.runner_state)."""
        self._state = state
        self._job = job
        self._running = state in RUNNING_STATES
        self.badge.set_state(local_to_display(state), job)
        self._set_detail(None)
        self._refresh_buttons()

    def update_display(self, display: DisplayStatus) -> None:
        """Merged local + GitHub status arrived (controller.statuses_updated)."""
        self.badge.set_display(display)
        self._set_detail(display.detail)

    def set_busy(self, busy: bool) -> None:
        """An operation (start / stop / remove) is in flight for this runner."""
        self._busy = busy
        self.spinner.setVisible(busy)
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()
        self._refresh_buttons()

    def _set_detail(self, detail: str | None) -> None:
        self.detailLabel.setText(detail or "")
        self.detailLabel.setVisible(bool(detail))

    def _refresh_buttons(self) -> None:
        self.startButton.setVisible(not self._running)
        self.stopButton.setVisible(self._running)
        self.startButton.setEnabled(not self._busy)
        self.stopButton.setEnabled(not self._busy and self._state is not LocalState.STOPPING)
        self.moreButton.setEnabled(not self._busy)

    # --- menu --------------------------------------------------------------------

    def _show_menu(self) -> None:
        menu = self.build_menu()
        menu.exec(self.moreButton.mapToGlobal(QPoint(0, self.moreButton.height())))

    def build_menu(self) -> RoundMenu:
        """Context menu (public so tests can trigger the actions without a popup)."""
        menu = RoundMenu(parent=self)
        open_dir = Action(FIF.FOLDER, "打开目录", self)
        open_dir.triggered.connect(lambda: self.open_dir_requested.emit(self.record.id))
        logs = Action(FIF.DOCUMENT, "查看输出", self)
        logs.triggered.connect(lambda: self.logs_requested.emit(self.record.id))
        github = Action(FIF.GITHUB, "在 GitHub 打开", self)
        github.triggered.connect(self.open_on_github)
        remove = Action(FIF.DELETE, "移除", self)
        remove.triggered.connect(self.ask_remove)
        menu.addActions([open_dir, logs, github])
        menu.addSeparator()
        menu.addAction(remove)
        return menu

    def open_on_github(self) -> None:
        QDesktopServices.openUrl(QUrl(self.record.repo_url + RUNNERS_SETTINGS_PATH))

    def ask_remove(self) -> None:
        """Confirm and emit remove_requested(id, keep_work)."""
        dialog = RemoveRunnerDialog(self.record, running=self._running, parent=self.window())
        if dialog.exec():
            self.remove_requested.emit(self.record.id, dialog.keep_work)
