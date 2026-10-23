"""Three-step wizard that registers a new runner (docs/m2-design.md §5).

Step 1 picks a repository (controller.load_repos), step 2 collects name / labels / options
and asks the controller for an AddPlan (plan_add -> plan_ready), step 3 runs add_runner and
shows add_progress until runner_added or failed("add").

The page reads the plan only through attributes (full_name, name, labels, target_dir, version,
warnings, needs_confirmation, private): it never imports runnerdeck.core.manager.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QListWidgetItem,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    IconWidget,
    IndeterminateProgressRing,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    ListWidget,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SearchLineEdit,
    StrongBodyLabel,
    SubtitleLabel,
    TitleLabel,
)
from qfluentwidgets import FluentIcon as FIF

if TYPE_CHECKING:
    from runnerdeck.app.controller import RunnerController

log = logging.getLogger(__name__)

STEP_REPO, STEP_CONFIG, STEP_PROGRESS = 0, 1, 2
SUCCESS_RETURN_DELAY_MS = 2000
REPO_ROLE = Qt.UserRole
PS_POLICY_TEXT = "允许 PowerShell 步骤（.env 写入 PSExecutionPolicyPreference=RemoteSigned）"
CONFIRM_TEXT = "我已了解上述风险"
# Progress rows in the order manager.Step reports them (rollback is shown in the status line).
PROGRESS_STEPS: tuple[tuple[str, str], ...] = (
    ("release", "查询 runner 版本"),
    ("download", "下载安装包"),
    ("extract", "解压"),
    ("token", "获取注册 token"),
    ("configure", "注册 runner"),
    ("env", "写入运行环境"),
    ("verify", "验证"),
    ("save", "保存记录"),
    ("done", "完成"),
)


def split_labels(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def repo_item_text(repo: Any, has_local_runner: bool) -> str:
    tags = ["私有" if repo.private else "公开"]
    if getattr(repo, "archived", False):
        tags.append("已归档")
    if getattr(repo, "fork", False):
        tags.append("fork")
    if has_local_runner:
        tags.append("本机已有 runner")
    return f"{repo.full_name}   ·   {' / '.join(tags)}"


class RepoStep(QWidget):
    """Filterable list of the account's repositories."""

    selection_changed = pyqtSignal()
    reload_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._repos: list[Any] = []
        self._local_full_names: set[str] = set()

        self.titleLabel = SubtitleLabel("① 选择仓库", self)
        self.searchEdit = SearchLineEdit(self)
        self.searchEdit.setPlaceholderText("按 owner/repo 过滤")
        self.listWidget = ListWidget(self)
        self.spinner = IndeterminateProgressRing(self, start=False)
        self.spinner.setFixedSize(24, 24)
        self.spinner.hide()
        self.statusLabel = CaptionLabel("", self)
        self.reloadButton = PushButton(FIF.SYNC, "重新加载", self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.titleLabel)
        row = QHBoxLayout()
        row.addWidget(self.searchEdit, 1)
        row.addWidget(self.reloadButton)
        layout.addLayout(row)
        status = QHBoxLayout()
        status.addWidget(self.spinner)
        status.addWidget(self.statusLabel, 1)
        layout.addLayout(status)
        layout.addWidget(self.listWidget, 1)

        self.searchEdit.textChanged.connect(self._apply_filter)
        self.listWidget.itemSelectionChanged.connect(self.selection_changed)
        self.listWidget.itemDoubleClicked.connect(lambda _item: self.selection_changed.emit())
        self.reloadButton.clicked.connect(self.reload_requested)

    def set_loading(self, loading: bool) -> None:
        self.spinner.setVisible(loading)
        if loading:
            self.spinner.start()
            self.statusLabel.setText("正在加载仓库列表…")
        else:
            self.spinner.stop()
        self.reloadButton.setEnabled(not loading)

    def set_repos(self, repos: list[Any], local_full_names: set[str]) -> None:
        self._repos = list(repos)
        self._local_full_names = {name.casefold() for name in local_full_names}
        self.set_loading(False)
        self.statusLabel.setText(
            f"共 {len(self._repos)} 个仓库" if self._repos else "没有可用的仓库"
        )
        self._apply_filter(self.searchEdit.text())

    def set_error(self, message: str) -> None:
        self.set_loading(False)
        self.statusLabel.setText(message)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().casefold()
        self.listWidget.clear()
        for repo in self._repos:
            if needle and needle not in repo.full_name.casefold():
                continue
            item = QListWidgetItem(
                repo_item_text(repo, repo.full_name.casefold() in self._local_full_names)
            )
            item.setData(REPO_ROLE, repo)
            self.listWidget.addItem(item)

    def selected_repo(self) -> Any | None:
        items = self.listWidget.selectedItems()
        return items[0].data(REPO_ROLE) if items else None


class ConfigStep(QWidget):
    """Name, labels, options and (once planned) the plan summary."""

    changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.titleLabel = SubtitleLabel("② 配置 runner", self)
        self.repoLabel = StrongBodyLabel("", self)
        self.nameEdit = LineEdit(self)
        self.nameEdit.setPlaceholderText("留空则使用默认名称：{主机名}-{仓库名}")
        self.nameEdit.setClearButtonEnabled(True)
        self.labelsEdit = LineEdit(self)
        self.labelsEdit.setPlaceholderText(
            "自定义标签，逗号分隔（self-hosted / windows / x64 会自动添加）"
        )
        self.labelsEdit.setClearButtonEnabled(True)
        self.psPolicyBox = CheckBox(PS_POLICY_TEXT, self)
        self.psPolicyBox.setChecked(True)
        self.startAfterBox = CheckBox("添加后立即启动", self)
        self.startAfterBox.setChecked(True)

        self.summaryTitle = StrongBodyLabel("即将执行", self)
        self.summaryLabel = BodyLabel("", self)
        self.summaryLabel.setWordWrap(True)
        self.summaryLabel.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.warningLabel = BodyLabel("", self)
        self.warningLabel.setWordWrap(True)
        self.confirmBox = CheckBox(CONFIRM_TEXT, self)
        self.summaryWidgets = (
            self.summaryTitle,
            self.summaryLabel,
            self.warningLabel,
            self.confirmBox,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.titleLabel)
        layout.addWidget(self.repoLabel)
        layout.addWidget(BodyLabel("名称", self))
        layout.addWidget(self.nameEdit)
        layout.addWidget(BodyLabel("标签", self))
        layout.addWidget(self.labelsEdit)
        layout.addWidget(self.psPolicyBox)
        layout.addWidget(self.startAfterBox)
        layout.addSpacing(8)
        for widget in self.summaryWidgets:
            layout.addWidget(widget)
        layout.addStretch(1)

        for signal in (self.nameEdit.textChanged, self.labelsEdit.textChanged):
            signal.connect(lambda _text: self.changed.emit())
        self.psPolicyBox.toggled.connect(lambda _on: self.changed.emit())
        self.clear_plan()

    @property
    def name(self) -> str | None:
        return self.nameEdit.text().strip() or None

    @property
    def labels(self) -> list[str]:
        return split_labels(self.labelsEdit.text())

    @property
    def ps_policy(self) -> bool:
        return self.psPolicyBox.isChecked()

    @property
    def start_after(self) -> bool:
        return self.startAfterBox.isChecked()

    @property
    def confirmed(self) -> bool:
        return not self.confirmBox.isVisibleTo(self) or self.confirmBox.isChecked()

    def set_repo(self, full_name: str) -> None:
        self.repoLabel.setText(f"仓库：{full_name}")

    def set_default_labels(self, labels: list[str]) -> None:
        self.labelsEdit.setText(", ".join(labels))

    def set_inputs_enabled(self, enabled: bool) -> None:
        for widget in (self.nameEdit, self.labelsEdit, self.psPolicyBox, self.startAfterBox):
            widget.setEnabled(enabled)

    def clear_plan(self) -> None:
        for widget in self.summaryWidgets:
            widget.hide()
        self.confirmBox.setChecked(False)

    def show_plan(self, plan: Any) -> None:
        labels = ", ".join(plan.labels) if plan.labels else "（无自定义标签）"
        version = plan.version or "最新版"
        visibility = "私有" if plan.private else "公开"
        self.summaryLabel.setText(
            f"仓库：{plan.full_name}（{visibility}）\n"
            f"名称：{plan.name}\n"
            f"目录：{plan.target_dir}\n"
            f"版本：{version}\n"
            f"标签：{labels}"
        )
        warnings = list(plan.warnings)
        self.warningLabel.setText("\n".join(f"⚠ {w}" for w in warnings))
        self.summaryTitle.show()
        self.summaryLabel.show()
        self.warningLabel.setVisible(bool(warnings))
        self.confirmBox.setChecked(False)
        self.confirmBox.setVisible(bool(plan.needs_confirmation))


class ProgressStep(QWidget):
    """Checklist of manager.Step ids with the current step spinning."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.titleLabel = SubtitleLabel("③ 安装与注册", self)
        self.rows: dict[str, tuple[IndeterminateProgressRing, IconWidget, BodyLabel]] = {}
        self.statusLabel = BodyLabel("", self)
        self.statusLabel.setWordWrap(True)
        self.outputEdit = PlainTextEdit(self)
        self.outputEdit.setReadOnly(True)
        self.outputEdit.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.titleLabel)
        for step_id, title in PROGRESS_STEPS:
            row = QHBoxLayout()
            row.setSpacing(8)
            spinner = IndeterminateProgressRing(self, start=False)
            spinner.setFixedSize(16, 16)
            spinner.setStrokeWidth(2)
            spinner.hide()
            check = IconWidget(FIF.ACCEPT, self)
            check.setFixedSize(16, 16)
            check.hide()
            placeholder = QWidget(self)
            placeholder.setFixedSize(16, 16)
            label = BodyLabel(title, self)
            row.addWidget(placeholder)
            row.addWidget(spinner)
            row.addWidget(check)
            row.addWidget(label, 1)
            layout.addLayout(row)
            self.rows[step_id] = (spinner, check, label)
            placeholder.setObjectName(f"placeholder_{step_id}")
        layout.addSpacing(8)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.outputEdit, 1)
        layout.addStretch(1)
        self.reset()

    def reset(self) -> None:
        for step_id, (spinner, check, label) in self.rows.items():
            spinner.stop()
            spinner.hide()
            check.hide()
            label.setText(dict(PROGRESS_STEPS)[step_id])
            self._placeholder(step_id).show()
        self.statusLabel.setText("")
        self.outputEdit.clear()
        self.outputEdit.hide()

    def _placeholder(self, step_id: str) -> QWidget:
        widget = self.findChild(QWidget, f"placeholder_{step_id}")
        assert widget is not None
        return widget

    def set_env_visible(self, visible: bool) -> None:
        spinner, check, label = self.rows["env"]
        for widget in (label, self._placeholder("env")):
            widget.setVisible(visible)
        if not visible:
            spinner.hide()
            check.hide()

    def on_progress(self, step: str, text: str) -> None:
        step = str(step)
        self.statusLabel.setText(text)
        order = [s for s, _ in PROGRESS_STEPS]
        if step not in self.rows:
            return  # rollback etc.: only the status line
        index = order.index(step)
        for position, step_id in enumerate(order):
            spinner, check, label = self.rows[step_id]
            placeholder = self._placeholder(step_id)
            if position < index or step == "done":
                spinner.stop()
                spinner.hide()
                placeholder.hide()
                check.setVisible(label.isVisibleTo(self))
            elif position == index:
                placeholder.hide()
                check.hide()
                spinner.show()
                spinner.start()
                label.setText(f"{dict(PROGRESS_STEPS)[step_id]}：{text}")
            else:
                spinner.stop()
                spinner.hide()
                check.hide()
                placeholder.show()

    def finish_all(self) -> None:
        self.on_progress("done", self.statusLabel.text())

    def show_failure(self, message: str, output: str) -> None:
        for spinner, _check, _label in self.rows.values():
            spinner.stop()
            spinner.hide()
        self.statusLabel.setText(f"失败：{message}")
        self.outputEdit.setPlainText(output)
        self.outputEdit.setVisible(bool(output))


class AddWizardPage(QWidget):
    """objectName "add"; emits `finished` after a successful add so the window can switch."""

    finished = pyqtSignal()

    def __init__(self, controller: RunnerController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("add")
        self._controller = controller
        self._repos_loaded = False
        self._loading_repos = False
        self._plan: Any | None = None
        self._planning = False
        self._adding = False
        self._selected_full_name = ""

        self.titleLabel = TitleLabel("添加 runner", self)
        self.stack = QStackedWidget(self)
        self.repoStep = RepoStep(self)
        self.configStep = ConfigStep(self)
        self.progressStep = ProgressStep(self)
        for step in (self.repoStep, self.configStep, self.progressStep):
            self.stack.addWidget(step)
        self.backButton = PushButton("上一步", self)
        self.nextButton = PrimaryPushButton("下一步", self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 24, 36, 24)
        layout.setSpacing(12)
        layout.addWidget(self.titleLabel)
        layout.addWidget(self.stack, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.backButton)
        buttons.addWidget(self.nextButton)
        layout.addLayout(buttons)

        self.repoStep.selection_changed.connect(self._refresh_buttons)
        self.repoStep.reload_requested.connect(self.reload_repos)
        self.configStep.changed.connect(self._invalidate_plan)
        self.configStep.confirmBox.toggled.connect(lambda _on: self._refresh_buttons())
        self.backButton.clicked.connect(self.go_back)
        self.nextButton.clicked.connect(self.go_next)

        controller.repos_loaded.connect(self._on_repos_loaded)
        controller.plan_ready.connect(self._on_plan_ready)
        controller.add_progress.connect(self._on_add_progress)
        controller.runner_added.connect(self._on_runner_added)
        controller.failed.connect(self._on_failed)

        self.reset()

    # --- state -------------------------------------------------------------------------

    @property
    def step(self) -> int:
        return self.stack.currentIndex()

    @property
    def plan(self) -> Any | None:
        return self._plan

    def reset(self) -> None:
        """Back to step 1 with empty inputs (after success or when the window asks)."""
        self._plan = None
        self._planning = False
        self._adding = False
        self._selected_full_name = ""
        self.configStep.nameEdit.clear()
        self.configStep.set_default_labels(list(self._controller.config.settings.default_labels))
        self.configStep.psPolicyBox.setChecked(True)
        self.configStep.startAfterBox.setChecked(True)
        self.configStep.clear_plan()
        self.configStep.set_inputs_enabled(True)
        self.progressStep.reset()
        self.stack.setCurrentIndex(STEP_REPO)
        self._refresh_buttons()

    def showEvent(self, event: object) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)  # type: ignore[arg-type]
        if not self._repos_loaded and not self._loading_repos:
            self.reload_repos()

    def reload_repos(self) -> None:
        if self._loading_repos:
            return
        self._loading_repos = True
        self.repoStep.set_loading(True)
        self._controller.load_repos()

    def _refresh_buttons(self) -> None:
        step = self.step
        if step == STEP_REPO:
            self.backButton.setEnabled(False)
            self.nextButton.setText("下一步")
            self.nextButton.setEnabled(self.repoStep.selected_repo() is not None)
        elif step == STEP_CONFIG:
            self.backButton.setEnabled(not self._planning)
            if self._plan is None:
                self.nextButton.setText("检查中…" if self._planning else "检查")
                self.nextButton.setEnabled(not self._planning)
            else:
                self.nextButton.setText("开始添加")
                self.nextButton.setEnabled(self.configStep.confirmed)
        else:
            self.backButton.setEnabled(not self._adding)
            self.nextButton.setText("添加中…" if self._adding else "完成")
            self.nextButton.setEnabled(not self._adding)

    # --- navigation ----------------------------------------------------------------------

    def go_next(self) -> None:
        step = self.step
        if step == STEP_REPO:
            repo = self.repoStep.selected_repo()
            if repo is None:
                return
            if repo.full_name != self._selected_full_name:
                self._selected_full_name = repo.full_name
                self._invalidate_plan()
            self.configStep.set_repo(repo.full_name)
            self.stack.setCurrentIndex(STEP_CONFIG)
        elif step == STEP_CONFIG:
            if self._plan is None:
                self._request_plan()
            elif self.configStep.confirmed:
                self._start_add()
        else:
            self.finished.emit()
            self.reset()
        self._refresh_buttons()

    def go_back(self) -> None:
        step = self.step
        if step == STEP_CONFIG and not self._planning:
            self.stack.setCurrentIndex(STEP_REPO)
        elif step == STEP_PROGRESS and not self._adding:
            self.progressStep.reset()
            self._invalidate_plan()
            self.stack.setCurrentIndex(STEP_CONFIG)
        self._refresh_buttons()

    def _invalidate_plan(self) -> None:
        if self._adding:
            return
        self._plan = None
        self.configStep.clear_plan()
        self._refresh_buttons()

    def _request_plan(self) -> None:
        owner, _, repo = self._selected_full_name.partition("/")
        self._planning = True
        self._refresh_buttons()
        self._controller.plan_add(
            owner,
            repo,
            name=self.configStep.name,
            labels=self.configStep.labels,
            ps_policy=self.configStep.ps_policy,
        )

    def _start_add(self) -> None:
        assert self._plan is not None
        self._adding = True
        self.configStep.set_inputs_enabled(False)
        self.progressStep.reset()
        self.progressStep.set_env_visible(bool(getattr(self._plan, "runner_env", ())))
        self.stack.setCurrentIndex(STEP_PROGRESS)
        self._refresh_buttons()
        self._controller.add_runner(self._plan, start_after=self.configStep.start_after)

    # --- controller signals ------------------------------------------------------------

    def _on_repos_loaded(self, repos: object) -> None:
        self._loading_repos = False
        self._repos_loaded = True
        local = {r.full_name for r in self._controller.records}
        self.repoStep.set_repos(list(repos), local)  # type: ignore[arg-type]
        self._refresh_buttons()

    def _on_plan_ready(self, plan: object) -> None:
        if not self._planning:
            return
        self._planning = False
        self._plan = plan
        self.configStep.show_plan(plan)
        for warning in plan.warnings:  # type: ignore[attr-defined]
            InfoBar.warning(
                "请注意", warning, parent=self, position=InfoBarPosition.TOP, duration=8000
            )
        self._refresh_buttons()

    def _on_add_progress(self, step: str, text: str) -> None:
        if self._adding:
            self.progressStep.on_progress(step, text)

    def _on_runner_added(self, record: object) -> None:
        if not self._adding:
            return
        self._adding = False
        self.progressStep.finish_all()
        name = getattr(record, "name", "")
        InfoBar.success(
            "添加成功",
            f"runner {name} 已注册",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=SUCCESS_RETURN_DELAY_MS,
        )
        self._refresh_buttons()
        QTimer.singleShot(SUCCESS_RETURN_DELAY_MS, self._return_after_success)

    def _return_after_success(self) -> None:
        if self._adding or self.step != STEP_PROGRESS:
            return  # the user already moved on
        self.finished.emit()
        self.reset()

    def _on_failed(self, op: str, message: str, exc: object = None) -> None:
        if op == "repos":
            self._loading_repos = False
            self.repoStep.set_error(f"加载仓库失败：{message}")
        elif op == "plan_add":
            self._planning = False
            self._plan = None
            self.configStep.clear_plan()
            InfoBar.error(
                "检查失败", message, parent=self, position=InfoBarPosition.TOP, duration=8000
            )
        elif op == "add":
            if not self._adding:
                return
            self._adding = False
            self.configStep.set_inputs_enabled(True)
            output = getattr(exc, "output", "") or ""
            self.progressStep.show_failure(message, str(output))
            InfoBar.error(
                "添加失败", message, parent=self, position=InfoBarPosition.TOP, duration=-1
            )
        else:
            return
        self._refresh_buttons()
