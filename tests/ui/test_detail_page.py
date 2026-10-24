"""RunnerDetailPage: empty state, header / actions, live output of one id, `_diag` logs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from PyQt5.QtGui import QDesktopServices
from pytestqt.qtbot import QtBot
from qfluentwidgets import MessageBox

from runnerdeck.core.models import AppConfig, DiagLog, DisplayState, LocalState
from runnerdeck.core.status import DETAIL_EXTERNAL_LOCAL, LABELS
from runnerdeck.ui.components.runner_card import RemoveRunnerDialog
from runnerdeck.ui.pages.runner_detail_page import (
    EMPTY_TEXT,
    NO_DIAG_TEXT,
    TAB_DIAG,
    TAB_OUTPUT,
    RunnerDetailPage,
    diag_item_text,
    format_size,
    labels_text,
)
from ui.conftest import FakeController, make_display, make_record


@pytest.fixture
def page(qtbot: QtBot, fake_with_records: FakeController) -> RunnerDetailPage:
    widget = RunnerDetailPage(fake_with_records)
    qtbot.addWidget(widget)
    widget.show()
    return widget


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """MessageBox texts; every box answers yes."""
    asked: list[str] = []
    original_init = MessageBox.__init__

    def _init(self: MessageBox, title: str, content: str, parent=None) -> None:  # type: ignore[no-untyped-def]
        asked.append(content)
        original_init(self, title, content, parent)

    monkeypatch.setattr(MessageBox, "__init__", _init)
    monkeypatch.setattr(MessageBox, "exec", lambda self: True)
    return asked


def _log(directory: Path, name: str, kind: str = "runner", size: int = 2048) -> DiagLog:
    return DiagLog(
        path=directory / "_diag" / name,
        kind=kind,  # type: ignore[arg-type]
        modified=datetime(2026, 10, 1, 8, 30, tzinfo=UTC),
        size=size,
    )


def test_empty_state_for_none_and_unknown(page: RunnerDetailPage) -> None:
    assert page.objectName() == "detail"
    assert page.record is None
    assert page.emptyLabel.isVisibleTo(page) and page.emptyLabel.text() == EMPTY_TEXT
    assert not page.content.isVisibleTo(page)
    assert not page.startButton.isVisibleTo(page)
    page.show_record("no-such-id")
    assert page.record is None and page.emptyLabel.isVisibleTo(page)
    page.request_start()  # no record: no-ops, no crash
    page.request_stop()
    page.reload_diag_logs()


def test_shows_record_facts_and_display(
    page: RunnerDetailPage, fake_with_records: FakeController
) -> None:
    fake = fake_with_records
    record = fake.records[0]
    fake.displays[record.id] = make_display(DisplayState.STALLED, detail="网络")
    page.show_record(record.id)
    assert not page.emptyLabel.isVisibleTo(page)
    assert page.nameLabel.text() == "pc-alpha"
    assert page.repoValue.text() == "octocat/alpha"
    assert page.pathValue.text() == record.path
    assert page.labelsValue.text() == labels_text([]) == "（无自定义标签）"
    assert page.idValue.text() == "42"
    assert page.versionValue.text() == "未知"
    assert page.badge.text() == LABELS[DisplayState.STALLED]
    assert page.detailLabel.text() == "网络"
    assert page.startButton.isVisibleTo(page) and not page.stopButton.isVisibleTo(page)
    assert not page.autoStartSwitch.isChecked()

    fake.set_state(record.id, LocalState.IDLE)  # display follows the pinned STALLED
    assert page.stopButton.isVisibleTo(page)
    fake.set_display(record.id, make_display(DisplayState.IDLE))
    assert page.badge.text() == LABELS[DisplayState.IDLE]
    assert not page.detailLabel.isVisibleTo(page)
    fake.display_changed.emit(fake.records[1].id, make_display(DisplayState.ERROR))
    assert page.badge.text() == LABELS[DisplayState.IDLE]  # another runner's change
    fake.busy_changed.emit(record.id, True)
    assert not page.stopButton.isEnabled()


def test_live_output_appends_only_own_lines(
    page: RunnerDetailPage, fake_with_records: FakeController
) -> None:
    fake = fake_with_records
    rid = fake.records[0].id
    fake.outputs[rid] = ["line 1", "line 2"]
    page.show_record(rid)
    assert page.current_tab == TAB_OUTPUT
    fake.runner_line.emit(rid, "line 3")
    fake.runner_line.emit(fake.records[1].id, "noise")
    assert page.outputView.text() == "line 1\nline 2\nline 3"

    fake.config_changed.emit(fake.config)  # same record: output kept
    assert page.outputView.text().endswith("line 3")
    page.show_record(fake.records[1].id)
    assert page.outputView.text() == ""
    fake.runner_line.emit(rid, "late")
    assert "late" not in page.outputView.text()


def test_record_removed_shows_empty_state(
    page: RunnerDetailPage, fake_with_records: FakeController
) -> None:
    fake = fake_with_records
    rid = fake.records[0].id
    page.show_record(rid)
    fake.config.remove(rid)
    fake.runner_forgotten.emit(rid)
    fake.config_changed.emit(fake.config)
    assert page.record is None
    assert page.emptyLabel.isVisibleTo(page)
    fake.runner_line.emit(rid, "after removal")
    assert page.outputView.text() == ""


def test_actions_call_the_controller(
    page: RunnerDetailPage,
    fake_with_records: FakeController,
    monkeypatch: pytest.MonkeyPatch,
    answers: list[str],
) -> None:
    fake = fake_with_records
    record = fake.records[0]
    page.show_record(record.id)
    opened: list[str] = []
    monkeypatch.setattr(
        QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString()) or True)
    )
    page.startButton.click()
    page.autoStartSwitch.setChecked(True)
    page.openDirButton.click()
    page.githubButton.click()
    assert fake.calls_of("start") == [((record.id,), {})]
    assert fake.calls_of("set_auto_start") == [((record.id, True), {})]
    assert fake.calls_of("open_dir") == [((record.id,), {})]
    assert opened == ["https://github.com/octocat/alpha/settings/actions/runners"]

    # the switch follows the saved record without calling the controller again
    fake.config.upsert(record.model_copy(update={"auto_start": True}))
    fake.config_changed.emit(fake.config)
    assert page.autoStartSwitch.isChecked()
    assert len(fake.calls_of("set_auto_start")) == 1

    fake.set_state(record.id, LocalState.BUSY, "build")
    page.stopButton.click()
    assert fake.calls_of("stop") == [((record.id,), {})]
    assert "正在执行 job build" in answers[-1]

    menu = {a.text(): a for a in page.build_menu().actions() if a.text()}
    assert set(menu) == {"转为服务…", "移除", "取消登记"}
    assert not menu["取消登记"].isEnabled()  # running

    fake.set_state(record.id, LocalState.STOPPED)
    monkeypatch.setattr(RemoveRunnerDialog, "exec", lambda self: True)
    menu = {a.text(): a for a in page.build_menu().actions() if a.text()}
    menu["移除"].trigger()
    menu["取消登记"].trigger()
    assert fake.calls_of("remove") == [((record.id,), {"keep_work": False})]
    assert fake.calls_of("forget") == [((record.id,), {})]


def test_external_take_over_and_kill(
    page: RunnerDetailPage, fake_with_records: FakeController, answers: list[str]
) -> None:
    fake = fake_with_records
    rid = fake.records[0].id
    page.show_record(rid)
    fake.remote_busy_ids.add(rid)
    fake.set_display(rid, make_display(DisplayState.EXTERNAL, detail=DETAIL_EXTERNAL_LOCAL))
    assert page.external and page.startButton.text() == "接管"
    page.startButton.click()
    assert fake.calls_of("take_over") == [((rid,), {})]
    assert "僵尸 job" in answers[-1]
    menu = {a.text(): a for a in page.build_menu().actions() if a.text()}
    menu["结束进程"].trigger()
    assert fake.calls_of("kill_external") == [((rid,), {})]
    assert not fake.calls_of("start")


def test_diag_logs_load_lazily_and_select(
    page: RunnerDetailPage, fake_with_records: FakeController
) -> None:
    fake = fake_with_records
    record = fake.records[0]
    page.show_record(record.id)
    assert not fake.calls_of("load_diag_logs")  # only when the tab is opened

    page.switch_tab(TAB_DIAG)
    assert page.tabStack.currentWidget() is page.diagPanel
    assert fake.calls_of("load_diag_logs") == [((record.id,), {})]
    assert "正在读取" in page.diagStatusLabel.text()

    root = Path(record.path)
    newest, older = _log(root, "Runner_2.log"), _log(root, "Worker_1.log", "worker", 10)
    fake.diag_logs_loaded.emit(fake.records[1].id, [_log(root, "other.log")])  # ignored
    assert page.diagCombo.count() == 0
    fake.diag_logs_loaded.emit(record.id, [newest, older])
    assert page.diagCombo.count() == 2
    assert page.diagCombo.currentText() == diag_item_text(newest)
    assert fake.calls_of("load_diag_text") == [((record.id, str(newest.path)), {})]

    fake.diag_text_loaded.emit(record.id, str(older.path), "stale answer")  # not selected
    assert page.diagView.text() == "正在读取…"
    fake.diag_text_loaded.emit(record.id, str(newest.path), "[2026] Runner listener started")
    assert page.diagView.text() == "[2026] Runner listener started"

    page.diagCombo.setCurrentIndex(1)  # the user picks the worker log
    assert fake.calls_of("load_diag_text")[-1] == ((record.id, str(older.path)), {})
    fake.failed.emit("diag", "无法读取 Worker_1.log：被占用")
    assert page.diagStatusLabel.text() == "读取失败：无法读取 Worker_1.log：被占用"
    assert page.diagView.text() == ""

    page.diagRefreshButton.click()  # keeps the selection when the file still exists
    fake.diag_logs_loaded.emit(record.id, [newest, older])
    assert page.diagCombo.currentIndex() == 1
    assert fake.calls_of("load_diag_text")[-1] == ((record.id, str(older.path)), {})

    page.switch_tab(TAB_OUTPUT)
    page.switch_tab(TAB_DIAG)
    assert len(fake.calls_of("load_diag_logs")) == 2  # already loaded for this record


def test_diag_empty_and_other_failures(
    page: RunnerDetailPage, fake_with_records: FakeController
) -> None:
    fake = fake_with_records
    rid = fake.records[0].id
    page.show_record(rid)
    page.switch_tab(TAB_DIAG)
    fake.diag_logs_loaded.emit(rid, [])
    assert page.diagStatusLabel.text() == NO_DIAG_TEXT
    assert not fake.calls_of("load_diag_text")
    fake.failed.emit("diag", "late")  # nothing pending: ignored
    assert page.diagStatusLabel.text() == NO_DIAG_TEXT
    fake.failed.emit("start", "unrelated")
    assert page.diagStatusLabel.text() == NO_DIAG_TEXT

    page.show_record(fake.records[1].id)  # diag tab open: loads for the new record
    assert fake.calls_of("load_diag_logs")[-1] == ((fake.records[1].id,), {})


def test_open_diag_dir(
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_infobars: list[tuple[str, str, str]],
) -> None:
    record = make_record(path=str(tmp_path / "runner"))
    fake = FakeController(AppConfig(account="octocat", runners=[record]))
    page = RunnerDetailPage(fake)
    qtbot.addWidget(page)
    page.show_record(record.id)
    opened: list[str] = []
    monkeypatch.setattr(
        QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toLocalFile()))
    )
    page.diagOpenButton.click()
    assert not opened and no_infobars[0][1] == "没有 _diag 目录"
    (tmp_path / "runner" / "_diag").mkdir(parents=True)
    page.diagOpenButton.click()
    assert [Path(p) for p in opened] == [tmp_path / "runner" / "_diag"]


def test_back_button(page: RunnerDetailPage) -> None:
    backs: list[bool] = []
    page.back_requested.connect(lambda: backs.append(True))
    page.backButton.click()
    assert backs == [True]


def test_helpers(tmp_path: Path) -> None:
    assert format_size(10) == "10 B"
    assert format_size(2048) == "2.0 KB"
    assert format_size(3 * 1024 * 1024) == "3.0 MB"
    text = diag_item_text(_log(tmp_path, "Worker_1.log", "worker", 10))
    assert text.startswith("Worker_1.log · Worker · 2026-10-01") and text.endswith("10 B")
    assert labels_text(["gpu", "ci"]) == "gpu, ci"


def test_shows_runner_version(page: RunnerDetailPage, fake_with_records: FakeController) -> None:
    record = fake_with_records.records[0]
    fake_with_records.versions[record.id] = "2.338.1"
    page.show_record(record.id)
    assert page.versionValue.text() == "2.338.1"


def test_service_record_facts_and_menu(
    page: RunnerDetailPage, fake_with_records: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qfluentwidgets import MessageBox

    from runnerdeck.core.models import RunnerMode

    fake = fake_with_records
    record = fake.records[0]
    page.show_record(record.id)
    assert "进程" in page.modeValue.text() and page.autoStartSwitch.isVisibleTo(page)
    monkeypatch.setattr(MessageBox, "exec", lambda self: True)
    actions = {a.text(): a for a in page.build_menu().actions() if a.text()}
    actions["转为服务…"].trigger()
    assert fake.calls_of("convert_to_service") == [((record.id,), {})]

    service = record.model_copy(
        update={"mode": RunnerMode.SERVICE, "service_name": "actions.runner.o-r.x"}
    )
    fake.config.upsert(service)
    fake.config_changed.emit(fake.config)
    page.show_record(record.id)
    assert "actions.runner.o-r.x" in page.modeValue.text()
    assert not page.autoStartSwitch.isVisibleTo(page)
    actions = {a.text(): a for a in page.build_menu().actions() if a.text()}
    actions["授权启停…"].trigger()
    assert fake.calls_of("grant_service_control") == [((record.id,), {})]
