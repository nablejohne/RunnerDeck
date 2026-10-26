"""Detail page of one runner: header, actions, live output and `_diag` logs (docs/m3-design.md §5).

The page has no navigation item: the window adds it with stackedWidget.addWidget and opens it
through MainWindow.open_detail(record_id). `show_record(None)` or a record that disappeared
(removed / forgotten) shows an empty state instead of failing. Listing and reading `_diag`
files happens in the controller's worker thread (load_diag_logs / load_diag_text).
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt5.QtCore import QPoint, Qt, QTimer, QUrl, pyqtSignal
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
    TextBrowser,
    TitleLabel,
    TransparentToolButton,
)
from qfluentwidgets import FluentIcon as FIF

from runnerdeck.core import job_log
from runnerdeck.core.models import (
    DiagLog,
    DisplayState,
    JobSnapshot,
    LocalState,
    MatchKind,
    RepoInsights,
    RunnerMode,
    RunnerRecord,
)
from runnerdeck.core.status import DisplayStatus
from runnerdeck.ui.components.log_view import LogView
from runnerdeck.ui.components.runner_card import (
    AUTO_START_TEXT,
    RUNNING_STATES,
    ask_remove,
    confirm_convert,
    confirm_forget,
    confirm_grant,
    confirm_kill,
    confirm_stop,
    runners_settings_url,
)
from runnerdeck.ui.components.status_badge import StatusBadge

if TYPE_CHECKING:
    from runnerdeck.app.controller import RunnerController

EMPTY_TEXT = "没有选中的 runner（可能已被移除或取消登记），请返回概览选择一个"
TAB_OUTPUT, TAB_DIAG, TAB_WORKFLOW, TAB_JOB = "output", "diag", "workflow", "job"
JOB_POLL_MS = 10_000  # Job tab refresh while the runner works on a job
CONCLUSION_TEXT = {
    "success": "成功",
    "failure": "失败",
    "cancelled": "已取消",
    "skipped": "已跳过",
    "timed_out": "超时",
    "action_required": "待批准",
}
STATUS_TEXT = {
    "queued": "排队中",
    "in_progress": "运行中",
    "waiting": "等待中",
    "pending": "等待中",
}


def insights_html(insights: RepoInsights) -> str:
    """The Workflow tab: which jobs this runner can take, then the recent runs (escaped)."""
    esc = html.escape
    parts = ["<h4>会用到 self-hosted runner 的 job</h4>"]
    if insights.workflow_error:
        parts.append(f"<p>无法读取 workflow：{esc(insights.workflow_error)}</p>")
    elif not insights.matches:
        parts.append("<p>没有找到要求 self-hosted runner 的 job。</p>")
    else:
        parts.append("<ul>")
        for item in insights.matches:
            target = item.target
            where = f"{esc(target.workflow)} → <b>{esc(target.job)}</b>"
            asks = ", ".join(target.labels or ()) or (target.group or "")
            if item.kind is MatchKind.OK:
                verdict = "✓ 这台 runner 可以接"
            elif item.kind is MatchKind.MISSING:
                missing = ", ".join(item.missing)
                verdict = f"✗ 缺少标签 <b>{esc(missing)}</b>：这个 job 会一直排队"
            else:
                verdict = "? 使用了表达式或 runner 组，无法判断"
            asks_text = f"（runs-on: {esc(asks)}）" if asks else ""
            parts.append(f"<li>{where}{asks_text}<br/>{verdict}</li>")
        parts.append("</ul>")
    for error in insights.parse_errors:
        parts.append(f"<p>跳过：{esc(error)}</p>")
    parts.append("<h4>最近运行</h4>")
    if insights.runs_error:
        parts.append(f"<p>无法读取运行记录：{esc(insights.runs_error)}</p>")
    elif not insights.runs:
        parts.append("<p>还没有运行记录。</p>")
    else:
        parts.append("<ul>")
        for run in insights.runs:
            title = esc(run.display_title or run.name or f"#{run.run_number or run.id}")
            state = CONCLUSION_TEXT.get(run.conclusion or "", run.conclusion or "") or (
                STATUS_TEXT.get(run.status or "", run.status or "")
            )
            when = run.created_at.astimezone().strftime("%m-%d %H:%M") if run.created_at else ""
            branch = f" · {esc(run.head_branch)}" if run.head_branch else ""
            link = f'<a href="{esc(run.html_url)}">{title}</a>' if run.html_url else title
            parts.append(f"<li>{link} — {esc(state)}{branch} · {esc(when)}</li>")
        parts.append("</ul>")
    return "".join(parts)


def job_html(snapshot: JobSnapshot, now: datetime | None = None) -> str:
    """The Job tab: the job, its steps, and once it failed the errors and what led to them."""
    esc = html.escape
    if snapshot.error:
        return f"<p>无法读取 job：{esc(snapshot.error)}</p>"
    job = snapshot.job
    if job is None:
        return f"<p>最近 {JOB_SEARCH_RUNS_TEXT} 次运行里没有这台 runner 接过的 job。</p>"
    title = esc(f"{job.workflow_name} → {job.name}" if job.workflow_name else job.name)
    link = f'<a href="{esc(job.html_url)}">{title}</a>' if job.html_url else title
    if job.done:
        state = CONCLUSION_TEXT.get(job.conclusion or "", job.conclusion or "已结束")
    else:
        state = STATUS_TEXT.get(job.status, job.status)
    took = job_log.duration(job.started_at, job.completed_at, now)
    branch = f" · {esc(job.head_branch)}" if job.head_branch else ""
    parts = [f"<h4>{link}</h4><p>{esc(state)}{f' · {took}' if took else ''}{branch}</p>"]
    if job.steps:
        parts.append("<table cellspacing='0' cellpadding='3'>")
        for icon, step in job_log.step_states(job.steps):
            spent = job_log.duration(step.started_at, step.completed_at, now)
            name = esc(step.name)
            if step.conclusion == "failure":
                name = f"<b>{name}</b>"
            parts.append(f"<tr><td>{icon}</td><td>{name}</td><td>{esc(spent)}</td></tr>")
        parts.append("</table>")
    if not job.done:
        parts.append(
            "<p>运行中只能看到步骤状态；日志在 job 结束后显示（GitHub 不提供运行中的日志）。</p>"
        )
    elif snapshot.log_pending:
        parts.append("<p>GitHub 还没提供这个 job 的日志，稍后点刷新。</p>")
    if snapshot.errors:
        parts.append("<h4>错误</h4><ul>")
        parts.extend(f"<li>{esc(error)}</li>" for error in snapshot.errors)
        parts.append("</ul>")
    if snapshot.excerpt:
        parts.append(f"<h4>出错前的输出</h4><pre>{esc(chr(10).join(snapshot.excerpt))}</pre>")
    return "".join(parts)


JOB_SEARCH_RUNS_TEXT = "10"  # manager.JOB_SEARCH_RUNS (ui/ may not import core.manager)
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
        self.versionValue = BodyLabel("", self.content)
        self.modeValue = BodyLabel("", self.content)
        for value in (
            self.repoValue,
            self.pathValue,
            self.labelsValue,
            self.idValue,
            self.versionValue,
            self.modeValue,
        ):
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setWordWrap(True)

        # tabs
        self.segmented = SegmentedWidget(self.content)
        self.segmented.addItem(TAB_OUTPUT, "实时输出")
        self.segmented.addItem(TAB_JOB, "Job")
        self.segmented.addItem(TAB_DIAG, "诊断日志")
        self.segmented.addItem(TAB_WORKFLOW, "Workflow")
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
        self.workflowPanel = QWidget(self.content)
        self.workflowRefreshButton = PushButton(FIF.SYNC, "刷新", self.workflowPanel)
        self.workflowStatusLabel = CaptionLabel("", self.workflowPanel)
        self.workflowView = TextBrowser(self.workflowPanel)
        self.workflowView.setOpenExternalLinks(True)
        workflow_layout = QVBoxLayout(self.workflowPanel)
        workflow_layout.setContentsMargins(0, 0, 0, 0)
        workflow_bar = QHBoxLayout()
        workflow_bar.addWidget(self.workflowRefreshButton)
        workflow_bar.addWidget(self.workflowStatusLabel, 1)
        workflow_layout.addLayout(workflow_bar)
        workflow_layout.addWidget(self.workflowView, 1)
        self._insights_for: str | None = None  # record id the Workflow tab shows / loads
        self.jobPanel = QWidget(self.content)
        self.jobRefreshButton = PushButton(FIF.SYNC, "刷新", self.jobPanel)
        self.jobGithubButton = PushButton(FIF.GITHUB, "在 GitHub 打开", self.jobPanel)
        self.jobGithubButton.setEnabled(False)
        self.jobStatusLabel = CaptionLabel("", self.jobPanel)
        self.jobView = TextBrowser(self.jobPanel)
        self.jobView.setOpenExternalLinks(True)
        self.jobLogView = LogView(self.jobPanel)
        self.jobLogView.hide()
        job_layout = QVBoxLayout(self.jobPanel)
        job_layout.setContentsMargins(0, 0, 0, 0)
        job_bar = QHBoxLayout()
        job_bar.addWidget(self.jobRefreshButton)
        job_bar.addWidget(self.jobGithubButton)
        job_bar.addWidget(self.jobStatusLabel, 1)
        job_layout.addLayout(job_bar)
        job_layout.addWidget(self.jobView, 2)
        job_layout.addWidget(self.jobLogView, 3)
        self._job_for: str | None = None  # record id the Job tab shows / loads
        self._job_snapshot: JobSnapshot | None = None
        self.jobTimer = QTimer(self)
        self.jobTimer.setInterval(JOB_POLL_MS)
        self.jobTimer.timeout.connect(self.reload_job)
        self.tabStack.addWidget(self.outputView)
        self.tabStack.addWidget(self.diagPanel)
        self.tabStack.addWidget(self.workflowPanel)
        self.tabStack.addWidget(self.jobPanel)
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
        controller.repo_insights_loaded.connect(self._on_insights_loaded)
        self.workflowRefreshButton.clicked.connect(lambda: self.reload_insights(force=True))
        self.jobRefreshButton.clicked.connect(self.reload_job)
        self.jobGithubButton.clicked.connect(self.open_job_on_github)
        controller.job_snapshot_loaded.connect(self._on_job_loaded)
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
                ("runner 版本", self.versionValue),
                ("运行方式", self.modeValue),
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
            self._reset_job()
            return
        self._fill_facts(record)
        state, job = self._controller.local_state(record.id)
        self._state, self._job = state, job
        self._busy = self._controller.is_busy(record.id)
        self._apply_display(self._controller.display(record.id))
        if changed:
            self.outputView.set_lines(self._controller.output(record.id))
            self._reset_diag()
            self._insights_for = None
            self.workflowView.clear()
            if self.current_tab == TAB_DIAG:
                self.reload_diag_logs()
            elif self.current_tab == TAB_WORKFLOW:
                self.reload_insights()
            self._reset_job()
            if self.current_tab == TAB_JOB:
                self.reload_job()

    def _fill_facts(self, record: RunnerRecord) -> None:
        self.nameLabel.setText(record.name)
        self.repoValue.setText(record.full_name)
        self.pathValue.setText(record.path)
        self.labelsValue.setText(labels_text(record.labels))
        self.idValue.setText(
            str(record.github_runner_id) if record.github_runner_id is not None else "未知"
        )
        self.versionValue.setText(self._controller.runner_version(record.id) or "未知")
        service = record.mode is RunnerMode.SERVICE
        self.modeValue.setText(
            f"Windows 服务（{record.service_name or '服务名未知'}，NETWORK SERVICE）"
            if service
            else "进程（由 RunnerDeck 启动）"
        )
        self.autoStartSwitch.setVisible(not service)  # a service starts with Windows
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
        started = self._state is not LocalState.BUSY and LocalState(state) is LocalState.BUSY
        self._state = LocalState(state)
        self._job = job if isinstance(job, str) else None
        self._refresh_buttons()
        if started and self.current_tab == TAB_JOB:
            self.reload_job()  # a new job: find it
        self._sync_job_timer()

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
        record = self._record
        if record is not None and record.mode is RunnerMode.SERVICE:
            grant = Action(FIF.CERTIFICATE, "授权启停…", self)
            grant.triggered.connect(self.ask_grant)
            menu.addAction(grant)
            menu.addSeparator()
        elif record is not None:
            convert = Action(FIF.SETTING, "转为服务…", self)
            convert.setEnabled(not self.running and not self._busy)
            convert.triggered.connect(self.ask_convert)
            menu.addAction(convert)
            menu.addSeparator()
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

    def ask_convert(self) -> None:
        record = self._record
        if record is not None and confirm_convert(record, self.window()):
            self._controller.convert_to_service(record.id)

    def ask_grant(self) -> None:
        record = self._record
        if record is not None and confirm_grant(record, self.window()):
            self._controller.grant_service_control(record.id)

    # --- diag logs ------------------------------------------------------------------

    def reload_insights(self, *, force: bool = False) -> None:
        record = self._record
        if record is None:
            return
        self._insights_for = record.id
        self.workflowStatusLabel.setText("正在读取 workflow 和运行记录…")
        self._controller.load_repo_insights(record.id, force=force)

    def _on_insights_loaded(self, record_id: str, insights: RepoInsights) -> None:
        if record_id != self._insights_for:
            return
        fetched = insights.fetched_at.astimezone().strftime("%H:%M") if insights.fetched_at else ""
        self.workflowStatusLabel.setText(f"{fetched} 读取（最多缓存 10 分钟，点刷新重新读取）")
        self.workflowView.setHtml(insights_html(insights))

    def _on_tab_changed(self, key: str) -> None:
        panels = {
            TAB_DIAG: self.diagPanel,
            TAB_WORKFLOW: self.workflowPanel,
            TAB_JOB: self.jobPanel,
        }
        self.tabStack.setCurrentWidget(panels.get(key, self.outputView))
        record = self._record
        if key == TAB_WORKFLOW and record is not None and self._insights_for != record.id:
            self.reload_insights()
        if key == TAB_DIAG and record is not None and self._diag_loaded_for != record.id:
            self.reload_diag_logs()
        if key == TAB_JOB and record is not None:
            self.reload_job()
        self._sync_job_timer()

    # --- Job tab (spike-notes §13) -----------------------------------------------------

    def reload_job(self) -> None:
        record = self._record
        if record is None:
            return
        self._job_for = record.id
        if self._job_snapshot is None:
            self.jobStatusLabel.setText("正在从 GitHub 读取这台 runner 的 job…")
        self._controller.load_job(record.id)

    def _reset_job(self) -> None:
        self._job_for = None
        self._job_snapshot = None
        self.jobView.clear()
        self.jobLogView.clear()
        self.jobLogView.hide()
        self.jobStatusLabel.setText("")
        self.jobGithubButton.setEnabled(False)
        self._sync_job_timer()

    def _on_job_loaded(self, record_id: str, snapshot: object) -> None:
        if record_id != self._job_for or not isinstance(snapshot, JobSnapshot):
            return
        self._job_snapshot = snapshot
        self.jobView.setHtml(job_html(snapshot, datetime.now(UTC)))
        self.jobLogView.setVisible(bool(snapshot.log))
        if snapshot.log:
            self.jobLogView.set_lines(snapshot.log)
        job = snapshot.job
        self.jobGithubButton.setEnabled(job is not None and bool(job.html_url))
        when = datetime.now().strftime("%H:%M:%S")
        live = job is not None and not job.done
        self.jobStatusLabel.setText(f"{when} 读取" + ("，job 运行中每 10 秒刷新" if live else ""))
        self._sync_job_timer()

    def _job_live(self) -> bool:
        job = self._job_snapshot.job if self._job_snapshot is not None else None
        return self._state is LocalState.BUSY or (job is not None and not job.done)

    def _sync_job_timer(self) -> None:
        wanted = (
            self._record is not None
            and self.current_tab == TAB_JOB
            and self.isVisible()
            and self._job_live()
        )
        if wanted and not self.jobTimer.isActive():
            self.jobTimer.start()
        elif not wanted and self.jobTimer.isActive():
            self.jobTimer.stop()

    def open_job_on_github(self) -> None:
        job = self._job_snapshot.job if self._job_snapshot is not None else None
        if job is not None and job.html_url:
            QDesktopServices.openUrl(QUrl(job.html_url))

    def showEvent(self, event: object) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)  # type: ignore[arg-type]
        self._sync_job_timer()

    def hideEvent(self, event: object) -> None:  # noqa: N802 (Qt override)
        super().hideEvent(event)  # type: ignore[arg-type]
        self._sync_job_timer()

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
