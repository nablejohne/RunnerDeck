"""Settings page: account, runner defaults, startup & tray, theme, about.

docs/m2-design.md §5 and docs/m3-design.md §5. Every change is saved immediately through
controller.save_settings (launch-on-login through controller.set_launch_on_login, which also
writes the registry); the page re-reads controller.config on config_changed. Cards never use
qfluentwidgets' qconfig: the values live in AppSettings.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from PyQt5.QtCore import Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QFileDialog, QWidget
from qfluentwidgets import (
    ComboBox,
    ExpandLayout,
    HyperlinkCard,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBox,
    PrimaryPushSettingCard,
    PushButton,
    PushSettingCard,
    ScrollArea,
    SettingCard,
    SettingCardGroup,
    SwitchSettingCard,
    TitleLabel,
)
from qfluentwidgets import FluentIcon as FIF

from runnerdeck import __version__
from runnerdeck.core import paths, pip_mirror
from runnerdeck.core.account import TOKEN_KIND_LABELS, format_expiry
from runnerdeck.core.models import (
    AppSettings,
    AutostartState,
    MirrorSpeed,
    ProxyApplyResult,
    UpdateInfo,
)
from runnerdeck.ui.components.login_dialog import LoginDialog
from runnerdeck.ui.components.mirror_speed_dialog import MirrorSpeedDialog, format_speed
from runnerdeck.ui.theme import THEME_LABELS

if TYPE_CHECKING:
    from runnerdeck.app.controller import RunnerController

REPO_URL = "https://github.com/nablejohne/RunnerDeck"
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
NOT_LOGGED_IN = "未登录"
PROXY_FORMAT_ERROR = "代理格式不对：应为 http://host:port、https://host:port 或 socks5://host:port"
POLL_INTERVALS: tuple[int, ...] = (15, 30, 60, 120, 300)
NOTIFY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("all", "全部"),
    ("failed", "仅失败"),
    ("off", "关闭"),
)
PIP_MIRROR_TEXT = (
    "CI 里 pip 下载依赖用的源，写入每个 runner 的 .env（PIP_INDEX_URL）；不设置则用本机的 pip 配置"
)
PIP_TEST_TEXT = "测速"
HARDEN_ROOT_TEXT = "只允许管理员、SYSTEM 和你修改根目录；其他用户只能看到目录名"
HARDEN_ROOT_CONFIRM = (
    "将改写这个目录的权限：管理员、SYSTEM 和你完全控制（子目录继承），其他用户只能列出目录名，"
    "不能在里面新建或修改文件。已有的 runner 目录各有自己的权限，不受影响。"
)
UPDATE_IDLE_TEXT = "到 GitHub 查看是否有新版本"
LAUNCH_TEXT = "登录 Windows 后自动在托盘启动 RunnerDeck（并拉起设为随 RunnerDeck 启动的 runner）"
LAUNCH_ON_TEXT = "已开启：登录 Windows 后自动在托盘启动 RunnerDeck"
LAUNCH_DISABLED_TEXT = "已在任务管理器中禁用，请在任务管理器的‘启动’页启用"
LAUNCH_MISMATCH_TEXT = "已开启，但启动项指向别的位置（程序被移动过？）；关掉再打开即可更新"
LAUNCH_UNSUPPORTED_TEXT = "只支持 Windows"


def poll_options(current: int) -> list[tuple[int, str]]:
    """Poll interval choices; a hand-edited value outside the list is kept as an extra one."""
    values = sorted({*POLL_INTERVALS, current})
    return [(value, f"{value} 秒") for value in values]


def launch_text(state: AutostartState) -> str:
    if not state.supported:
        return LAUNCH_UNSUPPORTED_TEXT
    if not state.registered:
        return LAUNCH_TEXT
    if state.disabled_by_user:
        return LAUNCH_DISABLED_TEXT
    if not state.matches:
        return LAUNCH_MISMATCH_TEXT
    return LAUNCH_ON_TEXT


def _spaced(what: str) -> str:
    """A space before a term that starts with Latin text, after Chinese (写入 pip 镜像源失败)."""
    return f" {what}" if what[:1].isascii() else what


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


class SwitchCard(SwitchSettingCard):
    """SwitchSettingCard without qconfig, labelled 开 / 关 (no Qt translator is installed)."""

    def __init__(
        self, icon: FIF, title: str, content: str | None, parent: QWidget | None = None
    ) -> None:
        super().__init__(icon, title, content, parent=parent)
        self.setValue(False)

    def setValue(self, isChecked: bool) -> None:  # noqa: N802, N803 (qfluentwidgets API)
        super().setValue(isChecked)
        self.switchButton.setText("开" if isChecked else "关")


class ComboSettingCard(SettingCard):
    """SettingCard with a ComboBox on the right (no qconfig); emits `selected(value)`.
    With `button_text`, a PushButton (`button`) follows the ComboBox."""

    selected = pyqtSignal(object)

    def __init__(
        self,
        icon: FIF,
        title: str,
        content: str | None,
        options: Sequence[tuple[object, str]],
        parent: QWidget | None = None,
        button_text: str | None = None,
    ) -> None:
        super().__init__(icon, title, content, parent)
        self.comboBox = ComboBox(self)
        self.comboBox.setMinimumWidth(160)
        self.hBoxLayout.addWidget(self.comboBox, 0, Qt.AlignRight)
        self.button: PushButton | None = None
        if button_text is not None:
            self.button = PushButton(button_text, self)
            self.hBoxLayout.addSpacing(8)
            self.hBoxLayout.addWidget(self.button, 0, Qt.AlignRight)
        self.hBoxLayout.addSpacing(16)
        self._syncing = False
        self.set_options(options)
        self.comboBox.currentIndexChanged.connect(self._on_index_changed)

    def set_options(self, options: Sequence[tuple[object, str]]) -> None:
        current = self.value()
        self._syncing = True
        try:
            self.comboBox.clear()
            for value, text in options:
                self.comboBox.addItem(text, userData=value)
        finally:
            self._syncing = False
        if current is not None:
            self.set_value(current)

    def set_value(self, value: object) -> None:
        index = self.comboBox.findData(value)
        if index < 0:
            return
        self._syncing = True
        try:
            self.comboBox.setCurrentIndex(index)
        finally:
            self._syncing = False

    def value(self) -> object:
        return self.comboBox.currentData()

    def select(self, value: object) -> None:
        """Pick `value` as if the user chose it (emits `selected` when it changes)."""
        index = self.comboBox.findData(value)
        if index >= 0:
            self.comboBox.setCurrentIndex(index)

    def _on_index_changed(self, index: int) -> None:
        if not self._syncing and index >= 0:
            self.selected.emit(self.comboBox.itemData(index))


class SettingsPage(ScrollArea):
    """objectName "settings"."""

    def __init__(self, controller: RunnerController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settings")
        self._controller = controller
        self._account_loaded = False
        self._autostart: AutostartState | None = None
        self._syncing = False  # refresh() sets widgets; their change signals must not save

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
        self.hardenRootCard = PushSettingCard(
            "加固权限",
            FIF.CERTIFICATE,
            "根目录权限",
            HARDEN_ROOT_TEXT,
            self.runnerGroup,
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
            "访问 GitHub、下载 runner 时使用，并写入每个 runner 的 .env；留空则不设置",
            "http://127.0.0.1:7890",
            self.runnerGroup,
        )
        self.pipMirrorCard = ComboSettingCard(
            FIF.DOWNLOAD,
            "pip 镜像源",
            PIP_MIRROR_TEXT,
            pip_mirror.options(controller.config.settings.pip_index_url),
            self.runnerGroup,
            button_text=PIP_TEST_TEXT,
        )
        self.pollCard = ComboSettingCard(
            FIF.SYNC,
            "轮询间隔",
            "多久从 GitHub 拉取一次在线状态（本机进程状态是实时的）",
            poll_options(controller.config.settings.poll_interval_s),
            self.runnerGroup,
        )
        self.runnerGroup.addSettingCard(self.rootCard)
        self.runnerGroup.addSettingCard(self.hardenRootCard)
        self.runnerGroup.addSettingCard(self.labelsCard)
        self.runnerGroup.addSettingCard(self.proxyCard)
        self.runnerGroup.addSettingCard(self.pipMirrorCard)
        self.runnerGroup.addSettingCard(self.pollCard)

        # startup & tray
        self.startupGroup = SettingCardGroup("启动与托盘", self.scrollWidget)
        self.launchCard = SwitchCard(FIF.POWER_BUTTON, "开机自启", LAUNCH_TEXT, self.startupGroup)
        self.startMinimizedCard = SwitchCard(
            FIF.MINIMIZE,
            "启动时最小化到托盘",
            "手动启动时也不显示窗口，只在托盘（开机自启时总是只在托盘）",
            self.startupGroup,
        )
        self.closeToTrayCard = SwitchCard(
            FIF.HIDE,
            "关闭窗口时最小化到托盘",
            "关闭窗口后 runner 继续运行；要退出请用托盘菜单的“退出”",
            self.startupGroup,
        )
        self.notifyCard = ComboSettingCard(
            FIF.RINGER,
            "job 通知",
            "job 结束时在托盘弹出通知",
            NOTIFY_OPTIONS,
            self.startupGroup,
        )
        for card in (
            self.launchCard,
            self.startMinimizedCard,
            self.closeToTrayCard,
            self.notifyCard,
        ):
            self.startupGroup.addSettingCard(card)

        # personalization
        self.personalGroup = SettingCardGroup("个性化", self.scrollWidget)
        self.themeCard = ComboSettingCard(
            FIF.BRUSH,
            "主题",
            "界面的浅色 / 深色外观",
            list(THEME_LABELS.items()),
            self.personalGroup,
        )
        self.personalGroup.addSettingCard(self.themeCard)

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
        self.updateCard = PushSettingCard(
            "检查更新", FIF.UPDATE, "检查更新", UPDATE_IDLE_TEXT, self.aboutGroup
        )
        self.checkUpdatesCard = SwitchCard(
            FIF.SYNC,
            "启动时检查更新",
            "RunnerDeck 启动约 30 秒后查一次 GitHub 上的最新版本（只提示，不自动安装）",
            self.aboutGroup,
        )
        self._update_url: str | None = None
        self.aboutGroup.addSettingCard(self.versionCard)
        self.aboutGroup.addSettingCard(self.updateCard)
        self.aboutGroup.addSettingCard(self.checkUpdatesCard)
        self.aboutGroup.addSettingCard(self.repoCard)
        self.aboutGroup.addSettingCard(self.logCard)

        self._build_layout()

        self.accountCard.clicked.connect(self.change_pat)
        self.logoutCard.clicked.connect(self.ask_logout)
        self.rootCard.clicked.connect(self.choose_root)
        self.hardenRootCard.clicked.connect(self.ask_harden_root)
        self.labelsCard.committed.connect(self._on_labels_committed)
        self.proxyCard.committed.connect(self._on_proxy_committed)
        self.pipMirrorCard.selected.connect(lambda value: self._save(pip_index_url=value))
        self.pip_test_button.clicked.connect(self.test_pip_mirrors)
        self.pollCard.selected.connect(lambda value: self._save(poll_interval_s=value))
        self.launchCard.checkedChanged.connect(self._on_launch_switched)
        self.startMinimizedCard.checkedChanged.connect(
            lambda checked: self._save_switch(start_minimized=checked)
        )
        self.closeToTrayCard.checkedChanged.connect(
            lambda checked: self._save_switch(close_to_tray=checked)
        )
        self.notifyCard.selected.connect(lambda value: self._save(notify_jobs=value))
        self.themeCard.selected.connect(lambda value: self._save(theme=value))
        self.logCard.clicked.connect(self.open_log_dir)
        self.updateCard.clicked.connect(self.check_or_download_update)
        self.checkUpdatesCard.checkedChanged.connect(
            lambda checked: self._save_switch(check_updates=checked)
        )
        controller.update_checked.connect(self._on_update_checked)

        controller.config_changed.connect(self.refresh)
        controller.account_loaded.connect(self._on_account_loaded)
        controller.logged_in.connect(self._on_logged_in)
        controller.logged_out.connect(self._on_logged_out)
        controller.autostart_state_loaded.connect(self._on_autostart_state_loaded)
        controller.proxy_applied.connect(self._on_proxy_applied)
        controller.pip_index_applied.connect(self._on_pip_index_applied)
        controller.pip_mirrors_tested.connect(self._on_pip_mirrors_tested)
        controller.runners_root_hardened.connect(self._on_root_hardened)
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
        for group in (
            self.accountGroup,
            self.runnerGroup,
            self.startupGroup,
            self.personalGroup,
            self.aboutGroup,
        ):
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
        self.pipMirrorCard.set_options(pip_mirror.options(settings.pip_index_url))
        self.pipMirrorCard.set_value(pip_mirror.option_value(settings.pip_index_url))
        self.pollCard.set_options(poll_options(settings.poll_interval_s))
        self.pollCard.set_value(settings.poll_interval_s)
        self.notifyCard.set_value(settings.notify_jobs)
        self.themeCard.set_value(settings.theme)
        self._syncing = True
        try:
            self.startMinimizedCard.setChecked(settings.start_minimized)
            self.closeToTrayCard.setChecked(settings.close_to_tray)
            self.checkUpdatesCard.setChecked(settings.check_updates)
        finally:
            self._syncing = False
        self._refresh_launch_card()
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
        if self._controller.autostart_supported:
            self._controller.load_autostart_state()  # the user may have changed it elsewhere

    def _save(self, **changes: object) -> None:
        if any(getattr(self.settings, key) != value for key, value in changes.items()):
            self._controller.save_settings(self.settings.model_copy(update=changes))

    def _save_switch(self, **changes: object) -> None:
        if not self._syncing:
            self._save(**changes)

    # --- launch on login ----------------------------------------------------------------

    def _refresh_launch_card(self) -> None:
        supported = bool(self._controller.autostart_supported)
        state = self._autostart
        if not supported or (state is not None and not state.supported):
            checked, text, enabled = False, LAUNCH_UNSUPPORTED_TEXT, False
        elif state is None:
            checked, text, enabled = self.settings.launch_on_login, LAUNCH_TEXT, True
        else:
            checked, text, enabled = state.registered, launch_text(state), True
        self._syncing = True
        try:
            self.launchCard.setChecked(checked)
        finally:
            self._syncing = False
        self.launchCard.setContent(text)
        self.launchCard.setEnabled(enabled)

    def _on_autostart_state_loaded(self, state: object) -> None:
        if isinstance(state, AutostartState):
            self._autostart = state
            self._refresh_launch_card()

    def _on_launch_switched(self, checked: bool) -> None:
        if not self._syncing:
            self._controller.set_launch_on_login(checked)

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

    def ask_harden_root(self) -> None:
        box = MessageBox(
            "加固根目录权限",
            f"{self.settings.runners_root}\n\n{HARDEN_ROOT_CONFIRM}",
            self.window(),
        )
        box.yesButton.setText("加固")
        box.cancelButton.setText("取消")
        if box.exec():
            self._controller.harden_runners_root()

    def _on_root_hardened(self, root: str) -> None:
        InfoBar.success(
            "已加固", f"{root} 现在只有管理员、SYSTEM 和你可以修改", parent=self,
            position=InfoBarPosition.TOP, duration=5000,
        )  # fmt: skip

    def _on_proxy_applied(self, result: ProxyApplyResult) -> None:
        self._show_env_applied(result, "代理")

    def _on_pip_index_applied(self, result: ProxyApplyResult) -> None:
        self._show_env_applied(result, "pip 镜像源")

    def _show_env_applied(self, result: ProxyApplyResult, what: str) -> None:
        if result.updated:
            InfoBar.success(
                f"{what}已写入 runner",
                f"已更新 {len(result.updated)} 个 runner 的 .env；正在运行的 runner 重启后生效",
                parent=self, position=InfoBarPosition.TOP, duration=6000,
            )  # fmt: skip
        if result.custom:
            InfoBar.warning(
                f"保留了自定义{_spaced(what)}",
                f"{'、'.join(result.custom)} 的 .env 里有你自己写的{_spaced(what)}，没有改动",
                parent=self, position=InfoBarPosition.TOP, duration=8000,
            )  # fmt: skip
        if result.failed:
            InfoBar.error(
                f"写入{_spaced(what)}失败",
                "；".join(f"{name}：{reason}" for name, reason in result.failed),
                parent=self, position=InfoBarPosition.TOP, duration=10000,
            )  # fmt: skip

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
        elif op == "launch_on_login":
            self._refresh_launch_card()  # back to the last known state
            if self._controller.autostart_supported:
                self._controller.load_autostart_state()
        elif op == "account":
            self.accountCard.setContent("读取 token 信息失败，详情见上方提示")
        elif op == "pip_mirror_test":
            self._reset_pip_test_button()
            self.pipMirrorCard.setContent("测速失败，详情见上方提示")

    # --- pip mirror ------------------------------------------------------------------------

    @property
    def pip_test_button(self) -> PushButton:
        assert self.pipMirrorCard.button is not None
        return self.pipMirrorCard.button

    def test_pip_mirrors(self) -> None:
        self.pip_test_button.setEnabled(False)
        self.pip_test_button.setText("测速中…")
        self.pipMirrorCard.setContent(
            f"正在逐个测速 {len(pip_mirror.candidates(self.settings.pip_index_url))} 个镜像源…"
        )
        self._controller.test_pip_mirrors()

    def _reset_pip_test_button(self) -> None:
        self.pip_test_button.setEnabled(True)
        self.pip_test_button.setText(PIP_TEST_TEXT)

    def _on_pip_mirrors_tested(self, results: list[MirrorSpeed]) -> None:
        self._reset_pip_test_button()
        ranked = pip_mirror.ranked(results)
        if ranked and ranked[0].mb_per_s is not None:
            self.pipMirrorCard.setContent(
                f"上次测速：{ranked[0].label} 最快（{format_speed(ranked[0])}）"
            )
        else:
            self.pipMirrorCard.setContent("上次测速：所有镜像源都失败了")
        settings = self.settings
        dialog = MirrorSpeedDialog(results, settings.pip_index_url, settings.proxy, self.window())
        if dialog.exec() and dialog.best_url is not None:
            self._save(pip_index_url=dialog.best_url)

    # --- about -------------------------------------------------------------------------

    def check_or_download_update(self) -> None:
        if self._update_url is not None:
            QDesktopServices.openUrl(QUrl(self._update_url))
            return
        self.updateCard.setContent("正在检查…")
        self.updateCard.button.setEnabled(False)
        self._controller.check_updates()

    def _on_update_checked(self, info: UpdateInfo) -> None:
        self.updateCard.button.setEnabled(True)
        if info.error:
            self.updateCard.setContent(f"检查失败：{info.error}")
        elif info.newer and info.latest:
            self._update_url = info.url
            self.updateCard.setContent(
                f"有新版本 {info.latest}（当前 {info.current}），点“下载”打开发布页"
            )
            self.updateCard.button.setText("下载")
        elif info.latest:
            self.updateCard.setContent(f"已是最新版本（{info.current}）")
        else:
            self.updateCard.setContent("GitHub 上还没有正式版本")

    def open_log_dir(self) -> None:
        log_dir = paths.log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir)))
