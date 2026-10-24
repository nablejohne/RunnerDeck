"""AddWizardPage: repo list -> plan -> progress, with the fake controller answering at once."""

from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

from runnerdeck.core.models import AppConfig, AppSettings
from runnerdeck.ui.pages import add_wizard_page
from runnerdeck.ui.pages.add_wizard_page import (
    STEP_CONFIG,
    STEP_PROGRESS,
    STEP_REPO,
    AddWizardPage,
    repo_item_text,
    split_labels,
)
from ui.conftest import FakeController, make_plan, make_record, make_repo


@pytest.fixture
def fake() -> FakeController:
    config = AppConfig(
        account="octocat",
        settings=AppSettings(default_labels=["gpu", "win"]),
        runners=[make_record("pc-alpha", "alpha")],
    )
    return FakeController(config)


@pytest.fixture
def page(qtbot: QtBot, fake: FakeController) -> AddWizardPage:
    widget = AddWizardPage(fake)
    qtbot.addWidget(widget)
    widget.show()
    return widget


def _select(page: AddWizardPage, full_name: str) -> None:
    items = page.repoStep.listWidget.findItems(full_name, add_wizard_page.Qt.MatchContains)
    assert items, full_name
    page.repoStep.listWidget.setCurrentItem(items[0])


def test_loads_repos_on_show_and_marks_local_ones(
    page: AddWizardPage, fake: FakeController
) -> None:
    assert fake.calls_of("load_repos") == [((), {})]
    assert page.repoStep.spinner.isVisibleTo(page.repoStep)
    assert not page.nextButton.isEnabled()

    fake.repos_loaded.emit(
        [
            make_repo("octocat/alpha"),
            make_repo("octocat/pub", private=False, fork=True, archived=True),
        ]
    )
    texts = [page.repoStep.listWidget.item(i).text() for i in range(2)]
    assert "本机已有 runner" in texts[0] and "私有" in texts[0]
    assert "公开" in texts[1] and "fork" in texts[1] and "已归档" in texts[1]
    assert not page.repoStep.spinner.isVisibleTo(page.repoStep)

    page.repoStep.searchEdit.setText("pub")
    assert page.repoStep.listWidget.count() == 1
    page.repoStep.searchEdit.clear()
    assert page.repoStep.listWidget.count() == 2

    fake.calls.clear()
    page.repoStep.reloadButton.click()
    assert fake.calls_of("load_repos") == [((), {})]


def test_repo_load_failure_shows_message(page: AddWizardPage, fake: FakeController) -> None:
    fake.failed.emit("repos", "没有网络")
    assert "没有网络" in page.repoStep.statusLabel.text()
    assert page.repoStep.reloadButton.isEnabled()


def test_walks_three_steps_for_private_repo(
    page: AddWizardPage, fake: FakeController, qtbot: QtBot, no_infobars: list
) -> None:
    fake.repos_loaded.emit([make_repo("octocat/demo")])
    _select(page, "octocat/demo")
    assert page.nextButton.isEnabled()
    page.nextButton.click()
    assert page.step == STEP_CONFIG
    assert page.configStep.labelsEdit.text() == "gpu, win"
    assert page.configStep.psPolicyBox.isChecked() and page.configStep.startAfterBox.isChecked()
    assert page.configStep.autoStartBox.isChecked()  # M3: auto-start by default

    page.configStep.nameEdit.setText("my-runner")
    page.configStep.labelsEdit.setText("gpu, ci")
    page.configStep.psPolicyBox.setChecked(False)
    fake.auto_plan = make_plan(
        "octocat/demo", name="my-runner", labels=("gpu", "ci"), runner_env=()
    )
    page.nextButton.click()
    assert fake.calls_of("plan_add") == [
        (("octocat", "demo"), {"name": "my-runner", "labels": ["gpu", "ci"], "ps_policy": False})
    ]
    assert page.plan is fake.auto_plan
    assert page.configStep.summaryLabel.isVisibleTo(page.configStep)
    assert "my-runner" in page.configStep.summaryLabel.text()
    assert not page.configStep.confirmBox.isVisibleTo(page.configStep)  # private: no risk box
    assert page.nextButton.text() == "开始添加" and page.nextButton.isEnabled()
    page.configStep.autoStartBox.setChecked(False)
    assert page.plan is fake.auto_plan  # not part of the plan: no re-check needed

    fake.auto_add_steps = [("release", "查询"), ("download", "下载 50%"), ("configure", "注册")]
    page.nextButton.click()
    assert page.step == STEP_PROGRESS
    assert fake.calls_of("add_runner") == [
        ((fake.auto_plan,), {"start_after": True, "auto_start": False})
    ]
    assert not page.configStep.autoStartBox.isEnabled()
    rows = page.progressStep.rows
    assert rows["release"][1].isVisibleTo(page.progressStep)  # check mark
    assert rows["configure"][0].isVisibleTo(page.progressStep)  # spinner on the current step
    assert "注册" in rows["configure"][2].text()
    assert not rows["env"][2].isVisibleTo(page.progressStep)  # no runner_env in this plan
    assert not page.nextButton.isEnabled() and not page.backButton.isEnabled()

    fake.config.upsert(make_record("my-runner", "demo"))
    fake.runner_added.emit(fake.config.runners[-1])
    assert ("success", "添加成功", "runner my-runner 已注册") in no_infobars
    assert rows["done"][1].isVisibleTo(page.progressStep)
    with qtbot.waitSignal(page.finished, timeout=5000):
        pass
    assert page.step == STEP_REPO
    assert page.configStep.nameEdit.text() == ""
    assert page.configStep.autoStartBox.isChecked()  # reset for the next runner


def test_public_repo_needs_confirmation(
    page: AddWizardPage, fake: FakeController, no_infobars: list
) -> None:
    fake.repos_loaded.emit([make_repo("octocat/pub", private=False)])
    _select(page, "octocat/pub")
    page.nextButton.click()
    fake.auto_plan = make_plan(
        "octocat/pub", private=False, warnings=("公开仓库：fork 的 PR 可以在这台机器上执行代码",)
    )
    page.nextButton.click()
    assert page.configStep.confirmBox.isVisibleTo(page.configStep)
    assert not page.nextButton.isEnabled()
    assert ("warning", "请注意", fake.auto_plan.warnings[0]) in no_infobars
    assert "⚠" in page.configStep.warningLabel.text()

    page.configStep.confirmBox.setChecked(True)
    assert page.nextButton.isEnabled()
    page.nextButton.click()
    assert page.step == STEP_PROGRESS
    assert fake.calls_of("add_runner")[0][1] == {"start_after": True, "auto_start": True}
    assert page.progressStep.rows["env"][2].isVisibleTo(page.progressStep)


def test_editing_inputs_invalidates_plan(page: AddWizardPage, fake: FakeController) -> None:
    fake.repos_loaded.emit([make_repo("octocat/demo")])
    _select(page, "octocat/demo")
    page.nextButton.click()
    fake.auto_plan = make_plan()
    page.nextButton.click()
    assert page.plan is not None
    page.configStep.nameEdit.setText("other")
    assert page.plan is None
    assert page.nextButton.text() == "检查"
    page.backButton.click()
    assert page.step == STEP_REPO


def test_plan_failure_and_add_failure(
    page: AddWizardPage, fake: FakeController, no_infobars: list
) -> None:
    fake.repos_loaded.emit([make_repo("octocat/demo")])
    _select(page, "octocat/demo")
    page.nextButton.click()
    page.nextButton.click()  # plan requested, fake answers nothing yet
    assert not page.nextButton.isEnabled()
    fake.failed.emit("plan_add", "标签不合法")
    assert page.nextButton.isEnabled() and page.plan is None
    assert ("error", "检查失败", "标签不合法") in no_infobars

    fake.auto_plan = make_plan()
    page.nextButton.click()
    fake.auto_add_steps = [("release", "查询"), ("rollback", "回滚中")]
    page.nextButton.click()
    assert page.progressStep.statusLabel.text() == "回滚中"
    fake.failed.emit("add", "config.cmd 失败")
    assert page.progressStep.statusLabel.text().startswith("失败：config.cmd 失败")
    assert ("error", "添加失败", "config.cmd 失败") in no_infobars
    assert page.backButton.isEnabled()
    page.backButton.click()
    assert page.step == STEP_CONFIG and page.plan is None


def test_add_failure_with_output_shows_text(
    page: AddWizardPage, fake: FakeController, no_infobars: list
) -> None:
    fake.repos_loaded.emit([make_repo("octocat/demo")])
    _select(page, "octocat/demo")
    page.nextButton.click()
    fake.auto_plan = make_plan()
    page.nextButton.click()
    page.nextButton.click()

    class Exc:
        output = "config.cmd output"

    page._on_failed("add", "出错", Exc())  # a 3-argument failed signal is also accepted
    assert page.progressStep.outputEdit.isVisibleTo(page.progressStep)
    assert page.progressStep.outputEdit.toPlainText() == "config.cmd output"


def test_helpers() -> None:
    assert split_labels(" gpu, , ci ,") == ["gpu", "ci"]
    assert repo_item_text(make_repo("a/b", private=False), True).endswith("公开 / 本机已有 runner")
