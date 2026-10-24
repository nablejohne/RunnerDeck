"""Overview page: counters, import / start-all / stop-all / refresh, a discovery banner and one
RunnerCard per record (docs/m2-design.md §5, docs/m3-design.md §5).

Badges and counters follow controller.display_changed; runner_state only drives the buttons.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QFileDialog, QFrame, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    MessageBox,
    PrimaryPushButton,
    PushButton,
    SmoothScrollArea,
    StrongBodyLabel,
    TitleLabel,
    TransparentToolButton,
)
from qfluentwidgets import FluentIcon as FIF

from runnerdeck.core.models import Discovery, DisplayState, LocalState, RunnerRecord, StrayRunner
from runnerdeck.core.status import Bucket, DisplayStatus, bucket
from runnerdeck.ui.components.runner_card import RunnerCard, confirm_stop

if TYPE_CHECKING:
    from runnerdeck.app.controller import RunnerController

EMPTY_TEXT = "还没有 runner，去“添加 runner”页注册一个，或点“导入”登记已有的 runner 目录"
SERVICE_TOOLTIP = "这个 runner 以 Windows 服务方式运行，请在“服务”里停止（服务模式 M4 支持）"


def summarize(states: Iterable[DisplayState]) -> str:
    """Counter line; groups come from status.bucket (same as the tray colour)."""
    counts = dict.fromkeys(Bucket, 0)
    for state in states:
        counts[bucket(state)] += 1
    return (
        f"运行中 {counts[Bucket.RUNNING]} · 空闲 {counts[Bucket.IDLE]} · "
        f"异常 {counts[Bucket.TROUBLE]} · 已停止 {counts[Bucket.STOPPED]}"
    )


def _same_path(a: Path | str, b: Path | str) -> bool:
    return Path(a).as_posix().casefold().rstrip("/") == Path(b).as_posix().casefold().rstrip("/")


def stray_text(stray: StrayRunner) -> str:
    pids = "、".join(str(pid) for pid in stray.pids) or "?"
    tags = [f"PID {pids}"]
    if not stray.configured:
        tags.append("未配置")
    if stray.service:
        tags.append("Windows 服务")
    return f"{stray.path}（{' · '.join(tags)}）"


class DiscoveryBanner(QFrame):
    """What controller.discover() found: unregistered runner dirs (导入) and strays (结束)."""

    import_requested = pyqtSignal(str)  # directory
    kill_requested = pyqtSignal(str)  # stray runner directory

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("discoveryBanner")
        self.setStyleSheet(
            "#discoveryBanner{border:1px solid rgba(128,128,128,0.35);border-radius:6px}"
        )
        self.discovery: Discovery | None = None
        self.dismissed = False
        self.titleLabel = StrongBodyLabel("", self)
        self.strayLabel = StrongBodyLabel("", self)
        self.closeButton = TransparentToolButton(FIF.CLOSE, self)
        self.closeButton.setToolTip("暂时隐藏（下次扫描有结果时再显示）")
        self.closeButton.clicked.connect(self.dismiss)
        self.importButtons: dict[str, PushButton] = {}
        self.killButtons: dict[str, PushButton] = {}
        self._rows: list[QWidget] = []

        self.vBoxLayout = QVBoxLayout(self)
        self.vBoxLayout.setContentsMargins(16, 10, 8, 10)
        self.vBoxLayout.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(self.titleLabel, 1)
        head.addWidget(self.closeButton, 0, Qt.AlignTop)
        self.vBoxLayout.addLayout(head)
        self.unregisteredBox = QVBoxLayout()
        self.vBoxLayout.addLayout(self.unregisteredBox)
        self.vBoxLayout.addWidget(self.strayLabel)
        self.strayBox = QVBoxLayout()
        self.vBoxLayout.addLayout(self.strayBox)
        self.hide()

    def set_discovery(self, discovery: Discovery | None) -> None:
        """A new scan result (controller.discovery_updated): shown again even if dismissed."""
        self.dismissed = False
        self._render(discovery)

    def dismiss(self) -> None:
        self.dismissed = True
        self.hide()

    def without(self, *, imported: str | None = None, killed: str | None = None) -> None:
        """Drop one entry (an imported directory / a stray that is gone) and redraw."""
        found = self.discovery
        if found is None:
            return
        unregistered = tuple(
            p for p in found.unregistered if imported is None or not _same_path(p, imported)
        )
        strays = tuple(s for s in found.strays if killed is None or not _same_path(s.path, killed))
        if unregistered != found.unregistered or strays != found.strays:
            self._render(Discovery(root=found.root, unregistered=unregistered, strays=strays))

    def _render(self, discovery: Discovery | None) -> None:
        self.discovery = discovery
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows = []
        self.importButtons = {}
        self.killButtons = {}
        if discovery is None or discovery.empty:
            self.hide()
            return
        unregistered = list(discovery.unregistered)
        strays = list(discovery.strays)
        stray_title = f"发现 {len(strays)} 个不属于 RunnerDeck 的 runner 进程"
        if unregistered:
            self.titleLabel.setText(
                f"在 runner 根目录发现 {len(unregistered)} 个未登记的 runner 目录"
            )
        else:
            self.titleLabel.setText(stray_title)
        self.strayLabel.setText(stray_title)
        self.strayLabel.setVisible(bool(strays) and bool(unregistered))
        for path in unregistered:
            button = PushButton(FIF.FOLDER_ADD, "导入")
            button.clicked.connect(lambda _=False, p=str(path): self.import_requested.emit(p))
            self.importButtons[str(path)] = button
            self._add_row(self.unregisteredBox, str(path), button)
        for stray in strays:
            button = PushButton(FIF.CLOSE, "结束")
            button.clicked.connect(lambda _=False, p=str(stray.path): self.kill_requested.emit(p))
            if stray.service:
                button.setEnabled(False)
                button.setToolTip(SERVICE_TOOLTIP)
            self.killButtons[str(stray.path)] = button
            self._add_row(self.strayBox, stray_text(stray), button)
        self.setVisible(not self.dismissed)

    def _add_row(self, box: QVBoxLayout, text: str, button: PushButton) -> None:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        label = CaptionLabel(text, row)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        button.setParent(row)
        layout.addWidget(label, 1)
        layout.addWidget(button)
        box.addWidget(row)
        self._rows.append(row)


class DashboardPage(QWidget):
    """objectName "dashboard"; rebuilt from controller.records on every config_changed."""

    detail_requested = pyqtSignal(str)  # record id; the window opens the detail page

    def __init__(self, controller: RunnerController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dashboard")
        self._controller = controller
        self.cards: dict[str, RunnerCard] = {}
        self._refreshing = False

        self.titleLabel = TitleLabel("概览", self)
        self.summaryLabel = BodyLabel("", self)
        self.importButton = PushButton(FIF.FOLDER_ADD, "导入", self)
        self.importButton.setToolTip("登记一个已经配置好的 runner 目录（不重新注册）")
        self.startAllButton = PrimaryPushButton(FIF.PLAY, "全部启动", self)
        self.stopAllButton = PushButton(FIF.PAUSE, "全部停止", self)
        self.refreshButton = PushButton(FIF.SYNC, "刷新", self)
        self.refreshButton.setToolTip("从 GitHub 拉取每个 runner 的在线状态")
        self.banner = DiscoveryBanner(self)
        self.emptyLabel = BodyLabel(EMPTY_TEXT, self)
        self.emptyLabel.setAlignment(Qt.AlignCenter)

        self.scrollArea = SmoothScrollArea(self)
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setStyleSheet("QScrollArea{background:transparent;border:none}")
        self.scrollArea.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cardContainer = QWidget(self.scrollArea)
        self.cardContainer.setStyleSheet("QWidget{background:transparent}")
        self.cardLayout = QVBoxLayout(self.cardContainer)
        self.cardLayout.setContentsMargins(0, 0, 8, 0)
        self.cardLayout.setSpacing(8)
        self.cardLayout.addStretch(1)
        self.scrollArea.setWidget(self.cardContainer)

        self._build_layout()

        self.importButton.clicked.connect(self.choose_import)
        self.startAllButton.clicked.connect(controller.start_all)
        self.stopAllButton.clicked.connect(self.ask_stop_all)
        self.refreshButton.clicked.connect(self.refresh)
        self.banner.import_requested.connect(self.import_dir)
        self.banner.kill_requested.connect(self.ask_kill_stray)

        controller.config_changed.connect(self.rebuild)
        controller.runner_state.connect(self._on_runner_state)
        controller.display_changed.connect(self._on_display_changed)
        controller.busy_changed.connect(self._on_busy_changed)
        controller.statuses_updated.connect(self._on_statuses_updated)
        controller.refreshing_changed.connect(self._on_refreshing_changed)
        controller.discovery_updated.connect(self._on_discovery_updated)
        controller.runner_imported.connect(self._on_runner_imported)
        controller.external_stopped.connect(self._on_external_stopped)
        controller.failed.connect(self._on_failed)

        self.rebuild()

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 24, 36, 24)
        layout.setSpacing(12)
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(self.titleLabel)
        header.addSpacing(16)
        header.addWidget(self.summaryLabel, 1)
        header.addWidget(self.importButton)
        header.addWidget(self.startAllButton)
        header.addWidget(self.stopAllButton)
        header.addWidget(self.refreshButton)
        layout.addLayout(header)
        layout.addWidget(self.banner)
        layout.addWidget(self.emptyLabel)
        layout.addWidget(self.scrollArea, 1)

    # --- building ----------------------------------------------------------------

    def rebuild(self, _config: object = None) -> None:
        """One card per controller.records, in order.

        Cards whose record still exists are kept and updated in place: a card may have a
        confirmation dialog open (its nested event loop runs while config_changed arrives), and
        deleting it then would make the card emit from a destroyed object after exec() returns.
        """
        controller = self._controller
        records = controller.records
        wanted = {record.id for record in records}
        for record_id in [rid for rid in self.cards if rid not in wanted]:
            card = self.cards.pop(record_id)
            self.cardLayout.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        for index, record in enumerate(records):
            card = self.cards.get(record.id)
            if card is None:
                card = self._new_card(record)
                self.cards[record.id] = card
            else:
                card.set_record(record)
                self.cardLayout.removeWidget(card)
            self.cardLayout.insertWidget(index, card)
            state, job = controller.local_state(record.id)
            card.update_state(state, job)
            card.update_display(controller.display(record.id))
            card.set_busy(controller.is_busy(record.id))
        self.emptyLabel.setVisible(not self.cards)
        self.scrollArea.setVisible(bool(self.cards))
        self._update_summary()

    def _new_card(self, record: RunnerRecord) -> RunnerCard:
        controller = self._controller
        card = RunnerCard(record, self.cardContainer, remote_busy=controller.remote_busy)
        card.start_requested.connect(controller.start)
        card.stop_requested.connect(controller.stop)
        card.remove_requested.connect(self._on_remove_requested)
        card.open_dir_requested.connect(controller.open_dir)
        card.detail_requested.connect(self.detail_requested)
        card.take_over_requested.connect(controller.take_over)
        card.kill_requested.connect(controller.kill_external)
        card.auto_start_toggled.connect(controller.set_auto_start)
        card.forget_requested.connect(controller.forget)
        card.convert_requested.connect(controller.convert_to_service)
        card.grant_requested.connect(controller.grant_service_control)
        return card

    def _update_summary(self) -> None:
        self.summaryLabel.setText(summarize(c.display_state for c in self.cards.values()))
        has_cards = bool(self.cards)
        self.startAllButton.setEnabled(has_cards)
        self.stopAllButton.setEnabled(has_cards)

    # --- controller signals ---------------------------------------------------------

    def _on_runner_state(self, record_id: str, state: object, job: object) -> None:
        card = self.cards.get(record_id)
        if card is not None:
            card.update_state(LocalState(state), job if isinstance(job, str) else None)

    def _on_display_changed(self, record_id: str, display: object) -> None:
        card = self.cards.get(record_id)
        if card is None or not isinstance(display, DisplayStatus):
            return
        card.update_display(display)
        self._update_summary()

    def _on_busy_changed(self, record_id: str, busy: bool) -> None:
        card = self.cards.get(record_id)
        if card is not None:
            card.set_busy(busy)

    def _on_statuses_updated(self, _rows: object) -> None:
        # The badges follow display_changed; a finished poll only re-enables the button.
        self._set_refreshing(False)

    def _on_refreshing_changed(self, refreshing: bool) -> None:
        if not refreshing:
            self._set_refreshing(False)

    def _on_failed(self, op: str, _message: str, _exc: object = None) -> None:
        if op == "status":
            self._set_refreshing(False)

    def _on_discovery_updated(self, discovery: object) -> None:
        self.banner.set_discovery(discovery if isinstance(discovery, Discovery) else None)

    def _on_runner_imported(self, record: object) -> None:
        path = getattr(record, "path", None)
        if path:
            self.banner.without(imported=str(path))

    def _on_external_stopped(self, key: str, result: object) -> None:
        if getattr(result, "ok", False):
            self.banner.without(killed=key)

    # --- actions ----------------------------------------------------------------------

    def refresh(self) -> None:
        if self._refreshing:
            return
        self._set_refreshing(True)
        self._controller.refresh_statuses()

    def _set_refreshing(self, refreshing: bool) -> None:
        self._refreshing = refreshing
        self.refreshButton.setEnabled(not refreshing)
        self.refreshButton.setText("刷新中…" if refreshing else "刷新")

    def busy_jobs(self) -> list[tuple[str, str | None]]:
        """(name, job) of every runner whose local state is BUSY."""
        busy: list[tuple[str, str | None]] = []
        for record in self._controller.records:
            state, job = self._controller.local_state(record.id)
            if state is LocalState.BUSY:
                busy.append((record.name, job))
        return busy

    def ask_stop_all(self) -> None:
        if confirm_stop(self.busy_jobs(), self.window()):
            self._controller.stop_all()

    def _on_remove_requested(self, record_id: str, keep_work: bool) -> None:
        self._controller.remove(record_id, keep_work=keep_work)

    def choose_import(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "选择要导入的 runner 目录", self._controller.config.settings.runners_root
        )
        if chosen:
            self.import_dir(chosen)

    def import_dir(self, path: str) -> None:
        self._controller.import_runner(path)

    def ask_kill_stray(self, path: str) -> None:
        box = MessageBox(
            "结束 runner 进程",
            f"将强制结束目录 {path} 下的 runner 进程（它不归 RunnerDeck 管理）。\n"
            "GitHub 端旧会话约 3 分钟后过期；如果它正在执行 job，job 会失败。要结束吗？",
            self.window(),
        )
        box.yesButton.setText("结束进程")
        box.cancelButton.setText("取消")
        if box.exec():
            self._controller.kill_stray(path)
