"""Settings page: account, runner defaults, about (docs/m2-design.md §5).

Every change is saved immediately through controller.save_settings; the page re-reads
controller.config on config_changed. Theme / tray / autostart / polling come in M3.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from PyQt5.QtCore import Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QFileDialog, QWidget
from qfluentwidgets import (
    ExpandLayout,
    HyperlinkCard,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBox,
    PrimaryPushSettingCard,
    PushSettingCard,
    ScrollArea,
    SettingCard,
    SettingCardGroup,
    TitleLabel,
)
from qfluentwidgets import FluentIcon as FIF

from runnerdeck import __version__
from runnerdeck.core import paths
from runnerdeck.core.account import TOKEN_KIND_LABELS, format_expiry
from runnerdeck.core.models import AppSettings
from runnerdeck.ui.components.login_dialog import LoginDialog

if TYPE_CHECKING:
    from runnerdeck.app.controller import RunnerController

# TODO(settings): point this at the published repository once it exists.
REPO_URL = "https://github.com/nablejohne/RunnerDeck"
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
NOT_LOGGED_IN = "未登录"
PROXY_FORMAT_ERROR = "代理格式不对：应为 http://host:port、https://host:port 或 socks5://host:port"


def validate_proxy(text: str) -> str | None:
    """Return the normalized proxy URL (None when empty) or raise ValueError."""
    value = text.strip()
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme.lower() not in PROXY_SCHEMES or not parts.hostname:
        raise ValueError(PROXY_FORMAT_ERROR)
    return value


class LineEditSettingCard(SettingCard):
    """SettingCard with a LineEdit on the right; commits on Enter or focus loss."""

    committed = pyqtSignal(str)

    def __init__(
        self,
        icon: FIF,
        title: str,
        content: str | None = None,
        placeholder: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(icon, title, content, parent)
        self.lineEdit = LineEdit(self)
        self.lineEdit.setPlaceholderText(placeholder)
        self.lineEdit.setClearButtonEnabled(True)
        self.lineEdit.setMinimumWidth(320)
        self.hBoxLayout.addWidget(self.lineEdit, 0, Qt.AlignRight)
        self.hBoxLayout.addSpacing(16)
        self._last = ""
        self.lineEdit.editingFinished.connect(self._commit)

    def set_value(self, value: str) -> None:
        self._last = value
        self.lineEdit.setText(value)

    def _commit(self) -> None:
        value = self.lineEdit.text().strip()
        if value == self._last:
            return
        self._last = value
        self.committed.emit(value)


class SettingsPage(ScrollArea):
    """objectName "settings"."""

    def __init__(self, controller: RunnerController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settings")
        self._controller = controller
        self._account_loaded = False

        self.scrollWidget = QWidget(self)
        self.expandLayout = ExpandLayout(self.scrollWidget)
        self.titleLabel = TitleLabel("设置", self)

        # account
        self.accountGroup = SettingCardGroup("账号", self.scrollWidget)
        self.accountCard = PrimaryPushSettingCard(
            "更换 PAT", FIF.PEOPLE, NOT_LOGGED_IN, "", self.accountGroup
        )
        self.logoutCard = PushSettingCard(
            "退出登录", FIF.POWER_BUTTON, "退出登录", "删除本机保存的 PAT", self.accountGroup
        )
        self.accountGroup.addSettingCard(self.accountCard)
        self.accountGroup.addSettingCard(self.logoutCard)

        # runner
        self.runnerGroup = SettingCardGroup("runner", self.scrollWidget)
        self.rootCard = PushSettingCard(
            "选择目录", FIF.FOLDER, "runner 根目录", "", self.runnerGroup
        )
        self.labelsCard = LineEditSettingCard(
            FIF.TAG,
            "默认标签",
            "新 runner 预填的自定义标签，逗号分隔",
            "例如 gpu, build",
            self.runnerGroup,
        )
        self.proxyCard = LineEditSettingCard(
            FIF.GLOBE,
            "代理",
            "访问 GitHub 和下载 runner 时使用；留空则不设置",
            "http://127.0.0.1:7890",
            self.runnerGroup,
        )
        self.runnerGroup.addSettingCard(self.rootCard)
        self.runnerGroup.addSettingCard(self.labelsCard)
        self.runnerGroup.addSettingCard(self.proxyCard)

        # about
        self.aboutGroup = SettingCardGroup("关于", self.scrollWidget)
        self.versionCard = SettingCard(
            FIF.INFO, "版本", f"RunnerDeck {__version__}", self.aboutGroup
        )
        self.repoCard = HyperlinkCard(
            REPO_URL, "打开", FIF.GITHUB, "项目仓库", REPO_URL, self.aboutGroup
        )
        self.logCard = PushSettingCard(
            "打开", FIF.DOCUMENT, "日志目录", str(paths.log_dir()), self.aboutGroup
        )
        self.aboutGroup.addSettingCard(self.versionCard)
        self.aboutGroup.addSettingCard(self.repoCard)
        self.aboutGroup.addSettingCard(self.logCard)

        self._build_layout()

        self.accountCard.clicked.connect(self.change_pat)
        self.logoutCard.clicked.connect(self.ask_logout)
        self.rootCard.clicked.connect(self.choose_root)
        self.labelsCard.committed.connect(self._on_labels_committed)
        self.proxyCard.committed.connect(self._on_proxy_committed)
        self.logCard.clicked.connect(self.open_log_dir)

        controller.config_changed.connect(self.refresh)
        controller.account_loaded.connect(self._on_account_loaded)
        controller.logged_in.connect(self._on_logged_in)
        controller.logged_out.connect(self._on_logged_out)
        controller.failed.connect(self._on_failed)

        self.refresh()

    def _build_layout(self) -> None:
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setViewportMargins(0, 80, 0, 20)
        self.setStyleSheet("QScrollArea{background:transparent;border:none}")
        self.scrollWidget.setStyleSheet("QWidget{background:transparent}")
        self.titleLabel.move(36, 30)
        self.expandLayout.setSpacing(28)
        self.expandLayout.setContentsMargins(36, 10, 36, 0)
        for group in (self.accountGroup, self.runnerGroup, self.aboutGroup):
            self.expandLayout.addWidget(group)

    # --- data -------------------------------------------------------------------------

    @property
    def settings(self) -> AppSettings:
        return self._controller.config.settings

    def refresh(self, _config: object = None) -> None:
        settings = self.settings
        self.rootCard.setContent(settings.runners_root)
        self.labelsCard.set_value(", ".join(settings.default_labels))
        self.proxyCard.set_value(settings.proxy or "")
        if not self._controller.is_logged_in:
            self.accountCard.setTitle(NOT_LOGGED_IN)
            self.accountCard.setContent("点击“更换 PAT”登录")
            self.logoutCard.setEnabled(False)
        else:
            self.logoutCard.setEnabled(True)
            if not self._account_loaded:
                self.accountCard.setTitle(self._controller.config.account or "已登录")
                self.accountCard.setContent("正在读取 token 信息…")

    def showEvent(self, event: object) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)  # type: ignore[arg-type]
        if self._controller.is_logged_in and not self._account_loaded:
            self._controller.load_account()

    def _save(self, **changes: object) -> None:
        self._controller.save_settings(self.settings.model_copy(update=changes))

    # --- account ---------------------------------------------------------------------

    def _on_account_loaded(self, user: object, source: str) -> None:
        self._account_loaded = True
        kind = TOKEN_KIND_LABELS.get(getattr(user, "token_kind", "unknown"), "未知")
        expiry, _attention = format_expiry(
            getattr(user, "token_expires_at", None), datetime.now(UTC)
        )
        self.accountCard.setTitle(str(getattr(user, "login", "")))
        self.accountCard.setContent(f"{kind} · 过期：{expiry} · 来源：{source}")
        self.logoutCard.setEnabled(True)

    def _on_logged_in(self, _user: object) -> None:
        self._account_loaded = False
        self.refresh()
        self._controller.load_account()

    def _on_logged_out(self) -> None:
        self._account_loaded = False
        self.refresh()

    def change_pat(self) -> None:
        LoginDialog.ask(self._controller, self.window())

    def ask_logout(self) -> None:
        box = MessageBox(
            "退出登录", "将删除本机保存的 PAT；已添加的 runner 不受影响。", self.window()
        )
        box.yesButton.setText("退出登录")
        box.cancelButton.setText("取消")
        if box.exec():
            self._controller.logout()

    # --- runner ------------------------------------------------------------------------

    def choose_root(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "选择 runner 根目录", self.settings.runners_root
        )
        if chosen:
            self.set_root(chosen)

    def set_root(self, root: str) -> None:
        if root != self.settings.runners_root:
            self._save(runners_root=root)

    def _on_labels_committed(self, text: str) -> None:
        labels = [part.strip() for part in text.split(",") if part.strip()]
        self._save(default_labels=labels)

    def _on_proxy_committed(self, text: str) -> None:
        try:
            proxy = validate_proxy(text)
        except ValueError as e:
            InfoBar.error(
                "代理无效", str(e), parent=self, position=InfoBarPosition.TOP, duration=6000
            )
            self.proxyCard.set_value(self.settings.proxy or "")
            return
        self._save(proxy=proxy)

    def _on_failed(self, op: str, _message: str, _exc: object = None) -> None:
        if op == "settings":
            self.refresh()  # the window shows the message; undo the optimistic edit
        elif op == "account":
            self.accountCard.setContent("读取 token 信息失败，详情见上方提示")

    # --- about -------------------------------------------------------------------------

    def open_log_dir(self) -> None:
        log_dir = paths.log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir)))
