"""Overview page: counters, start/stop-all, refresh and one RunnerCard per record."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    PrimaryPushButton,
    PushButton,
    SmoothScrollArea,
    TitleLabel,
)
from qfluentwidgets import FluentIcon as FIF

from runnerdeck.core.models import DisplayState, LocalState
from runnerdeck.ui.components.log_dialog import LogDialog
from runnerdeck.ui.components.runner_card import RunnerCard

if TYPE_CHECKING:
    from runnerdeck.app.controller import RunnerController

EMPTY_TEXT = "还没有 runner，去“添加 runner”页注册一个"

# Which counter a display state belongs to (docs/m2-design.md §5: 运行中 / 空闲 / 异常 / 已停止).
RUNNING_STATES = frozenset(
    {
        DisplayState.BUSY,
        DisplayState.STARTING,
        DisplayState.CONNECTING,
        DisplayState.RECONNECTING,
        DisplayState.STOPPING,
    }
)
TROUBLE_STATES = frozenset(
    {DisplayState.STALLED, DisplayState.ERROR, DisplayState.MISSING, DisplayState.EXTERNAL}
)


def summarize(states: Iterable[DisplayState]) -> str:
    running = idle = trouble = stopped = 0
    for state in states:
        if state is DisplayState.IDLE:
            idle += 1
        elif state in RUNNING_STATES:
            running += 1
        elif state in TROUBLE_STATES:
            trouble += 1
        else:
            stopped += 1
    return f"运行中 {running} · 空闲 {idle} · 异常 {trouble} · 已停止 {stopped}"


class DashboardPage(QWidget):
    """objectName "dashboard"; rebuilt from controller.records on every config_changed."""

    def __init__(self, controller: RunnerController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dashboard")
        self._controller = controller
        self.cards: dict[str, RunnerCard] = {}
        self._refreshing = False

        self.titleLabel = TitleLabel("概览", self)
        self.summaryLabel = BodyLabel("", self)
        self.startAllButton = PrimaryPushButton(FIF.PLAY, "全部启动", self)
        self.stopAllButton = PushButton(FIF.PAUSE, "全部停止", self)
        self.refreshButton = PushButton(FIF.SYNC, "刷新", self)
        self.refreshButton.setToolTip("从 GitHub 拉取每个 runner 的在线状态")
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

        self.startAllButton.clicked.connect(controller.start_all)
        self.stopAllButton.clicked.connect(controller.stop_all)
        self.refreshButton.clicked.connect(self.refresh)

        controller.config_changed.connect(self.rebuild)
        controller.runner_state.connect(self._on_runner_state)
        controller.busy_changed.connect(self._on_busy_changed)
        controller.statuses_updated.connect(self._on_statuses_updated)
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
        header.addWidget(self.startAllButton)
        header.addWidget(self.stopAllButton)
        header.addWidget(self.refreshButton)
        layout.addLayout(header)
        layout.addWidget(self.emptyLabel)
        layout.addWidget(self.scrollArea, 1)

    # --- building ----------------------------------------------------------------

    def rebuild(self, _config: object = None) -> None:
        """Throw the cards away and create one per controller.records (cheap; few runners)."""
        for card in self.cards.values():
            self.cardLayout.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self.cards = {}
        for index, record in enumerate(self._controller.records):
            card = RunnerCard(record, self.cardContainer)
            state, job = self._controller.local_state(record.id)
            card.update_state(state, job)
            card.update_display(self._controller.display(record.id))
            card.set_busy(self._controller.is_busy(record.id))
            card.start_requested.connect(self._controller.start)
            card.stop_requested.connect(self._controller.stop)
            card.remove_requested.connect(self._on_remove_requested)
            card.logs_requested.connect(self.show_logs)
            card.open_dir_requested.connect(self._controller.open_dir)
            self.cardLayout.insertWidget(index, card)
            self.cards[record.id] = card
        self.emptyLabel.setVisible(not self.cards)
        self.scrollArea.setVisible(bool(self.cards))
        self._update_summary()

    def _update_summary(self) -> None:
        self.summaryLabel.setText(summarize(c.display_state for c in self.cards.values()))
        has_cards = bool(self.cards)
        self.startAllButton.setEnabled(has_cards)
        self.stopAllButton.setEnabled(has_cards)

    # --- controller signals ---------------------------------------------------------

    def _on_runner_state(self, record_id: str, state: object, job: object) -> None:
        card = self.cards.get(record_id)
        if card is None:
            return
        card.update_state(LocalState(state), job if isinstance(job, str) else None)
        self._update_summary()

    def _on_busy_changed(self, record_id: str, busy: bool) -> None:
        card = self.cards.get(record_id)
        if card is not None:
            card.set_busy(busy)

    def _on_statuses_updated(self, rows: object) -> None:
        self._set_refreshing(False)
        for row in rows:  # type: ignore[attr-defined]
            card = self.cards.get(row.record.id)
            if card is not None:
                card.update_display(row.display)
        self._update_summary()

    def _on_failed(self, op: str, _message: str, _exc: object = None) -> None:
        if op == "status":
            self._set_refreshing(False)

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

    def _on_remove_requested(self, record_id: str, keep_work: bool) -> None:
        self._controller.remove(record_id, keep_work=keep_work)

    def show_logs(self, record_id: str) -> None:
        card = self.cards.get(record_id)
        if card is None:
            return
        dialog = LogDialog(self._controller, record_id, card.record.name, self.window())
        dialog.exec()
