"""Main window: navigation, error InfoBars, login prompt and graceful shutdown on close.

Construction is cheap (no network, no subprocess): `python -m runnerdeck --smoke-test` and the
UI tests build the whole window offscreen. Everything slow goes through the controller.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QCloseEvent, QIcon, QShowEvent
from PyQt5.QtWidgets import QApplication, QWidget
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    FluentWindow,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    NavigationItemPosition,
    StateToolTip,
)

from runnerdeck.ui.components.login_dialog import LoginDialog
from runnerdeck.ui.pages.add_wizard_page import AddWizardPage
from runnerdeck.ui.pages.dashboard_page import DashboardPage
from runnerdeck.ui.pages.settings_page import SettingsPage

if TYPE_CHECKING:
    from runnerdeck.app.controller import RunnerController

log = logging.getLogger(__name__)

ICON_PATH = Path(__file__).resolve().parent / "resources" / "runnerdeck.png"
WINDOW_TITLE = "RunnerDeck"
# controller.failed(op, message): op -> InfoBar title.
FAILED_TITLES: Mapping[str, str] = MappingProxyType(
    {
        "login": "登录失败",
        "logout": "退出登录失败",
        "account": "读取账号信息失败",
        "repos": "加载仓库失败",
        "plan_add": "检查失败",
        "add": "添加失败",
        "start": "启动失败",
        "stop": "停止失败",
        "remove": "移除失败",
        "status": "刷新状态失败",
        "settings": "保存设置失败",
    }
)
DEFAULT_FAILED_TITLE = "操作失败"
ERROR_BAR_MS = 10000


def exit_question(count: int) -> str:
    return f"有 {count} 个 runner 正在运行，退出会先停止它们（执行中的 job 会被取消）。要退出吗？"


class MainWindow(FluentWindow):
    """FluentWindow with 概览 / 添加 runner / 设置."""

    def __init__(
        self,
        controller: RunnerController,
        parent: QWidget | None = None,
        *,
        prompt_login: bool = True,
    ) -> None:
        """prompt_login=False skips the modal login dialog on first show (`--smoke-test`)."""
        super().__init__(parent)
        self._controller = controller
        self._shown_once = False
        self._shutting_down = False
        self._prompt_login = prompt_login

        self.dashboardPage = DashboardPage(controller, self)
        self.addPage = AddWizardPage(controller, self)
        self.settingsPage = SettingsPage(controller, self)
        self.addSubInterface(self.dashboardPage, FIF.HOME, "概览")
        self.addSubInterface(self.addPage, FIF.ADD, "添加 runner")
        self.addSubInterface(
            self.settingsPage, FIF.SETTING, "设置", position=NavigationItemPosition.BOTTOM
        )

        self.setWindowTitle(WINDOW_TITLE)
        if ICON_PATH.is_file():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(960, 640)
        self.navigationInterface.setExpandWidth(200)

        self.addPage.finished.connect(lambda: self.switchTo(self.dashboardPage))
        controller.failed.connect(self._on_failed)
        controller.logged_in.connect(self._on_logged_in)
        controller.runner_removed.connect(self._on_runner_removed)
        controller.runner_stopped.connect(self._on_runner_stopped)

    # --- feedback ------------------------------------------------------------------------

    def _on_failed(self, op: str, message: str, _exc: object = None) -> None:
        InfoBar.error(
            FAILED_TITLES.get(op, DEFAULT_FAILED_TITLE),
            message,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=ERROR_BAR_MS,
        )

    def _on_logged_in(self, user: object) -> None:
        login = getattr(user, "login", "")
        InfoBar.success(
            "已登录", f"GitHub 账号：{login}", parent=self, position=InfoBarPosition.TOP
        )

    def _on_runner_removed(self, _record_id: str, result: object) -> None:
        record = getattr(result, "record", None)
        name = getattr(record, "name", "")
        warnings = list(getattr(result, "warnings", ()))
        if warnings:
            InfoBar.warning(
                f"已移除 runner {name}",
                "\n".join(warnings),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=ERROR_BAR_MS,
            )
        else:
            InfoBar.success(
                "已移除", f"runner {name} 已注销并删除", parent=self, position=InfoBarPosition.TOP
            )

    def _on_runner_stopped(self, _record_id: str, result: object) -> None:
        if getattr(result, "graceful", True):
            return
        InfoBar.warning(
            "已强制停止",
            "runner 没有响应优雅停止，已被强制结束；GitHub 端旧会话约 3 分钟后才会过期，"
            "期间重新启动会先显示“等待旧会话过期”",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=ERROR_BAR_MS,
        )

    # --- lifecycle --------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        if self._shown_once:
            return
        self._shown_once = True
        if self._prompt_login and not self._controller.is_logged_in:
            QTimer.singleShot(0, self.prompt_login)

    def prompt_login(self) -> None:
        LoginDialog.ask(self._controller, self)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        if self._shutting_down:
            event.accept()
            return
        running = self._controller.running_ids()
        if not running:
            event.accept()
            return
        box = MessageBox("退出", exit_question(len(running)), self)
        box.yesButton.setText("停止并退出")
        box.cancelButton.setText("取消")
        if not box.exec():
            event.ignore()
            return
        self._shutting_down = True
        tip = StateToolTip("正在停止 runner", "最多等待 90 秒（执行中的 job 会被取消）", self)
        tip.move(tip.getSuitablePos())
        tip.show()
        QApplication.processEvents()
        try:
            leftovers = self._controller.shutdown()
        except Exception:  # never block the exit on a bug in shutdown
            log.exception("shutdown failed")
            leftovers = []
        if leftovers:
            log.warning("runners still alive after shutdown: %s", ", ".join(leftovers))
        tip.setState(True)
        event.accept()
