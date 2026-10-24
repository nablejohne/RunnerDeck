"""DashboardPage + RunnerCard: cards follow config_changed / runner_state / display_changed /
busy_changed; import, discovery banner, EXTERNAL take-over / kill, BUSY stop confirmation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QFileDialog
from pytestqt.qtbot import QtBot
from qfluentwidgets import InfoLevel, MessageBox

from runnerdeck.core.models import AppConfig, Discovery, DisplayState, LocalState, StrayRunner
from runnerdeck.core.process import ExternalStop
from runnerdeck.core.status import DETAIL_EXTERNAL_LOCAL, LABELS
from runnerdeck.ui.components.runner_card import (
    RemoveRunnerDialog,
    RunnerCard,
    forget_question,
    kill_question,
    stop_question,
)
from runnerdeck.ui.components.status_badge import BADGE_LEVELS, StatusBadge
from runnerdeck.ui.pages.dashboard_page import EMPTY_TEXT, DashboardPage, stray_text, summarize
from ui.conftest import FakeController, make_display, make_record, make_status_row


@pytest.fixture
def page(qtbot: QtBot, fake_with_records: FakeController) -> DashboardPage:
    widget = DashboardPage(fake_with_records)
    qtbot.addWidget(widget)
    widget.show()
    return widget


@pytest.fixture
def questions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every MessageBox text and answer yes (tests re-patch exec to say no)."""
    asked: list[str] = []
    original_init = MessageBox.__init__

    def _init(self: MessageBox, title: str, content: str, parent=None) -> None:  # type: ignore[no-untyped-def]
        asked.append(content)
        original_init(self, title, content, parent)

    monkeypatch.setattr(MessageBox, "__init__", _init)
    monkeypatch.setattr(MessageBox, "exec", lambda self: True)
    return asked


def _external(fake: FakeController, rid: str) -> None:
    fake.set_display(rid, make_display(DisplayState.EXTERNAL, detail=DETAIL_EXTERNAL_LOCAL))


def test_empty_state(qtbot: QtBot, fake: FakeController) -> None:
    page = DashboardPage(fake)
    qtbot.addWidget(page)
    assert not page.cards
    assert page.emptyLabel.text() == EMPTY_TEXT
    assert not page.startAllButton.isEnabled()
    assert not page.banner.isVisibleTo(page)


def test_cards_rebuilt_on_config_changed(
    page: DashboardPage, fake_with_records: FakeController
) -> None:
    fake = fake_with_records
    assert len(page.cards) == 2
    fake.config = AppConfig(account="octocat", runners=[make_record("pc-only", "only")])
    fake.config_changed.emit(fake.config)
    assert [card.record.name for card in page.cards.values()] == ["pc-only"]
    assert not page.emptyLabel.isVisible()


def test_runner_state_updates_badge_and_button(
    page: DashboardPage, fake_with_records: FakeController
) -> None:
    """runner_state only drives the buttons; badge, detail and counters follow display_changed."""
    fake = fake_with_records
    rid = fake.records[0].id
    card = page.cards[rid]
    assert card.startButton.isVisibleTo(card)
    assert card.badge.text() == LABELS[DisplayState.STOPPED]

    fake.runner_state.emit(rid, LocalState.BUSY, "build")  # local state alone
    assert card.stopButton.isVisibleTo(card) and not card.startButton.isVisibleTo(card)
    assert card.running
    assert card.badge.text() == LABELS[DisplayState.STOPPED]  # badge unchanged
    assert page.summaryLabel.text() == "运行中 0 · 空闲 0 · 异常 0 · 已停止 2"

    fake.display_changed.emit(rid, make_display(DisplayState.BUSY, job="build"))
    assert card.badge.text() == f"{LABELS[DisplayState.BUSY]}：build"
    assert card.badge.level == BADGE_LEVELS[DisplayState.BUSY]
    assert page.summaryLabel.text() == "运行中 1 · 空闲 0 · 异常 0 · 已停止 1"

    fake.set_state(rid, LocalState.STOPPED)  # emits runner_state + display_changed
    assert card.startButton.isVisibleTo(card)
    assert card.badge.text() == LABELS[DisplayState.STOPPED]


def test_statuses_updated_does_not_touch_badges(
    page: DashboardPage, fake_with_records: FakeController
) -> None:
    fake = fake_with_records
    record = fake.records[0]
    fake.statuses_updated.emit([make_status_row(record, DisplayState.STALLED, detail="网络")])
    card = page.cards[record.id]
    assert card.badge.text() == LABELS[DisplayState.STOPPED]
    assert not card.detailLabel.isVisibleTo(card)


def test_buttons_emit_to_controller(
    page: DashboardPage, fake_with_records: FakeController, questions: list[str]
) -> None:
    fake = fake_with_records
    rid = fake.records[1].id
    card = page.cards[rid]
    card.startButton.click()
    fake.set_state(rid, LocalState.IDLE)
    card.stopButton.click()
    page.startAllButton.click()
    page.stopAllButton.click()
    assert [c[0] for c in fake.calls] == ["start", "stop", "start_all", "stop_all"]
    assert fake.calls_of("start") == [((rid,), {})]
    assert fake.calls_of("stop") == [((rid,), {})]
    assert questions == []  # nothing executes a job: no confirmation


def test_stop_busy_runner_asks_first(
    page: DashboardPage,
    fake_with_records: FakeController,
    monkeypatch: pytest.MonkeyPatch,
    questions: list[str],
) -> None:
    fake = fake_with_records
    rid = fake.records[0].id
    card = page.cards[rid]
    fake.set_state(rid, LocalState.BUSY, "build")
    monkeypatch.setattr(MessageBox, "exec", lambda self: False)
    card.stopButton.click()
    assert not fake.calls_of("stop")
    assert questions == [stop_question([("pc-alpha", "build")])]
    assert "正在执行 job build，停止会取消它" in questions[0]

    monkeypatch.setattr(MessageBox, "exec", lambda self: True)
    card.stopButton.click()
    assert fake.calls_of("stop") == [((rid,), {})]


def test_stop_all_asks_when_a_job_runs(
    page: DashboardPage,
    fake_with_records: FakeController,
    monkeypatch: pytest.MonkeyPatch,
    questions: list[str],
) -> None:
    fake = fake_with_records
    fake.set_state(fake.records[0].id, LocalState.BUSY, "build")
    fake.set_state(fake.records[1].id, LocalState.IDLE)
    monkeypatch.setattr(MessageBox, "exec", lambda self: False)
    page.stopAllButton.click()
    assert not fake.calls_of("stop_all")
    assert questions == [stop_question([("pc-alpha", "build")])]  # pc-beta is only idle

    monkeypatch.setattr(MessageBox, "exec", lambda self: True)
    page.stopAllButton.click()
    assert fake.calls_of("stop_all") == [((), {})]


def test_busy_disables_buttons_and_shows_spinner(
    page: DashboardPage, fake_with_records: FakeController
) -> None:
    fake = fake_with_records
    rid = fake.records[0].id
    card = page.cards[rid]
    fake.busy_changed.emit(rid, True)
    assert card.busy
    assert not card.startButton.isEnabled()
    assert card.spinner.isVisibleTo(card)
    fake.busy_changed.emit(rid, False)
    assert card.startButton.isEnabled()
    assert not card.spinner.isVisibleTo(card)


def test_refresh_disabled_until_statuses_arrive(
    page: DashboardPage, fake_with_records: FakeController
) -> None:
    fake = fake_with_records
    page.refreshButton.click()
    page.refreshButton.click()  # ignored while refreshing
    assert fake.calls_of("refresh_statuses") == [((), {"remote": True, "quiet": False})]
    assert not page.refreshButton.isEnabled()

    record = fake.records[0]
    fake.statuses_updated.emit([make_status_row(record, DisplayState.STALLED, detail="网络")])
    assert page.refreshButton.isEnabled()
    fake.set_display(record.id, make_display(DisplayState.STALLED, detail="网络"))
    card = page.cards[record.id]
    assert card.badge.text() == LABELS[DisplayState.STALLED]
    assert card.badge.level == InfoLevel.ERROR
    assert card.detailLabel.text() == "网络"
    assert page.summaryLabel.text().startswith("运行中 0 · 空闲 0 · 异常 1")

    page.refreshButton.click()
    fake.failed.emit("status", "boom")
    assert page.refreshButton.isEnabled()

    page.refreshButton.click()
    fake.refreshing_changed.emit(False)  # a quiet refresh ends without statuses / failed
    assert page.refreshButton.isEnabled()


def test_remove_via_dialog_passes_keep_work(
    page: DashboardPage, fake_with_records: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_with_records
    rid = fake.records[0].id
    card = page.cards[rid]
    monkeypatch.setattr(
        RemoveRunnerDialog, "exec", lambda self: self.keepWorkBox.setChecked(True) or True
    )
    card.ask_remove()
    assert fake.calls_of("remove") == [((rid,), {"keep_work": True})]

    monkeypatch.setattr(RemoveRunnerDialog, "exec", lambda self: False)
    card.ask_remove()
    assert len(fake.calls_of("remove")) == 1


def test_menu_actions(
    page: DashboardPage,
    fake_with_records: FakeController,
    monkeypatch: pytest.MonkeyPatch,
    questions: list[str],
) -> None:
    fake = fake_with_records
    rid = fake.records[0].id
    card = page.cards[rid]
    opened: list[str] = []
    monkeypatch.setattr(
        QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString()) or True)
    )
    details: list[str] = []
    page.detail_requested.connect(details.append)
    actions = {action.text(): action for action in card.build_menu().actions() if action.text()}
    assert set(actions) == {
        "详情",
        "打开目录",
        "在 GitHub 打开",
        "随 RunnerDeck 启动",
        "移除",
        "取消登记",
    }
    assert actions["随 RunnerDeck 启动"].isCheckable()
    assert not actions["随 RunnerDeck 启动"].isChecked()
    actions["详情"].trigger()
    actions["打开目录"].trigger()
    actions["在 GitHub 打开"].trigger()
    actions["随 RunnerDeck 启动"].trigger()
    actions["取消登记"].trigger()
    assert details == [rid]
    assert fake.calls_of("open_dir") == [((rid,), {})]
    assert opened == ["https://github.com/octocat/alpha/settings/actions/runners"]
    assert fake.calls_of("set_auto_start") == [((rid, True), {})]
    assert fake.calls_of("forget") == [((rid,), {})]
    assert questions == [forget_question(card.record)]
    assert "目录" in questions[0] and "保留" in questions[0]


def test_forget_disabled_while_running_and_cancel(
    page: DashboardPage, fake_with_records: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_with_records
    rid = fake.records[0].id
    card = page.cards[rid]
    monkeypatch.setattr(MessageBox, "exec", lambda self: False)
    card.ask_forget()
    assert not fake.calls_of("forget")
    fake.set_state(rid, LocalState.IDLE)
    actions = {a.text(): a for a in card.build_menu().actions() if a.text()}
    assert not actions["取消登记"].isEnabled()


def test_auto_start_menu_reflects_record(qtbot: QtBot) -> None:
    record = make_record(auto_start=True)
    fake = FakeController(AppConfig(account="octocat", runners=[record]))
    page = DashboardPage(fake)
    qtbot.addWidget(page)
    card = page.cards[record.id]
    actions = {a.text(): a for a in card.build_menu().actions() if a.text()}
    assert actions["随 RunnerDeck 启动"].isChecked()
    actions["随 RunnerDeck 启动"].trigger()
    assert fake.calls_of("set_auto_start") == [((record.id, False), {})]


def test_clicking_card_requests_detail(
    page: DashboardPage, fake_with_records: FakeController
) -> None:
    rid = fake_with_records.records[1].id
    details: list[str] = []
    page.detail_requested.connect(details.append)
    page.cards[rid].clicked.emit()
    assert details == [rid]


def test_external_card_offers_take_over_and_kill(
    page: DashboardPage,
    fake_with_records: FakeController,
    monkeypatch: pytest.MonkeyPatch,
    questions: list[str],
) -> None:
    fake = fake_with_records
    rid = fake.records[0].id
    card = page.cards[rid]
    assert card.startButton.text() == "启动"
    _external(fake, rid)
    assert card.external
    assert card.startButton.text() == "接管"
    assert card.badge.text() == LABELS[DisplayState.EXTERNAL]
    assert card.detailLabel.text() == DETAIL_EXTERNAL_LOCAL

    card.startButton.click()
    assert fake.calls_of("take_over") == [((rid,), {})]
    assert not fake.calls_of("start")
    assert questions[-1] == kill_question(card.record, take_over=True, remote_busy=False)
    assert "约 3 分钟" in questions[-1] and "僵尸 job" not in questions[-1]

    fake.remote_busy_ids.add(rid)
    actions = {a.text(): a for a in card.build_menu().actions() if a.text()}
    assert "结束进程" in actions
    actions["结束进程"].trigger()
    assert fake.calls_of("kill_external") == [((rid,), {})]
    assert "僵尸 job" in questions[-1]

    monkeypatch.setattr(MessageBox, "exec", lambda self: False)
    card.startButton.click()
    card.ask_kill()
    assert len(fake.calls_of("take_over")) == 1 and len(fake.calls_of("kill_external")) == 1

    fake.set_display(rid, make_display(DisplayState.STOPPED))
    assert card.startButton.text() == "启动"
    assert "结束进程" not in {a.text() for a in card.build_menu().actions()}


def test_import_button_picks_directory(
    page: DashboardPage, fake_with_records: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_with_records
    starts: list[str] = []

    def _pick(_parent, _title, start):  # type: ignore[no-untyped-def]
        starts.append(start)
        return "/tmp/runners/legacy"

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(_pick))
    page.importButton.click()
    assert fake.calls_of("import_runner") == [(("/tmp/runners/legacy",), {"auto_start": False})]
    assert starts == [fake.config.settings.runners_root]

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a: ""))
    page.importButton.click()
    assert len(fake.calls_of("import_runner")) == 1  # cancelled


def test_discovery_banner_import_and_kill(
    page: DashboardPage,
    fake_with_records: FakeController,
    monkeypatch: pytest.MonkeyPatch,
    questions: list[str],
) -> None:
    fake = fake_with_records
    root = Path("/tmp/runners")
    stray = StrayRunner(path=root / "ghost", pids=(11, 12), configured=True, service=False)
    service = StrayRunner(path=root / "svc", pids=(13,), configured=True, service=True)
    found = Discovery(
        root=root, unregistered=(root / "old-a", root / "old-b"), strays=(stray, service)
    )
    fake.discovery_updated.emit(found)
    banner = page.banner
    assert banner.isVisibleTo(page)
    assert banner.titleLabel.text() == "在 runner 根目录发现 2 个未登记的 runner 目录"
    assert banner.strayLabel.text() == "发现 2 个不属于 RunnerDeck 的 runner 进程"
    assert set(banner.importButtons) == {str(root / "old-a"), str(root / "old-b")}
    assert not banner.killButtons[str(service.path)].isEnabled()

    banner.importButtons[str(root / "old-a")].click()
    assert fake.calls_of("import_runner") == [((str(root / "old-a"),), {"auto_start": False})]
    fake.runner_imported.emit(make_record("pc-old-a", "old-a", path=str(root / "old-a")))
    assert set(banner.importButtons) == {str(root / "old-b")}

    banner.killButtons[str(stray.path)].click()
    assert fake.calls_of("kill_stray") == [((str(stray.path),), {})]
    assert "强制结束" in questions[-1]
    fake.external_stopped.emit(str(stray.path), ExternalStop(killed=[11, 12], survivors=[]))
    assert set(banner.killButtons) == {str(service.path)}

    monkeypatch.setattr(MessageBox, "exec", lambda self: False)
    page.ask_kill_stray(str(service.path))
    assert len(fake.calls_of("kill_stray")) == 1

    banner.closeButton.click()
    assert not banner.isVisibleTo(page)
    fake.runner_imported.emit(make_record("pc-old-b", "old-b", path=str(root / "old-b")))
    assert not banner.isVisibleTo(page)  # dismissed stays dismissed until the next scan
    fake.discovery_updated.emit(Discovery(root=root, strays=(service,)))
    assert banner.isVisibleTo(page)
    assert banner.titleLabel.text() == "发现 1 个不属于 RunnerDeck 的 runner 进程"
    assert not banner.strayLabel.isVisibleTo(page)

    fake.discovery_updated.emit(Discovery(root=root))
    assert not banner.isVisibleTo(page)


def test_stray_text() -> None:
    stray = StrayRunner(path=Path("/r/x"), pids=(1, 2), configured=False, service=True)
    assert stray_text(stray) == f"{Path('/r/x')}（PID 1、2 · 未配置 · Windows 服务）"


def test_status_badge_levels_cover_every_state(qtbot: QtBot) -> None:
    badge = StatusBadge()
    qtbot.addWidget(badge)
    assert set(BADGE_LEVELS) == set(DisplayState)
    for state in DisplayState:
        badge.set_state(state, "job")
        assert badge.level == BADGE_LEVELS[state]
        assert badge.text().startswith(LABELS[state])
    assert BADGE_LEVELS[DisplayState.IDLE] != BADGE_LEVELS[DisplayState.BUSY]


def test_summarize_uses_status_buckets() -> None:
    states = [
        DisplayState.IDLE,
        DisplayState.BUSY,
        DisplayState.CONNECTING,
        DisplayState.MISSING,
        DisplayState.STOPPED,
        DisplayState.UNKNOWN,
        DisplayState.SESSION_LINGERING,
    ]
    assert summarize(states) == "运行中 2 · 空闲 1 · 异常 1 · 已停止 3"
    assert summarize([]) == "运行中 0 · 空闲 0 · 异常 0 · 已停止 0"


def test_stop_question_texts() -> None:
    assert stop_question([("a", None)]).startswith("runner a 正在执行 job （名字未知）")
    many = stop_question([("a", "build"), ("b", "test")])
    assert "a：build" in many and "b：test" in many


def test_card_standalone_defaults(qtbot: QtBot) -> None:
    card = RunnerCard(make_record())
    qtbot.addWidget(card)
    assert not card.running and not card.busy and not card.external
    assert card.display_state is DisplayState.STOPPED
    assert not card.detailLabel.isVisibleTo(card)


def _confirm_during(monkeypatch: pytest.MonkeyPatch, during: Callable[[], None]) -> None:
    """Make the stop confirmation run `during` inside its "modal", then answer yes."""
    from PyQt5.QtCore import QCoreApplication, QEvent

    import runnerdeck.ui.components.runner_card as card_module

    def confirm(*_args: object, **_kwargs: object) -> bool:
        during()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)  # what exec() would do
        return True

    monkeypatch.setattr(card_module, "confirm_stop", confirm)


def test_card_survives_config_changed_while_its_dialog_is_open(
    page: DashboardPage, fake_with_records: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = fake_with_records.records[0].id
    fake_with_records.set_state(rid, LocalState.BUSY, "build")
    card = page.cards[rid]
    _confirm_during(
        monkeypatch, lambda: fake_with_records.config_changed.emit(fake_with_records.config)
    )
    card.request_stop()
    assert page.cards[rid] is card
    assert fake_with_records.calls_of("stop") == [((rid,), {})]


def test_answer_is_dropped_when_the_record_vanished_meanwhile(
    page: DashboardPage, fake_with_records: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = fake_with_records.records[0].id
    fake_with_records.set_state(rid, LocalState.BUSY, "build")
    card = page.cards[rid]

    def remove_record() -> None:
        fake_with_records.config.remove(rid)
        fake_with_records.config_changed.emit(fake_with_records.config)

    _confirm_during(monkeypatch, remove_record)
    card.request_stop()  # must not raise "wrapped C/C++ object ... has been deleted"
    assert rid not in page.cards
    assert fake_with_records.calls_of("stop") == []
