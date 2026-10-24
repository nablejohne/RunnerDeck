"""One runner on the dashboard: name, repo, status badge, start/stop button and a menu.

The card never talks to the controller itself: it emits `*_requested` signals carrying the
record id and the dashboard page forwards them (docs/m2-design.md §5, docs/m3-design.md §5).
The badge and the detail line follow only `update_display` (controller.display_changed);
`update_state` (controller.runner_state) only decides which buttons are offered.

The confirmation helpers at module level are shared with the detail page and the tray.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PyQt5 import sip
from PyQt5.QtCore import QPoint, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckableMenu,
    CheckBox,
    IndeterminateProgressRing,
    MenuIndicatorType,
    MessageBox,
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
from runnerdeck.ui.components.status_badge import StatusBadge

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
AUTO_START_TEXT = "随 RunnerDeck 启动"


def runners_settings_url(record: RunnerRecord) -> QUrl:
    return QUrl(record.repo_url + RUNNERS_SETTINGS_PATH)


# --- confirmations (shared with the detail page and the tray) -----------------------------


def stop_question(busy: Sequence[tuple[str, str | None]]) -> str:
    """Text asking before stopping runners that execute a job; busy = (name, job) pairs."""
    if len(busy) == 1:
        name, job = busy[0]
        return f"runner {name} 正在执行 job {job or '（名字未知）'}，停止会取消它。要停止吗？"
    lines = [f"· {name}：{job or '（名字未知）'}" for name, job in busy]
    return "以下 runner 正在执行 job，停止会取消它们：\n" + "\n".join(lines) + "\n要继续吗？"


def confirm_stop(busy: Sequence[tuple[str, str | None]], parent: QWidget) -> bool:
    """True when nothing is busy or the user agreed to cancel the running jobs."""
    if not busy:
        return True
    box = MessageBox("停止 runner", stop_question(busy), parent)
    box.yesButton.setText("停止")
    box.cancelButton.setText("取消")
    return bool(box.exec())


def kill_question(record: RunnerRecord, *, take_over: bool, remote_busy: bool) -> str:
    lines = [f"将强制结束目录 {record.path} 下正在运行的 runner 进程。"]
    if take_over:
        lines.append("结束后由 RunnerDeck 重新启动它。")
    lines.append("GitHub 端旧会话约 3 分钟后才过期，期间启动会先显示“等待旧会话过期”。")
    if remote_busy:
        lines.append("GitHub 显示它正在执行 job，强杀会留下僵尸 job（要等 GitHub 超时才会结束）。")
    return "\n".join(lines)


def confirm_kill(
    record: RunnerRecord, *, take_over: bool, remote_busy: bool, parent: QWidget
) -> bool:
    title = f"接管 runner {record.name}" if take_over else f"结束 runner {record.name} 的进程"
    box = MessageBox(
        title, kill_question(record, take_over=take_over, remote_busy=remote_busy), parent
    )
    box.yesButton.setText("接管" if take_over else "结束进程")
    box.cancelButton.setText("取消")
    return bool(box.exec())


def forget_question(record: RunnerRecord) -> str:
    return (
        f"只从 RunnerDeck 删除这条记录，不会注销也不会删除任何文件：\n"
        f"目录 {record.path} 和 GitHub 仓库 {record.full_name} 里的注册都原样保留，"
        "之后可以重新导入。"
    )


def confirm_forget(record: RunnerRecord, parent: QWidget) -> bool:
    box = MessageBox(f"取消登记 runner {record.name}", forget_question(record), parent)
    box.yesButton.setText("取消登记")
    box.cancelButton.setText("返回")
    return bool(box.exec())


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


def ask_remove(record: RunnerRecord, *, running: bool, parent: QWidget) -> bool | None:
    """Show RemoveRunnerDialog; keep_work when confirmed, None when cancelled."""
    dialog = RemoveRunnerDialog(record, running=running, parent=parent)
    if dialog.exec():
        return dialog.keep_work
    return None


class RunnerCard(CardWidget):
    """Dashboard card for one RunnerRecord.

    Signals carry the record id; `remove_requested` also carries keep_work and
    `auto_start_toggled` the new value. Clicking the card itself asks for the detail page.
    `remote_busy(record_id)` (controller.remote_busy) adds the zombie-job warning to the kill
    confirmation.
    """

    start_requested = pyqtSignal(str)
    stop_requested = pyqtSignal(str)
    remove_requested = pyqtSignal(str, bool)
    open_dir_requested = pyqtSignal(str)
    detail_requested = pyqtSignal(str)
    take_over_requested = pyqtSignal(str)
    kill_requested = pyqtSignal(str)
    auto_start_toggled = pyqtSignal(str, bool)
    forget_requested = pyqtSignal(str)

    def __init__(
        self,
        record: RunnerRecord,
        parent: QWidget | None = None,
        *,
        remote_busy: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.record = record
        self._remote_busy = remote_busy
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

        self.startButton.clicked.connect(self.request_start)
        self.stopButton.clicked.connect(self.request_stop)
        self.moreButton.clicked.connect(self._show_menu)
        self.clicked.connect(lambda: self.detail_requested.emit(self.record.id))

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

    def set_record(self, record: RunnerRecord) -> None:
        """The record changed on disk (rename, auto_start, labels): refresh the texts."""
        self.record = record
        self.nameLabel.setText(record.name)
        self.repoLabel.setText(record.full_name)

    @property
    def running(self) -> bool:
        return self._running

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def display_state(self) -> DisplayState:
        return self.badge.state

    @property
    def external(self) -> bool:
        """Another process runs this runner: the main button offers "接管"."""
        return not self._running and self.badge.state is DisplayState.EXTERNAL

    def update_state(self, state: LocalState, job: str | None) -> None:
        """Local process state changed (controller.runner_state): buttons only."""
        self._state = state
        self._job = job
        self._running = state in RUNNING_STATES
        self._refresh_buttons()

    def update_display(self, display: DisplayStatus) -> None:
        """Merged local + GitHub status changed (controller.display_changed)."""
        self.badge.set_display(display)
        self.detailLabel.setText(display.detail or "")
        self.detailLabel.setVisible(bool(display.detail))
        self._refresh_buttons()

    def set_busy(self, busy: bool) -> None:
        """An operation (start / stop / remove / kill …) is in flight for this runner."""
        self._busy = busy
        self.spinner.setVisible(busy)
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        self.startButton.setText("接管" if self.external else "启动")
        self.startButton.setVisible(not self._running)
        self.stopButton.setVisible(self._running)
        self.startButton.setEnabled(not self._busy)
        self.stopButton.setEnabled(not self._busy and self._state is not LocalState.STOPPING)
        self.moreButton.setEnabled(not self._busy)

    # --- actions ------------------------------------------------------------------

    def _is_remote_busy(self) -> bool:
        return self._remote_busy is not None and self._remote_busy(self.record.id)

    def _alive(self) -> bool:
        """False once the card was deleted while one of its dialogs ran (its record was
        removed meanwhile); the answer is then dropped instead of emitting from a dead object."""
        return not sip.isdeleted(self)

    def request_start(self) -> None:
        """Start, or take over (after confirming the hard kill) when shown as EXTERNAL."""
        if not self.external:
            self.start_requested.emit(self.record.id)
        elif (
            confirm_kill(
                self.record,
                take_over=True,
                remote_busy=self._is_remote_busy(),
                parent=self.window(),
            )
            and self._alive()
        ):
            self.take_over_requested.emit(self.record.id)

    def request_stop(self) -> None:
        """Stop; a runner executing a job asks first."""
        busy = [(self.record.name, self._job)] if self._state is LocalState.BUSY else []
        if confirm_stop(busy, self.window()) and self._alive():
            self.stop_requested.emit(self.record.id)

    def ask_kill(self) -> None:
        if (
            confirm_kill(
                self.record,
                take_over=False,
                remote_busy=self._is_remote_busy(),
                parent=self.window(),
            )
            and self._alive()
        ):
            self.kill_requested.emit(self.record.id)

    def ask_remove(self) -> None:
        """Confirm and emit remove_requested(id, keep_work)."""
        keep_work = ask_remove(self.record, running=self._running, parent=self.window())
        if keep_work is not None and self._alive():
            self.remove_requested.emit(self.record.id, keep_work)

    def ask_forget(self) -> None:
        if confirm_forget(self.record, self.window()) and self._alive():
            self.forget_requested.emit(self.record.id)

    def open_on_github(self) -> None:
        QDesktopServices.openUrl(runners_settings_url(self.record))

    # --- menu --------------------------------------------------------------------

    def _show_menu(self) -> None:
        menu = self.build_menu()
        menu.exec(self.moreButton.mapToGlobal(QPoint(0, self.moreButton.height())))

    def build_menu(self) -> RoundMenu:
        """Context menu (public so tests can trigger the actions without a popup)."""
        rid = self.record.id
        menu = CheckableMenu(parent=self, indicatorType=MenuIndicatorType.CHECK)
        detail = Action(FIF.INFO, "详情", self)
        detail.triggered.connect(lambda: self.detail_requested.emit(rid))
        open_dir = Action(FIF.FOLDER, "打开目录", self)
        open_dir.triggered.connect(lambda: self.open_dir_requested.emit(rid))
        github = Action(FIF.GITHUB, "在 GitHub 打开", self)
        github.triggered.connect(self.open_on_github)
        auto_start = Action(FIF.POWER_BUTTON, AUTO_START_TEXT, self, checkable=True)
        auto_start.setChecked(self.record.auto_start)
        auto_start.triggered.connect(lambda checked: self.auto_start_toggled.emit(rid, checked))
        menu.addActions([detail, open_dir, github, auto_start])
        if self.external:
            kill = Action(FIF.CLOSE, "结束进程", self)
            kill.triggered.connect(self.ask_kill)
            menu.addAction(kill)
        menu.addSeparator()
        remove = Action(FIF.DELETE, "移除", self)
        remove.triggered.connect(self.ask_remove)
        forget = Action(FIF.REMOVE, "取消登记", self)
        forget.setToolTip("只删除 RunnerDeck 里的记录，目录和 GitHub 注册都保留")
        forget.setEnabled(not self._running)
        forget.triggered.connect(self.ask_forget)
        menu.addActions([remove, forget])
        return menu
