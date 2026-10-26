"""Main window: navigation, feedback, login prompt, tray, close-to-tray and graceful quit.

Construction is cheap (no network, no subprocess): `python -m runnerdeck --smoke-test` and the
UI tests build the whole window offscreen. Everything slow goes through the controller.

Closing (docs/m3-design.md §5): with a tray and settings.close_to_tray the window only hides;
otherwise, and from the tray's "退出", `quit_app()` confirms, stops every runner and quits.
A Windows logoff / shutdown (`isSavingSession()`) never asks: `on_session_end()` signals every
runner to stop without waiting and the close is accepted.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QCloseEvent, QIcon, QShowEvent
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QWidget
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    FluentWindow,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    NavigationItemPosition,
    StateToolTip,
)

from runnerdeck.core.models import LocalState
from runnerdeck.ui.components.login_dialog import LoginDialog
from runnerdeck.ui.pages.add_wizard_page import AddWizardPage
from runnerdeck.ui.pages.dashboard_page import DashboardPage
from runnerdeck.ui.pages.runner_detail_page import TAB_JOB, RunnerDetailPage
from runnerdeck.ui.pages.settings_page import SettingsPage
from runnerdeck.ui.theme import apply_theme
from runnerdeck.ui.tray import TrayIcon

if TYPE_CHECKING:
    from runnerdeck.app.controller import RunnerController

log = logging.getLogger(__name__)

ICON_PATH = Path(__file__).resolve().parent / "resources" / "runnerdeck.png"
WINDOW_TITLE = "RunnerDeck"
# controller.failed(op, message): op -> InfoBar / notification title.
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
        "open_dir": "打开目录失败",
        "import": "导入失败",
        "forget": "取消登记失败",
        "discover": "扫描失败",
        "kill": "结束进程失败",
        "take_over": "接管失败",
        "diag": "读取日志失败",
        "autostart": "自动启动",
        "launch_on_login": "自启设置失败",
        "auto_start": "保存失败",
    }
)
DEFAULT_FAILED_TITLE = "操作失败"
ERROR_BAR_MS = 10000
LOGIN_RETRY_MS = 500
BACKGROUND_TITLE = "RunnerDeck 仍在后台运行"
BACKGROUND_TEXT = "关闭窗口只会隐藏到托盘，runner 继续运行；要退出请在托盘菜单里选“退出”"
FORCED_STOP_TEXT = (
    "runner 没有响应优雅停止，已被强制结束；GitHub 端旧会话约 3 分钟后才会过期，"
    "期间重新启动会先显示“等待旧会话过期”"
)


def exit_question(count: int, jobs: Sequence[str] = ()) -> str:
    """Quit confirmation; jobs = "名字（job）" of runners executing a job right now."""
    text = f"有 {count} 个 runner 正在运行，退出会先停止它们（执行中的 job 会被取消）。"
    if jobs:
        text += f"\n正在执行 job：{'、'.join(jobs)}。"
    return text + "\n要退出吗？"


class MainWindow(FluentWindow):
    """FluentWindow with 概览 / 添加 runner / 设置 and a navigation-less detail page."""

    def __init__(
        self,
        controller: RunnerController,
        parent: QWidget | None = None,
        *,
        prompt_login: bool = True,
        tray: bool | None = None,
    ) -> None:
        """prompt_login=False skips the modal login dialog on first show (`--smoke-test`).

        tray=None creates the tray icon when the platform has a system tray; True / False force
        it (tests and `--smoke-test` pass True: the offscreen platform reports no tray).
        """
        super().__init__(parent)
        self._controller = controller
        self._shown_once = False
        self._quitting = False
        self._background_hint_shown = False
        self._prompt_login = prompt_login
        self._theme = controller.config.settings.theme

        self.dashboardPage = DashboardPage(controller, self)
        self.addPage = AddWizardPage(controller, self)
        self.settingsPage = SettingsPage(controller, self)
        self.detailPage = RunnerDetailPage(controller, self)
        self.addSubInterface(self.dashboardPage, FIF.HOME, "概览")
        self.addSubInterface(self.addPage, FIF.ADD, "添加 runner")
        self.addSubInterface(
            self.settingsPage, FIF.SETTING, "设置", position=NavigationItemPosition.BOTTOM
        )
        self.stackedWidget.addWidget(self.detailPage)  # no navigation item

        self.setWindowTitle(WINDOW_TITLE)
        if ICON_PATH.is_file():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(960, 640)
        self.navigationInterface.setExpandWidth(200)

        if tray is None:
            tray = QSystemTrayIcon.isSystemTrayAvailable()
        self.tray: TrayIcon | None = TrayIcon(controller, self, self) if tray else None
        if self.tray is not None:
            self.tray.show()

        self.addPage.finished.connect(lambda: self.switchTo(self.dashboardPage))
        self.dashboardPage.detail_requested.connect(self.open_detail)
        self.dashboardPage.job_requested.connect(self.open_job)
        self.detailPage.back_requested.connect(lambda: self.switchTo(self.dashboardPage))
        controller.failed.connect(self._on_failed)
        controller.logged_in.connect(self._on_logged_in)
        controller.runner_removed.connect(self._on_runner_removed)
        controller.runner_stopped.connect(self._on_runner_stopped)
        controller.runner_imported.connect(self._on_runner_imported)
        controller.runner_forgotten.connect(self._on_runner_forgotten)
        controller.external_stopped.connect(self._on_external_stopped)
        controller.config_changed.connect(self._on_config_changed)

    # --- showing --------------------------------------------------------------------------

    @property
    def in_background(self) -> bool:
        """Hidden to the tray or minimized: feedback goes to tray notifications."""
        return not self.isVisible() or self.isMinimized()

    def show_and_raise(self) -> None:
        """Bring the window back from the tray / taskbar (keeps it maximized if it was)."""
        if self.isMaximized():
            self.showMaximized()
        else:
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def open_job(self, record_id: str) -> None:
        self.open_detail(record_id)
        self.detailPage.switch_tab(TAB_JOB)

    def open_detail(self, record_id: str) -> None:
        self.detailPage.show_record(record_id)
        self.switchTo(self.detailPage)
        # The detail page has no navigation item: keep 概览 highlighted as its parent.
        self.navigationInterface.setCurrentItem(self.dashboardPage.objectName())

    # --- feedback ------------------------------------------------------------------------

    def _bar(self, level: str, title: str, content: str, duration: int = ERROR_BAR_MS) -> None:
        """InfoBar while visible; errors / warnings become tray notifications in background."""
        if self.tray is not None and self.in_background and level in ("error", "warning"):
            icon = QSystemTrayIcon.Critical if level == "error" else QSystemTrayIcon.Warning
            self.tray.notify(title, content, icon)
            return
        show = getattr(InfoBar, level)
        show(title, content, parent=self, position=InfoBarPosition.TOP, duration=duration)

    def _on_failed(self, op: str, message: str, _exc: object = None) -> None:
        self._bar("error", FAILED_TITLES.get(op, DEFAULT_FAILED_TITLE), message)

    def _on_logged_in(self, user: object) -> None:
        self._bar("success", "已登录", f"GitHub 账号：{getattr(user, 'login', '')}", 2000)

    def _on_runner_removed(self, _record_id: str, result: object) -> None:
        record = getattr(result, "record", None)
        name = getattr(record, "name", "")
        warnings = list(getattr(result, "warnings", ()))
        if warnings:
            self._bar("warning", f"已移除 runner {name}", "\n".join(warnings))
        else:
            self._bar("success", "已移除", f"runner {name} 已注销并删除", 2000)

    def _on_runner_stopped(self, _record_id: str, result: object) -> None:
        if not getattr(result, "graceful", True):
            self._bar("warning", "已强制停止", FORCED_STOP_TEXT)

    def _on_runner_imported(self, record: object) -> None:
        self._bar("success", "已导入", f"runner {getattr(record, 'name', '')} 已登记", 3000)

    def _on_runner_forgotten(self, _record_id: str) -> None:
        self._bar("success", "已取消登记", "目录和 GitHub 上的注册都原样保留", 3000)

    def _on_external_stopped(self, _key: str, result: object) -> None:
        survivors = list(getattr(result, "survivors", ()))
        if survivors:
            pids = "、".join(str(pid) for pid in survivors)
            self._bar("warning", "有进程没有结束", f"这些进程仍在运行：{pids}")
        else:
            killed = len(getattr(result, "killed", ()))
            self._bar(
                "success",
                "已结束进程",
                f"结束了 {killed} 个进程；GitHub 端旧会话约 3 分钟后过期",
                4000,
            )

    def _on_config_changed(self, _config: object = None) -> None:
        theme = self._controller.config.settings.theme
        if theme != self._theme:
            self._theme = theme
            apply_theme(theme)

    # --- lifecycle --------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        if self._shown_once:
            return
        self._shown_once = True
        if self._prompt_login and not self._controller.is_logged_in:
            QTimer.singleShot(0, self.prompt_login)

    def prompt_login(self) -> None:
        if QApplication.activeModalWidget() is not None:
            # e.g. the first show came from quit_app()'s or the tray's confirmation: never
            # stack the login mask on top of it; ask once that dialog is gone.
            QTimer.singleShot(LOGIN_RETRY_MS, self.prompt_login)
            return
        self.show_and_raise()
        LoginDialog.ask(self._controller, self)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        app = QApplication.instance()
        saving = isinstance(app, QApplication) and app.isSavingSession()
        if self._quitting or saving:
            event.accept()
            return
        if self.tray is not None and self._controller.config.settings.close_to_tray:
            event.ignore()
            self.hide()
            if not self._background_hint_shown:
                self._background_hint_shown = True
                self.tray.notify(BACKGROUND_TITLE, BACKGROUND_TEXT)
            return
        if self.quit_app():
            event.accept()
        else:
            event.ignore()

    def _busy_jobs(self, running: Sequence[str]) -> list[str]:
        names = {r.id: r.name for r in self._controller.records}
        jobs: list[str] = []
        for rid in running:
            state, job = self._controller.local_state(rid)
            if state is LocalState.BUSY:
                name = names.get(rid, rid)
                jobs.append(f"{name}（{job}）" if job else name)
        return jobs

    def quit_app(self) -> bool:
        """Confirm (when runners run), stop them all and quit; False when the user cancelled."""
        if self._quitting:
            return True
        running = self._controller.running_ids()
        if running:
            self.show_and_raise()
            box = MessageBox("退出", exit_question(len(running), self._busy_jobs(running)), self)
            box.yesButton.setText("停止并退出")
            box.cancelButton.setText("取消")
            if not box.exec():
                return False
        self._quitting = True
        if running:
            self._shutdown_runners()
        if self.tray is not None:
            self.tray.hide()
        QApplication.quit()
        return True

    def _shutdown_runners(self) -> None:
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

    def on_session_end(self, _manager: object = None) -> None:
        """Windows logoff / shutdown (QApplication.commitDataRequest, DirectConnection).

        Never blocks or asks: every running runner gets its stop signal and the session goes on.
        """
        try:
            count = self._controller.emergency_stop_all()
        except Exception:  # the session must end regardless
            log.exception("emergency stop failed")
            return
        log.info("session ending: signalled %d runner(s) to stop", count)

    def on_session_resumed(self, count: int) -> None:
        """The logoff was cancelled after on_session_end(): `count` runners restart."""
        if count and self.tray is not None:
            self.tray.notify("注销已取消", f"正在重新启动 {count} 个 runner")
