"""System tray icon: status colour, quick start / stop menu, notifications (docs/m3-design.md §5).

Lives in ui/ (not app/) because the menu is a qfluentwidgets SystemTrayMenu. The menu is rebuilt
as a whole in `aboutToShow`, so every entry reflects the state at the moment it opens; there are
no sub-menus (RoundMenu opens those with show(), which never fires aboutToShow).
Every modal confirmation first brings the main window back (`window.show_and_raise()`): a
MessageBox is an overlay on its parent window and would block invisibly on a hidden one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from PyQt5.QtCore import QObject, QRectF, Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QSystemTrayIcon
from qfluentwidgets import Action, SystemTrayMenu
from qfluentwidgets import FluentIcon as FIF

from runnerdeck.core.models import Discovery, DisplayState, LocalState
from runnerdeck.core.status import DisplayStatus, TrayLevel, is_job_failure, tray_level
from runnerdeck.ui.components.runner_card import confirm_stop
from runnerdeck.ui.pages.dashboard_page import summarize

if TYPE_CHECKING:
    from runnerdeck.app.controller import RunnerController
    from runnerdeck.ui.main_window import MainWindow

log = logging.getLogger(__name__)

APP_NAME = "RunnerDeck"
ICON_PATH = Path(__file__).resolve().parent / "resources" / "runnerdeck.png"
TRAY_COLORS: Mapping[TrayLevel, str] = MappingProxyType(
    {
        TrayLevel.STOPPED: "#8a8a8a",
        TrayLevel.ONLINE: "#2ea043",
        TrayLevel.BUSY: "#1f6feb",
        TrayLevel.ERROR: "#d1242f",
    }
)
ICON_SIZE = 64
DOT_RATIO = 0.42  # dot diameter / icon size
# Windows limits (NOTIFYICONDATA): tooltip 128, balloon title 64, balloon text 256 (with NUL).
TOOLTIP_MAX = 127
TITLE_MAX = 63
MESSAGE_MAX = 255
MESSAGE_MS = 10000
JOB_RESULT_TEXT: Mapping[str, str] = MappingProxyType(
    {
        "succeeded": "成功",
        "succeededwithissues": "成功（有警告）",
        "failed": "失败",
        "canceled": "已取消",
        "cancelled": "已取消",
        "abandoned": "已放弃",
        "skipped": "已跳过",
    }
)
NO_RUNNERS = "还没有 runner"


def truncate(text: str, limit: int) -> str:
    """Cut `text` to at most `limit` characters, ending with "…" when shortened."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def job_result_text(result: str) -> str:
    return JOB_RESULT_TEXT.get(result.strip().casefold(), result or "未知")


def tooltip_text(states: Iterable[DisplayState]) -> str:
    shown = list(states)
    body = summarize(shown) if shown else NO_RUNNERS
    return truncate(f"{APP_NAME}\n{body}", TOOLTIP_MAX)


def status_icon(base: QIcon, level: TrayLevel, size: int = ICON_SIZE) -> QIcon:
    """`base` with a coloured status dot in the bottom-right corner (drawn, no image files)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        if not base.isNull():
            painter.drawPixmap(0, 0, base.pixmap(size, size))
        diameter = size * DOT_RATIO
        border = max(1.0, size / 16)
        rect = QRectF(size - diameter - 0.5, size - diameter - 0.5, diameter, diameter)
        painter.setPen(QPen(QColor("#ffffff"), border))
        painter.setBrush(QColor(TRAY_COLORS[level]))
        painter.drawEllipse(rect.adjusted(border / 2, border / 2, -border / 2, -border / 2))
    finally:
        painter.end()
    return QIcon(pixmap)


class TrayIcon(QSystemTrayIcon):
    """Tray icon of the main window; `window` provides show_and_raise() / quit_app()."""

    def __init__(
        self, controller: RunnerController, window: MainWindow, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._window = window
        icon = window.windowIcon()
        if icon.isNull() and ICON_PATH.is_file():
            icon = QIcon(str(ICON_PATH))
        self._base_icon = icon
        self._level: TrayLevel | None = None
        self._states: dict[str, DisplayState] = {}
        self._seen_discovery: set[str] = set()
        self._menu_actions: list[Action] = []

        self.menu = SystemTrayMenu(parent=window)
        self.menu.aboutToShow.connect(self.rebuild_menu)
        self.setContextMenu(self.menu)
        self.activated.connect(self._on_activated)
        self.messageClicked.connect(window.show_and_raise)

        controller.display_changed.connect(self._on_display_changed)
        controller.config_changed.connect(self._on_config_changed)
        controller.runner_job_finished.connect(self._on_job_finished)
        controller.discovery_updated.connect(self._on_discovery_updated)

        self._states = {rid: d.state for rid, d in controller.all_displays().items()}
        self.refresh()
        self.rebuild_menu()

    # --- icon / tooltip -------------------------------------------------------------------

    @property
    def level(self) -> TrayLevel:
        return self._level if self._level is not None else TrayLevel.STOPPED

    def refresh(self) -> None:
        """Recompute colour and tooltip from controller.all_displays()."""
        states = [d.state for d in self._controller.all_displays().values()]
        level = tray_level(states)
        if level is not self._level:
            self._level = level
            self.setIcon(status_icon(self._base_icon, level))
        self.setToolTip(tooltip_text(states))

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._window.show_and_raise()

    # --- menu ----------------------------------------------------------------------------

    def rebuild_menu(self) -> None:
        """Rebuild every entry from the current state (connected to aboutToShow)."""
        for action in self._menu_actions:
            self.menu.removeAction(action)
            action.deleteLater()
        self._menu_actions = []
        self.menu.view.clear()  # separators are plain list items, not actions

        controller = self._controller
        records = controller.records
        open_window = self._action(FIF.HOME, "打开主界面", self._window.show_and_raise)
        start_all = self._action(FIF.PLAY, "全部启动", controller.start_all)
        stop_all = self._action(FIF.PAUSE, "全部停止", self.stop_all)
        start_all.setEnabled(bool(records))
        stop_all.setEnabled(bool(controller.running_ids()))
        self.menu.addAction(open_window)
        self.menu.addSeparator()
        self.menu.addActions([start_all, stop_all])
        if records:
            self.menu.addSeparator()
        for record in records:
            rid = record.id
            if controller.is_running(rid):
                label = controller.display(rid).label
                action = self._action(
                    FIF.PAUSE, f"停止 {record.name}（{label}）", lambda _=False, r=rid: self.stop(r)
                )
            elif controller.display(rid).state is DisplayState.EXTERNAL:
                # Starting would fail; taking over needs a confirmation: open the detail page.
                action = self._action(
                    FIF.SYNC, f"接管 {record.name}…", lambda _=False, r=rid: self.open_detail(r)
                )
            else:
                action = self._action(
                    FIF.PLAY, f"启动 {record.name}", lambda _=False, r=rid: controller.start(r)
                )
            action.setEnabled(not controller.is_busy(rid))
            self.menu.addAction(action)
        self.menu.addSeparator()
        self.menu.addAction(self._action(FIF.POWER_BUTTON, "退出", self._window.quit_app))

    def _action(self, icon: FIF, text: str, slot: object) -> Action:
        action = Action(icon, text, self.menu)
        action.triggered.connect(slot)  # type: ignore[arg-type]
        self._menu_actions.append(action)
        return action

    @property
    def menu_actions(self) -> list[Action]:
        return list(self._menu_actions)

    def open_detail(self, record_id: str) -> None:
        self._window.show_and_raise()
        self._window.open_detail(record_id)

    def stop(self, record_id: str) -> None:
        """Stop one runner; one executing a job asks first (in the main window)."""
        state, job = self._controller.local_state(record_id)
        if state is LocalState.BUSY:
            self._window.show_and_raise()
            if not confirm_stop([(self._name_of(record_id), job)], self._window):
                return
        self._controller.stop(record_id)

    def stop_all(self) -> None:
        busy: list[tuple[str, str | None]] = []
        for record in self._controller.records:
            state, job = self._controller.local_state(record.id)
            if state is LocalState.BUSY:
                busy.append((record.name, job))
        if busy:
            self._window.show_and_raise()
            if not confirm_stop(busy, self._window):
                return
        self._controller.stop_all()

    # --- notifications -------------------------------------------------------------------

    def notify(
        self,
        title: str,
        message: str,
        icon: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.Information,
    ) -> None:
        """Balloon / toast notification (title ≤ 63, text ≤ 255 characters)."""
        log.info("tray notification: %s - %s", title, message)
        self.showMessage(
            truncate(title, TITLE_MAX), truncate(message, MESSAGE_MAX), icon, MESSAGE_MS
        )

    def _name_of(self, record_id: str) -> str:
        record = next((r for r in self._controller.records if r.id == record_id), None)
        return record.name if record is not None else record_id

    def _on_job_finished(self, record_id: str, job: str, result: str) -> None:
        mode = self._controller.config.settings.notify_jobs
        failure = is_job_failure(result)
        if mode == "off" or (mode == "failed" and not failure):
            return
        self.notify(
            truncate(f"{self._name_of(record_id)}：job {job_result_text(result)}", TITLE_MAX),
            job or "（job 名字未知）",
            QSystemTrayIcon.Critical if failure else QSystemTrayIcon.Information,
        )

    def _on_display_changed(self, record_id: str, display: object) -> None:
        if isinstance(display, DisplayStatus):
            previous = self._states.get(record_id)
            self._states[record_id] = display.state
            if (
                display.state is DisplayState.ERROR
                and previous is not DisplayState.ERROR
                and self._controller.config.settings.notify_jobs != "off"
            ):
                self.notify(
                    f"{self._name_of(record_id)} 异常",
                    display.detail or display.label,
                    QSystemTrayIcon.Critical,
                )
        self.refresh()

    def _on_config_changed(self, _config: object = None) -> None:
        known = {r.id for r in self._controller.records}
        self._states = {rid: s for rid, s in self._states.items() if rid in known}
        self.refresh()

    def _on_discovery_updated(self, discovery: object) -> None:
        if not isinstance(discovery, Discovery):
            return
        dirs = {f"dir:{p}" for p in discovery.unregistered}
        strays = {f"stray:{s.path}" for s in discovery.strays}
        new_dirs = dirs - self._seen_discovery
        new_strays = strays - self._seen_discovery
        self._seen_discovery = dirs | strays
        window = self._window
        if window.isVisible() and not window.isMinimized():
            return  # the dashboard banner shows it
        parts = []
        if new_dirs:
            parts.append(f"在 runner 根目录发现 {len(new_dirs)} 个未登记的 runner 目录")
        if new_strays:
            parts.append(f"发现 {len(new_strays)} 个不属于 RunnerDeck 的 runner 进程")
        if parts:
            self.notify("RunnerDeck 发现了 runner", "；".join(parts) + "。点这里打开主界面查看。")
