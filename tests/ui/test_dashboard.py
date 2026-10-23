"""DashboardPage: cards follow config_changed / runner_state / busy_changed / statuses_updated."""

from __future__ import annotations

import pytest
from PyQt5.QtGui import QDesktopServices
from pytestqt.qtbot import QtBot
from qfluentwidgets import InfoLevel

from runnerdeck.core.models import AppConfig, DisplayState, LocalState
from runnerdeck.core.status import LABELS
from runnerdeck.ui.components.log_dialog import LogDialog
from runnerdeck.ui.components.runner_card import RemoveRunnerDialog, RunnerCard
from runnerdeck.ui.components.status_badge import BADGE_LEVELS, StatusBadge
from runnerdeck.ui.pages.dashboard_page import EMPTY_TEXT, DashboardPage, summarize
from ui.conftest import FakeController, make_record, make_status_row


@pytest.fixture
def page(qtbot: QtBot, fake_with_records: FakeController) -> DashboardPage:
    widget = DashboardPage(fake_with_records)
    qtbot.addWidget(widget)
    widget.show()
    return widget


def test_empty_state(qtbot: QtBot, fake: FakeController) -> None:
    page = DashboardPage(fake)
    qtbot.addWidget(page)
    assert not page.cards
    assert page.emptyLabel.text() == EMPTY_TEXT
    assert not page.startAllButton.isEnabled()


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
    fake = fake_with_records
    rid = fake.records[0].id
    card = page.cards[rid]
    assert card.startButton.isVisibleTo(card)
    assert card.badge.text() == LABELS[DisplayState.STOPPED]

    fake.set_state(rid, LocalState.BUSY, "build")
    assert card.badge.text() == f"{LABELS[DisplayState.BUSY]}：build"
    assert card.badge.level == BADGE_LEVELS[DisplayState.BUSY]
    assert card.stopButton.isVisibleTo(card) and not card.startButton.isVisibleTo(card)
    assert card.running
    assert page.summaryLabel.text() == "运行中 1 · 空闲 0 · 异常 0 · 已停止 1"

    fake.set_state(rid, LocalState.STOPPED)
    assert card.startButton.isVisibleTo(card)


def test_buttons_emit_to_controller(page: DashboardPage, fake_with_records: FakeController) -> None:
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
    assert fake.calls_of("refresh_statuses") == [((), {})]
    assert not page.refreshButton.isEnabled()

    record = fake.records[0]
    fake.statuses_updated.emit([make_status_row(record, DisplayState.STALLED, detail="网络")])
    card = page.cards[record.id]
    assert page.refreshButton.isEnabled()
    assert card.badge.text() == LABELS[DisplayState.STALLED]
    assert card.badge.level == InfoLevel.ERROR
    assert card.detailLabel.text() == "网络"
    assert page.summaryLabel.text().startswith("运行中 0 · 空闲 0 · 异常 1")

    page.refreshButton.click()
    fake.failed.emit("status", "boom")
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
    page: DashboardPage, fake_with_records: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_with_records
    rid = fake.records[0].id
    card = page.cards[rid]
    opened: list[str] = []
    monkeypatch.setattr(
        QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString()) or True)
    )
    shown: list[str] = []
    monkeypatch.setattr(LogDialog, "exec", lambda self: shown.append(self.record_id) or 0)
    actions = {action.text(): action for action in card.build_menu().actions() if action.text()}
    assert set(actions) == {"打开目录", "查看输出", "在 GitHub 打开", "移除"}
    actions["打开目录"].trigger()
    actions["在 GitHub 打开"].trigger()
    actions["查看输出"].trigger()
    assert fake.calls_of("open_dir") == [((rid,), {})]
    assert opened == ["https://github.com/octocat/alpha/settings/actions/runners"]
    assert shown == [rid]


def test_log_dialog_appends_only_own_lines(qtbot: QtBot, fake_with_records: FakeController) -> None:
    fake = fake_with_records
    rid = fake.records[0].id
    fake.outputs[rid] = ["line 1", "line 2"]
    host = DashboardPage(fake)
    qtbot.addWidget(host)
    dialog = LogDialog(fake, rid, "pc-alpha", host)
    fake.runner_line.emit(rid, "line 3")
    fake.runner_line.emit("other", "noise")
    assert dialog.textEdit.toPlainText() == "line 1\nline 2\nline 3"
    dialog.copy_all()
    dialog.reject()
    fake.runner_line.emit(rid, "after close")
    assert "after close" not in dialog.textEdit.toPlainText()


def test_status_badge_levels_cover_every_state(qtbot: QtBot) -> None:
    badge = StatusBadge()
    qtbot.addWidget(badge)
    assert set(BADGE_LEVELS) == set(DisplayState)
    for state in DisplayState:
        badge.set_state(state, "job")
        assert badge.level == BADGE_LEVELS[state]
        assert badge.text().startswith(LABELS[state])
    assert BADGE_LEVELS[DisplayState.IDLE] != BADGE_LEVELS[DisplayState.BUSY]


def test_summarize_groups_states() -> None:
    states = [
        DisplayState.IDLE,
        DisplayState.BUSY,
        DisplayState.CONNECTING,
        DisplayState.MISSING,
        DisplayState.STOPPED,
        DisplayState.UNKNOWN,
    ]
    assert summarize(states) == "运行中 2 · 空闲 1 · 异常 1 · 已停止 2"


def test_card_standalone_defaults(qtbot: QtBot) -> None:
    card = RunnerCard(make_record())
    qtbot.addWidget(card)
    assert not card.running and not card.busy
    assert card.display_state is DisplayState.STOPPED
    assert not card.detailLabel.isVisibleTo(card)
