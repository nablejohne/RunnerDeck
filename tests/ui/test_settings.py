"""SettingsPage: edits call save_settings (launch-on-login: set_launch_on_login); the account
and autostart cards follow controller signals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QFileDialog
from pytestqt.qtbot import QtBot
from qfluentwidgets import MessageBox

from runnerdeck.core.models import AppConfig, AppSettings, AutostartState, ProxyApplyResult
from runnerdeck.ui.components.login_dialog import LoginDialog
from runnerdeck.ui.pages.settings_page import (
    LAUNCH_DISABLED_TEXT,
    LAUNCH_MISMATCH_TEXT,
    LAUNCH_ON_TEXT,
    LAUNCH_TEXT,
    NOT_LOGGED_IN,
    PROXY_FORMAT_ERROR,
    SettingsPage,
    launch_text,
    poll_options,
    validate_proxy,
)
from ui.conftest import FakeController, make_user


@pytest.fixture
def fake() -> FakeController:
    settings = AppSettings(runners_root="/tmp/old-root", default_labels=["gpu"], proxy=None)
    return FakeController(AppConfig(account="octocat", settings=settings))


@pytest.fixture
def page(qtbot: QtBot, fake: FakeController) -> SettingsPage:
    widget = SettingsPage(fake)
    qtbot.addWidget(widget)
    return widget


def _saved(fake: FakeController) -> list[AppSettings]:
    return [args[0] for args, _ in fake.calls_of("save_settings")]


def test_shows_current_values(page: SettingsPage) -> None:
    assert page.rootCard.contentLabel.text() == "/tmp/old-root"
    assert page.labelsCard.lineEdit.text() == "gpu"
    assert page.proxyCard.lineEdit.text() == ""
    assert "RunnerDeck" in page.versionCard.contentLabel.text()


def test_choose_root_saves(
    page: SettingsPage, fake: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "/tmp/new-root")
    )
    page.rootCard.clicked.emit()
    assert [s.runners_root for s in _saved(fake)] == ["/tmp/new-root"]
    assert page.rootCard.contentLabel.text() == "/tmp/new-root"  # via config_changed

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: ""))
    page.rootCard.clicked.emit()
    assert len(_saved(fake)) == 1  # cancelled: nothing saved


def test_labels_and_proxy_commit_on_editing_finished(
    page: SettingsPage, fake: FakeController
) -> None:
    page.labelsCard.lineEdit.setText("gpu, ci ,")
    page.labelsCard.lineEdit.editingFinished.emit()
    page.labelsCard.lineEdit.editingFinished.emit()  # unchanged: no second save
    page.proxyCard.lineEdit.setText("http://127.0.0.1:7890")
    page.proxyCard.lineEdit.editingFinished.emit()
    saved = _saved(fake)
    assert [s.default_labels for s in saved] == [["gpu", "ci"], ["gpu", "ci"]]
    assert saved[-1].proxy == "http://127.0.0.1:7890"
    assert saved[-1].runners_root == "/tmp/old-root"  # other fields untouched

    page.proxyCard.lineEdit.setText("")
    page.proxyCard.lineEdit.editingFinished.emit()
    assert _saved(fake)[-1].proxy is None


def test_invalid_proxy_shows_error_and_reverts(
    page: SettingsPage, fake: FakeController, no_infobars: list[tuple[str, str, str]]
) -> None:
    page.proxyCard.lineEdit.setText("127.0.0.1:7890")
    page.proxyCard.lineEdit.editingFinished.emit()
    assert not _saved(fake)
    assert ("error", "代理无效", PROXY_FORMAT_ERROR) in no_infobars
    assert page.proxyCard.lineEdit.text() == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", None),
        ("  ", None),
        ("http://h:1", "http://h:1"),
        ("socks5://h:1080", "socks5://h:1080"),
    ],
)
def test_validate_proxy_ok(text: str, expected: str | None) -> None:
    assert validate_proxy(text) == expected


@pytest.mark.parametrize("text", ["h:1", "ftp://h", "http://", "http:///path"])
def test_validate_proxy_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        validate_proxy(text)


def test_account_card_follows_signals(page: SettingsPage, fake: FakeController) -> None:
    page.show()
    assert fake.calls_of("load_account") == [((), {})]
    expires = datetime.now(UTC) + timedelta(days=3)
    fake.account_loaded.emit(make_user("octocat", token_expires_at=expires), "系统凭据管理器")
    assert page.accountCard.titleLabel.text() == "octocat"
    content = page.accountCard.contentLabel.text()
    assert "fine-grained PAT" in content and "即将过期" in content and "系统凭据管理器" in content
    assert page.logoutCard.isEnabled()

    fake.is_logged_in = False
    fake.logged_out.emit()
    assert page.accountCard.titleLabel.text() == NOT_LOGGED_IN
    assert not page.logoutCard.isEnabled()

    fake.is_logged_in = True
    fake.logged_in.emit(make_user())
    assert len(fake.calls_of("load_account")) == 2


def test_logout_asks_first(
    page: SettingsPage, fake: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MessageBox, "exec", lambda self: False)
    page.logoutCard.clicked.emit()
    assert not fake.calls_of("logout")
    monkeypatch.setattr(MessageBox, "exec", lambda self: True)
    page.logoutCard.clicked.emit()
    assert fake.calls_of("logout") == [((), {})]


def test_change_pat_opens_login_dialog(
    page: SettingsPage, fake: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[object] = []
    monkeypatch.setattr(
        LoginDialog,
        "ask",
        staticmethod(lambda controller, parent: asked.append(controller) or True),
    )
    page.accountCard.clicked.emit()
    assert asked == [fake]


def test_open_log_dir(page: SettingsPage, monkeypatch: pytest.MonkeyPatch, isolated_home) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        QDesktopServices,
        "openUrl",
        staticmethod(lambda url: opened.append(url.toLocalFile()) or True),
    )
    page.logCard.clicked.emit()
    assert opened and Path(opened[0]) == isolated_home / "logs"


def test_settings_failure_reverts_fields(page: SettingsPage, fake: FakeController) -> None:
    page.labelsCard.lineEdit.setText("typo")
    fake.failed.emit("settings", "写入失败")
    assert page.labelsCard.lineEdit.text() == "gpu"
    fake.failed.emit("account", "401")
    assert "失败" in page.accountCard.contentLabel.text()


# --- M3: startup & tray, personalization, poll interval -------------------------------------


def test_m3_cards_show_current_values(page: SettingsPage, fake: FakeController) -> None:
    assert page.pollCard.value() == 30
    assert [page.pollCard.comboBox.itemData(i) for i in range(page.pollCard.comboBox.count())] == [
        15,
        30,
        60,
        120,
        300,
    ]
    assert page.notifyCard.value() == "failed"
    assert page.notifyCard.comboBox.currentText() == "仅失败"
    assert page.themeCard.value() == "auto"
    assert page.themeCard.comboBox.currentText() == "跟随系统"
    assert not page.launchCard.isChecked()
    assert not page.startMinimizedCard.isChecked()
    assert page.closeToTrayCard.isChecked()
    assert not _saved(fake)  # showing values never saves


def test_combo_cards_save(page: SettingsPage, fake: FakeController) -> None:
    page.pollCard.select(120)
    page.notifyCard.select("all")
    page.themeCard.select("dark")
    page.themeCard.select("dark")  # unchanged: no save
    saved = _saved(fake)
    assert [s.poll_interval_s for s in saved] == [120, 120, 120]
    assert [s.notify_jobs for s in saved] == ["failed", "all", "all"]
    assert saved[-1].theme == "dark"
    assert page.themeCard.comboBox.currentText() == "深色"


def test_odd_poll_interval_is_kept(qtbot: QtBot) -> None:
    fake = FakeController(AppConfig(account="octocat", settings=AppSettings(poll_interval_s=45)))
    page = SettingsPage(fake)
    qtbot.addWidget(page)
    assert page.pollCard.value() == 45
    assert page.pollCard.comboBox.currentText() == "45 秒"
    page.pollCard.select(15)
    assert _saved(fake)[-1].poll_interval_s == 15
    assert page.pollCard.comboBox.findData(45) == -1  # a hand-edited value goes once replaced


def test_tray_switches_save(page: SettingsPage, fake: FakeController) -> None:
    page.startMinimizedCard.switchButton.setChecked(True)
    page.closeToTrayCard.switchButton.setChecked(False)
    saved = _saved(fake)
    assert [(s.start_minimized, s.close_to_tray) for s in saved] == [(True, True), (True, False)]
    fake.config_changed.emit(fake.config)  # refresh sets the same values: nothing saved
    assert len(_saved(fake)) == 2


def test_launch_on_login_card(page: SettingsPage, fake: FakeController) -> None:
    page.show()
    assert fake.calls_of("load_autostart_state") == [((), {})]
    assert page.launchCard.isEnabled()
    page.launchCard.switchButton.setChecked(True)
    assert fake.calls_of("set_launch_on_login") == [((True,), {})]
    assert page.launchCard.isChecked()
    assert page.launchCard.contentLabel.text() == LAUNCH_ON_TEXT  # fake answers registered
    assert not _saved(fake)  # the controller saves launch_on_login itself

    fake.autostart_state_loaded.emit(
        AutostartState(
            supported=True, registered=True, command="x", expected="x", disabled_by_user=True
        )
    )
    assert page.launchCard.contentLabel.text() == LAUNCH_DISABLED_TEXT
    assert "任务管理器" in LAUNCH_DISABLED_TEXT
    fake.autostart_state_loaded.emit(
        AutostartState(supported=True, registered=True, command="old", expected="new")
    )
    assert page.launchCard.contentLabel.text() == LAUNCH_MISMATCH_TEXT
    fake.autostart_state_loaded.emit(AutostartState(supported=True, registered=False))
    assert not page.launchCard.isChecked()
    assert len(fake.calls_of("set_launch_on_login")) == 1  # programmatic updates do not call

    fake.failed.emit("launch_on_login", "拒绝访问")
    assert len(fake.calls_of("load_autostart_state")) == 2
    assert not page.launchCard.isChecked()


def test_launch_on_login_unsupported(qtbot: QtBot, fake: FakeController) -> None:
    fake.autostart_supported = False
    page = SettingsPage(fake)
    qtbot.addWidget(page)
    page.show()
    assert not fake.calls_of("load_autostart_state")
    assert not page.launchCard.isEnabled()
    assert page.launchCard.contentLabel.text() == "只支持 Windows"

    fake.autostart_supported = True
    fake.autostart_state_loaded.emit(AutostartState(supported=False))
    assert not page.launchCard.isEnabled()


@pytest.mark.parametrize(
    ("state", "text"),
    [
        (AutostartState(supported=False), "只支持 Windows"),
        (AutostartState(supported=True), LAUNCH_TEXT),
        (
            AutostartState(supported=True, registered=True, command="a", expected="A"),
            LAUNCH_ON_TEXT,
        ),
    ],
)
def test_launch_text(state: AutostartState, text: str) -> None:
    assert launch_text(state) == text


def test_poll_options() -> None:
    assert poll_options(30) == [
        (15, "15 秒"),
        (30, "30 秒"),
        (60, "60 秒"),
        (120, "120 秒"),
        (300, "300 秒"),
    ]
    assert poll_options(7)[0] == (7, "7 秒")


def test_switch_cards_use_chinese_labels(page: SettingsPage) -> None:
    assert page.closeToTrayCard.switchButton.getText() == "开"
    assert page.startMinimizedCard.switchButton.getText() == "关"
    page.startMinimizedCard.switchButton.setChecked(True)
    assert page.startMinimizedCard.switchButton.getText() == "开"


def test_harden_root_asks_first(
    page: SettingsPage, fake: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MessageBox, "exec", lambda self: False)
    page.hardenRootCard.clicked.emit()
    assert not fake.calls_of("harden_runners_root")
    monkeypatch.setattr(MessageBox, "exec", lambda self: True)
    page.hardenRootCard.clicked.emit()
    assert fake.calls_of("harden_runners_root") == [((), {})]


def test_root_hardened_and_proxy_applied_show_info_bars(
    page: SettingsPage, fake: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runnerdeck.ui.pages import settings_page

    shown: list[tuple[str, str, str]] = []
    for kind in ("success", "warning", "error"):
        monkeypatch.setattr(
            settings_page.InfoBar,
            kind,
            lambda title, content, *a, _k=kind, **kw: shown.append((_k, title, content)),
        )
    fake.runners_root_hardened.emit("D:\\runners")
    fake.proxy_applied.emit(
        ProxyApplyResult(updated=("a", "b"), custom=("b",), failed=(("c", "无法写入"),))
    )
    fake.proxy_applied.emit(ProxyApplyResult())
    kinds = [k for k, _, _ in shown]
    assert kinds == ["success", "success", "warning", "error"]
    assert "D:\\runners" in shown[0][2]
    assert "2 个" in shown[1][2] and "b" in shown[2][2] and "c：无法写入" in shown[3][2]


def test_update_card_checks_then_offers_the_download(
    page: SettingsPage, fake: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runnerdeck.core.models import UpdateInfo

    opened: list[str] = []
    monkeypatch.setattr(
        QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString()) or True)
    )
    page.updateCard.clicked.emit()
    assert fake.calls_of("check_updates") == [((), {})]
    fake.update_checked.emit(UpdateInfo(current="1.0.0", latest="1.0.0"))
    assert "已是最新" in page.updateCard.contentLabel.text()
    fake.update_checked.emit(UpdateInfo(current="1.0.0", error="网络不通"))
    assert "网络不通" in page.updateCard.contentLabel.text()
    fake.update_checked.emit(
        UpdateInfo(current="1.0.0", latest="1.1.0", newer=True, url="https://x/r")
    )
    assert (
        "1.1.0" in page.updateCard.contentLabel.text() and page.updateCard.button.text() == "下载"
    )
    page.updateCard.clicked.emit()
    assert opened == ["https://x/r"] and len(fake.calls_of("check_updates")) == 1


def test_check_updates_switch_saves(page: SettingsPage, fake: FakeController) -> None:
    assert page.checkUpdatesCard.isChecked()
    page.checkUpdatesCard.setChecked(False)
    assert fake.config.settings.check_updates is False


# --- pip mirror ----------------------------------------------------------------------------

TENCENT = "https://mirrors.cloud.tencent.com/pypi/simple/"
ALIYUN = "https://mirrors.aliyun.com/pypi/simple/"


def _speeds() -> list[Any]:
    from runnerdeck.core.models import MirrorSpeed

    mib = 1024 * 1024
    return [
        MirrorSpeed(TENCENT, "腾讯云", 29 * mib, 1.0),
        MirrorSpeed("https://mirrors.ustc.edu.cn/pypi/simple/", "中国科学技术大学", error="超时"),
        MirrorSpeed(ALIYUN, "阿里云", int(0.41 * mib), 1.0),
    ]


def test_pip_mirror_card_shows_and_saves_the_choice(
    page: SettingsPage, fake: FakeController
) -> None:
    assert page.pipMirrorCard.value() is None
    assert page.pipMirrorCard.comboBox.currentText().startswith("不设置")
    page.pipMirrorCard.select(TENCENT)
    assert fake.config.settings.pip_index_url == TENCENT
    page.pipMirrorCard.select(None)
    assert fake.config.settings.pip_index_url is None


def test_pip_mirror_card_keeps_a_custom_value(qtbot: QtBot) -> None:
    custom = "https://pypi.example.com/simple/"
    fake = FakeController(AppConfig(account="octocat", settings=AppSettings(pip_index_url=custom)))
    page = SettingsPage(fake)  # type: ignore[arg-type]
    qtbot.addWidget(page)
    assert page.pipMirrorCard.value() == custom
    assert page.pipMirrorCard.comboBox.currentText() == "自定义（pypi.example.com）"
    assert not _saved(fake)


def test_pip_speed_test_offers_the_fastest_mirror(
    page: SettingsPage, fake: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runnerdeck.ui.pages import settings_page

    fake.save_settings(fake.config.settings.model_copy(update={"pip_index_url": ALIYUN}))
    dialogs: list[Any] = []

    def accept(dialog: Any) -> bool:
        dialogs.append(dialog)
        return True

    monkeypatch.setattr(settings_page.MirrorSpeedDialog, "exec", accept)
    page.pip_test_button.click()
    assert fake.calls_of("test_pip_mirrors") == [((), {})]
    assert not page.pip_test_button.isEnabled() and page.pip_test_button.text() == "测速中…"

    fake.pip_mirrors_tested.emit(_speeds())
    assert page.pip_test_button.isEnabled() and page.pip_test_button.text() == "测速"
    assert "腾讯云 最快（29.0 MB/s）" in page.pipMirrorCard.contentLabel.text()
    (dialog,) = dialogs
    assert dialog.rows == [
        ("腾讯云", "29.0 MB/s"),
        ("阿里云（当前）", "0.41 MB/s"),
        ("中国科学技术大学", "失败：超时"),
    ]
    assert dialog.best_url == TENCENT and dialog.yesButton.text() == "使用腾讯云"
    assert fake.config.settings.pip_index_url == TENCENT


def test_pip_speed_test_closed_or_already_fastest_saves_nothing(
    page: SettingsPage, fake: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runnerdeck.ui.pages import settings_page

    monkeypatch.setattr(settings_page.MirrorSpeedDialog, "exec", lambda _d: False)
    fake.pip_mirrors_tested.emit(_speeds())
    assert not _saved(fake)

    fake.save_settings(fake.config.settings.model_copy(update={"pip_index_url": TENCENT}))
    shown: list[Any] = []
    monkeypatch.setattr(settings_page.MirrorSpeedDialog, "exec", lambda d: shown.append(d) or True)
    fake.pip_mirrors_tested.emit(_speeds())
    (dialog,) = shown
    assert dialog.best_url is None and dialog.yesButton.isHidden()
    assert [s.pip_index_url for s in _saved(fake)] == [TENCENT]


def test_pip_speed_test_failure_restores_the_button(
    page: SettingsPage, fake: FakeController
) -> None:
    page.pip_test_button.click()
    fake.failed.emit("pip_mirror_test", "代理地址不合法")
    assert page.pip_test_button.isEnabled() and page.pip_test_button.text() == "测速"
    assert "测速失败" in page.pipMirrorCard.contentLabel.text()


def test_pip_index_applied_shows_info_bars(
    page: SettingsPage, fake: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runnerdeck.ui.pages import settings_page

    shown: list[tuple[str, str, str]] = []
    for kind in ("success", "warning"):
        monkeypatch.setattr(
            settings_page.InfoBar,
            kind,
            lambda title, content, *a, _k=kind, **kw: shown.append((_k, title, content)),
        )
    fake.pip_index_applied.emit(ProxyApplyResult(updated=("a",), custom=("b",)))
    assert [(k, t) for k, t, _ in shown] == [
        ("success", "pip 镜像源已写入 runner"),
        ("warning", "保留了自定义 pip 镜像源"),
    ]
