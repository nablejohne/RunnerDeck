"""Detail page of one runner: header, actions, live output and `_diag` logs (docs/m3-design.md §5).

The page has no navigation item: the window adds it with stackedWidget.addWidget and opens it
through MainWindow.open_detail(record_id). `show_record(None)` or a record that disappeared
(removed / forgotten) shows an empty state instead of failing. Listing and reading `_diag`
files happens in the controller's worker thread (load_diag_logs / load_diag_text).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt5.QtCore import QPoint, Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QGridLayout, QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    ComboBox,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    RoundMenu,
    SegmentedWidget,
    SwitchButton,
    TitleLabel,
    TransparentToolButton,
)
from qfluentwidgets import FluentIcon as FIF

from runnerdeck.core.models import DiagLog, DisplayState, LocalState, RunnerRecord
from runnerdeck.core.status import DisplayStatus
from runnerdeck.ui.components.log_view import LogView
from runnerdeck.ui.components.runner_card import (
    AUTO_START_TEXT,
    RUNNING_STATES,
    ask_remove,
    confirm_forget,
    confirm_kill,
    confirm_stop,
    runners_settings_url,
)
from runnerdeck.ui.components.status_badge import StatusBadge

if TYPE_CHECKING:
    from runnerdeck.app.controller import RunnerController

EMPTY_TEXT = "没有选中的 runner（可能已被移除或取消登记），请返回概览选择一个"
TAB_OUTPUT, TAB_DIAG = "output", "diag"
OUTPUT_MAX_LINES = 5000  # the controller keeps 2000; room for what arrives while open
DIAG_DIR = "_diag"  # same name as core/diag.DIAG_DIR (ui/ may not import core.diag)
DIAG_KIND_LABELS = {"runner": "Runner", "worker": "Worker", "other": "其他"}
NO_DIAG_TEXT = "还没有诊断日志：_diag 目录为空或不存在（runner 启动过一次后才会生成）"
NO_OUTPUT_TEXT = "（runner 由 RunnerDeck 启动后，这里实时显示它的输出）"


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def diag_item_text(log: DiagLog) -> str:
    modified = log.modified.astimezone() if log.modified.tzinfo else log.modified
    kind = DIAG_KIND_LABELS.get(log.kind, log.kind)
    return f"{log.name} · {kind} · {modified:%Y-%m-%d %H:%M:%S} · {format_size(log.size)}"


def labels_text(labels: Sequence[str]) -> str:
    return ", ".join(labels) if labels else "（无自定义标签）"


class RunnerDetailPage(QWidget):
    """objectName "detail"; emits `back_requested` when the user wants the overview back."""

    back_requested = pyqtSignal()

    def __init__(self, controller: RunnerController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("detail")
        self.setProperty("isStackedTransparent", False)
        self._controller = controller
        self._record: RunnerRecord | None = None
        self._state = LocalState.STOPPED
        self._job: str | None = None
        self._busy = False
        self._diag_loaded_for: str | None = None
        self._diag_path: str | None = None  # file whose text is being / was loaded
        self._diag_waiting = False  # a load_diag_logs / load_diag_text answer is pending
        self._syncing = False  # programmatic widget updates must not call the controller

        # header
        self.backButton = TransparentToolButton(FIF.RETURN, self)
        self.backButton.setToolTip("返回概览")
        self.nameLabel = TitleLabel("", self)
        self.badge = StatusBadge(self)
        self.startButton = PrimaryPushButton(FIF.PLAY, "启动", self)
        self.stopButton = PushButton(FIF.PAUSE, "停止", self)
        self.autoStartSwitch = SwitchButton(AUTO_START_TEXT, self)
        self.autoStartSwitch.setOnText(AUTO_START_TEXT)
        self.autoStartSwitch.setOffText(AUTO_START_TEXT)
        self.openDirButton = TransparentToolButton(FIF.FOLDER, self)
        self.openDirButton.setToolTip("打开目录")
        self.githubButton = TransparentToolButton(FIF.GITHUB, self)
        self.githubButton.setToolTip("在 GitHub 打开")
        self.moreButton = TransparentToolButton(FIF.MORE, self)
        self.moreButton.setToolTip("更多操作")
        self.detailLabel = CaptionLabel("", self)
        self.detailLabel.setWordWrap(True)
        self.emptyLabel = BodyLabel(EMPTY_TEXT, self)
        self.emptyLabel.setAlignment(Qt.AlignCenter)

        # facts
        self.content = QWidget(self)
        self.repoValue = BodyLabel("", self.content)
        self.pathValue = BodyLabel("", self.content)
        self.labelsValue = BodyLabel("", self.content)
        self.idValue = BodyLabel("", self.content)
        for value in (self.repoValue, self.pathValue, self.labelsValue, self.idValue):
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setWordWrap(True)

        # tabs
        self.segmented = SegmentedWidget(self.content)
        self.segmented.addItem(TAB_OUTPUT, "实时输出")
        self.segmented.addItem(TAB_DIAG, "诊断日志")
        self.tabStack = QStackedWidget(self.content)
        self.outputView = LogView(self.content, max_lines=OUTPUT_MAX_LINES)
        self.outputView.textEdit.setPlaceholderText(NO_OUTPUT_TEXT)
        self.diagPanel = QWidget(self.content)
        self.diagCombo = ComboBox(self.diagPanel)
        self.diagCombo.setMinimumWidth(420)
        self.diagCombo.setPlaceholderText("选择日志文件")
        self.diagRefreshButton = PushButton(FIF.SYNC, "刷新", self.diagPanel)
        self.diagOpenButton = PushButton(FIF.FOLDER, "打开 _diag 目录", self.diagPanel)
        self.diagStatusLabel = CaptionLabel("", self.diagPanel)
        self.diagStatusLabel.setWordWrap(True)
        self.diagView = LogView(self.diagPanel)
        self.tabStack.addWidget(self.outputView)
        self.tabStack.addWidget(self.diagPanel)
        self.segmented.setCurrentItem(TAB_OUTPUT)

        self._build_layout()

        self.backButton.clicked.connect(self.back_requested)
        self.startButton.clicked.connect(self.request_start)
        self.stopButton.clicked.connect(self.request_stop)
        self.autoStartSwitch.checkedChanged.connect(self._on_auto_start_switched)
        self.openDirButton.clicked.connect(self._open_dir)
        self.githubButton.clicked.connect(self.open_on_github)
        self.moreButton.clicked.connect(self._show_menu)
        self.segmented.currentItemChanged.connect(self._on_tab_changed)
        self.diagCombo.currentIndexChanged.connect(self._on_diag_selected)
        self.diagRefreshButton.clicked.connect(self.reload_diag_logs)
        self.diagOpenButton.clicked.connect(self.open_diag_dir)

        controller.config_changed.connect(self._on_config_changed)
        controller.runner_state.connect(self._on_runner_state)
        controller.display_changed.connect(self._on_display_changed)
        controller.busy_changed.connect(self._on_busy_changed)
        controller.runner_line.connect(self._on_runner_line)
        controller.diag_logs_loaded.connect(self._on_diag_logs_loaded)
        controller.diag_text_loaded.connect(self._on_diag_text_loaded)
        controller.failed.connect(self._on_failed)

        self.show_record(None)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 24, 36, 24)
        layout.setSpacing(12)
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(self.backButton)
        header.addWidget(self.nameLabel)
        header.addWidget(self.badge, 0, Qt.AlignVCenter)
        header.addStretch(1)
        header.addWidget(self.autoStartSwitch)
        header.addSpacing(8)
        header.addWidget(self.startButton)
        header.addWidget(self.stopButton)
        header.addWidget(self.openDirButton)
        header.addWidget(self.githubButton)
        header.addWidget(self.moreButton)
        layout.addLayout(header)
        layout.addWidget(self.detailLabel)
        layout.addWidget(self.emptyLabel, 1)

        content = QVBoxLayout(self.content)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(12)
        facts = QGridLayout()
        facts.setHorizontalSpacing(16)
        facts.setVerticalSpacing(4)
        for row, (title, value) in enumerate(
            (
                ("仓库", self.repoValue),
                ("目录", self.pathValue),
                ("标签", self.labelsValue),
                ("GitHub id", self.idValue),
            )
        ):
            facts.addWidget(CaptionLabel(title, self.content), row, 0, Qt.AlignTop)
            facts.addWidget(value, row, 1)
        facts.setColumnStretch(1, 1)
        content.addLayout(facts)
        content.addWidget(self.segmented, 0, Qt.AlignLeft)
        content.addWidget(self.tabStack, 1)

        diag = QVBoxLayout(self.diagPanel)
        diag.setContentsMargins(0, 0, 0, 0)
        diag.setSpacing(8)
        bar = QHBoxLayout()
        bar.addWidget(self.diagCombo, 1)
        bar.addWidget(self.diagRefreshButton)
        bar.addWidget(self.diagOpenButton)
        diag.addLayout(bar)
        diag.addWidget(self.diagStatusLabel)
        diag.addWidget(self.diagView, 1)
        layout.addWidget(self.content, 1)

    # --- record ------------------------------------------------------------------------

    @property
    def record(self) -> RunnerRecord | None:
        return self._record

    @property
    def record_id(self) -> str | None:
        return self._record.id if self._record is not None else None

    @property
    def current_tab(self) -> str:
        return self.segmented.currentRouteKey()

    def _lookup(self, record_id: str | None) -> RunnerRecord | None:
        if record_id is None:
            return None
        return next((r for r in self._controller.records if r.id == record_id), None)

    def show_record(self, record_id: str | None) -> None:
        """Show one runner; None or an unknown id shows the empty state."""
        record = self._lookup(record_id)
        changed = record is None or self._record is None or record.id != self._record.id
        self._record = record
        has_record = record is not None
        for widget in (
            self.badge,
            self.startButton,
            self.stopButton,
            self.autoStartSwitch,
            self.openDirButton,
            self.githubButton,
            self.moreButton,
            self.content,
        ):
            widget.setVisible(has_record)
        self.emptyLabel.setVisible(not has_record)
        if record is None:
            self.nameLabel.setText("runner 详情")
            self.detailLabel.hide()
            self.outputView.clear()
            self._reset_diag()
            return
        self._fill_facts(record)
        state, job = self._controller.local_state(record.id)
        self._state, self._job = state, job
        self._busy = self._controller.is_busy(record.id)
        self._apply_display(self._controller.display(record.id))
        if changed:
            self.outputView.set_lines(self._controller.output(record.id))
            self._reset_diag()
            if self.current_tab == TAB_DIAG:
                self.reload_diag_logs()

    def _fill_facts(self, record: RunnerRecord) -> None:
        self.nameLabel.setText(record.name)
        self.repoValue.setText(record.full_name)
        self.pathValue.setText(record.path)
        self.labelsValue.setText(labels_text(record.labels))
        self.idValue.setText(
            str(record.github_runner_id) if record.github_runner_id is not None else "未知"
        )
        self._syncing = True
        try:
            self.autoStartSwitch.setChecked(record.auto_start)
        finally:
            self._syncing = False

    def _apply_display(self, display: DisplayStatus) -> None:
        self.badge.set_display(display)
        self.detailLabel.setText(display.detail or "")
        self.detailLabel.setVisible(bool(display.detail))
        self._refresh_buttons()

    @property
    def running(self) -> bool:
        return self._state in RUNNING_STATES

    @property
    def external(self) -> bool:
        return not self.running and self.badge.state is DisplayState.EXTERNAL

    def _refresh_buttons(self) -> None:
        running = self.running
        self.startButton.setText("接管" if self.external else "启动")
        self.startButton.setVisible(self._record is not None and not running)
        self.stopButton.setVisible(self._record is not None and running)
        self.startButton.setEnabled(not self._busy)
        self.stopButton.setEnabled(not self._busy and self._state is not LocalState.STOPPING)
        self.moreButton.setEnabled(not self._busy)

    # --- controller signals -------------------------------------------------------------

    def _on_config_changed(self, _config: object = None) -> None:
        if self._record is not None:
            self.show_record(self._record.id)  # refreshed facts, or the empty state if gone

    def _on_runner_state(self, record_id: str, state: object, job: object) -> None:
        if record_id != self.record_id:
            return
        self._state = LocalState(state)
        self._job = job if isinstance(job, str) else None
        self._refresh_buttons()

    def _on_display_changed(self, record_id: str, display: object) -> None:
        if record_id == self.record_id and isinstance(display, DisplayStatus):
            self._apply_display(display)

    def _on_busy_changed(self, record_id: str, busy: bool) -> None:
        if record_id == self.record_id:
            self._busy = busy
            self._refresh_buttons()

    def _on_runner_line(self, record_id: str, line: str) -> None:
        if record_id == self.record_id:
            self.outputView.append_line(line)

    def _on_failed(self, op: str, message: str, _exc: object = None) -> None:
        if op == "diag" and self._diag_waiting:
            self._diag_waiting = False
            self.diagStatusLabel.setText(f"读取失败：{message}")
            self.diagView.clear()

    # --- actions --------------------------------------------------------------------

    def request_start(self) -> None:
        record = self._record
        if record is None:
            return
        if not self.external:
            self._controller.start(record.id)
        elif confirm_kill(
            record,
            take_over=True,
            remote_busy=self._controller.remote_busy(record.id),
            parent=self.window(),
        ):
            self._controller.take_over(record.id)

    def request_stop(self) -> None:
        record = self._record
        if record is None:
            return
        busy = [(record.name, self._job)] if self._state is LocalState.BUSY else []
        if confirm_stop(busy, self.window()):
            self._controller.stop(record.id)

    def _on_auto_start_switched(self, checked: bool) -> None:
        if not self._syncing and self._record is not None:
            self._controller.set_auto_start(self._record.id, checked)

    def _open_dir(self) -> None:
        if self._record is not None:
            self._controller.open_dir(self._record.id)

    def open_on_github(self) -> None:
        if self._record is not None:
            QDesktopServices.openUrl(runners_settings_url(self._record))

    def ask_kill(self) -> None:
        record = self._record
        if record is not None and confirm_kill(
            record,
            take_over=False,
            remote_busy=self._controller.remote_busy(record.id),
            parent=self.window(),
        ):
            self._controller.kill_external(record.id)

    def ask_remove(self) -> None:
        record = self._record
        if record is None:
            return
        keep_work = ask_remove(record, running=self.running, parent=self.window())
        if keep_work is not None:
            self._controller.remove(record.id, keep_work=keep_work)

    def ask_forget(self) -> None:
        record = self._record
        if record is not None and confirm_forget(record, self.window()):
            self._controller.forget(record.id)

    def _show_menu(self) -> None:
        menu = self.build_menu()
        menu.exec(self.moreButton.mapToGlobal(QPoint(0, self.moreButton.height())))

    def build_menu(self) -> RoundMenu:
        """More-actions menu (public so tests can trigger the actions without a popup)."""
        menu = RoundMenu(parent=self)
        if self.external:
            kill = Action(FIF.CLOSE, "结束进程", self)
            kill.triggered.connect(self.ask_kill)
            menu.addAction(kill)
            menu.addSeparator()
        remove = Action(FIF.DELETE, "移除", self)
        remove.triggered.connect(self.ask_remove)
        forget = Action(FIF.REMOVE, "取消登记", self)
        forget.setEnabled(not self.running)
        forget.triggered.connect(self.ask_forget)
        menu.addActions([remove, forget])
        return menu

    # --- diag logs ------------------------------------------------------------------

    def _on_tab_changed(self, key: str) -> None:
        self.tabStack.setCurrentWidget(self.diagPanel if key == TAB_DIAG else self.outputView)
        record = self._record
        if key == TAB_DIAG and record is not None and self._diag_loaded_for != record.id:
            self.reload_diag_logs()

    def switch_tab(self, key: str) -> None:
        """Select 实时输出 / 诊断日志 (TAB_OUTPUT / TAB_DIAG) as if the user clicked it."""
        self.segmented.setCurrentItem(key)  # emits currentItemChanged -> _on_tab_changed

    def _reset_diag(self) -> None:
        self._diag_loaded_for = None
        self._diag_path = None
        self._diag_waiting = False
        self._syncing = True
        try:
            self.diagCombo.clear()
            self.diagCombo.setPlaceholderText("选择日志文件")
        finally:
            self._syncing = False
        self.diagStatusLabel.setText("")
        self.diagView.clear()

    def reload_diag_logs(self) -> None:
        if self._record is None:
            return
        self._diag_loaded_for = self._record.id
        self._diag_waiting = True
        self.diagStatusLabel.setText("正在读取诊断日志列表…")
        self._controller.load_diag_logs(self._record.id)

    def _on_diag_logs_loaded(self, record_id: str, logs: object) -> None:
        if record_id != self.record_id:
            return
        self._diag_waiting = False
        entries = [log for log in logs if isinstance(log, DiagLog)]  # type: ignore[attr-defined]
        previous = self._diag_path
        self._syncing = True
        try:
            self.diagCombo.clear()
            for log in entries:
                self.diagCombo.addItem(diag_item_text(log), userData=str(log.path))
            paths = [str(log.path) for log in entries]
            index = paths.index(previous) if previous in paths else 0
            if entries:
                self.diagCombo.setCurrentIndex(index)
            else:
                self.diagCombo.setPlaceholderText("没有日志文件")
        finally:
            self._syncing = False
        if not entries:
            self._diag_path = None
            self.diagStatusLabel.setText(NO_DIAG_TEXT)
            self.diagView.clear()
            return
        self.diagStatusLabel.setText(f"共 {len(entries)} 个日志文件，按修改时间从新到旧")
        self._load_diag_text(paths[index])

    def _on_diag_selected(self, index: int) -> None:
        if self._syncing or index < 0:
            return
        path = self.diagCombo.itemData(index)
        if isinstance(path, str):
            self._load_diag_text(path)

    def _load_diag_text(self, path: str) -> None:
        if self._record is None:
            return
        self._diag_path = path
        self._diag_waiting = True
        self.diagView.set_text("正在读取…")
        self._controller.load_diag_text(self._record.id, path)

    def _on_diag_text_loaded(self, record_id: str, path: str, text: str) -> None:
        if record_id != self.record_id or path != self._diag_path:
            return  # an answer for a file the user no longer looks at
        self._diag_waiting = False
        self.diagView.set_text(text)

    def open_diag_dir(self) -> None:
        if self._record is None:
            return
        diag_dir = Path(self._record.path) / DIAG_DIR
        if not diag_dir.is_dir():
            InfoBar.warning(
                "没有 _diag 目录",
                "runner 启动过一次后才会生成诊断日志",
                parent=self.window(),
                position=InfoBarPosition.TOP,
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(diag_dir)))
