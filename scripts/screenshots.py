"""Render the README screenshots offscreen with sample data (docs/assets/*.png).

    uv run python scripts/screenshots.py [--out docs/assets] [--theme light|dark]

The real main window and pages, driven by tests/ui's FakeController: no account, no network,
no runners, nothing of the machine it runs on appears. Run it on Windows so the screenshots
use the Windows fonts; it works offscreen anywhere.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    # The offscreen platform only sees fonts in QT_QPA_FONTDIR: without this, no text at all.
    os.environ.setdefault(
        "QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    )
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))  # ui.conftest: FakeController and sample builders

# Window size per screenshot: the overview has three cards and needs less height.
SIZES = {
    "dashboard.png": (1100, 400),
    "detail-workflow.png": (1100, 690),
    "add-service.png": (1100, 700),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "assets")
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
    args = parser.parse_args()

    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication(["RunnerDeck"])  # noqa: F841 - must exist
    from runnerdeck.core.models import (
        DisplayState,
        JobMatch,
        JobTarget,
        LocalState,
        MatchKind,
        RepoInsights,
        RunnerMode,
        WorkflowRun,
    )
    from runnerdeck.ui.compat import disable_mac_native_frameless, tolerate_offscreen_window_handles
    from runnerdeck.ui.main_window import MainWindow
    from runnerdeck.ui.pages.add_wizard_page import STEP_CONFIG
    from runnerdeck.ui.pages.runner_detail_page import TAB_WORKFLOW
    from runnerdeck.ui.theme import apply_theme
    from ui.conftest import FakeController, make_display, make_plan, make_record

    disable_mac_native_frameless()
    tolerate_offscreen_window_handles()
    apply_theme(args.theme)

    web = make_record(
        "build-box", repo="web-app", labels=["windows", "build"],
        path=r"D:\runners\web-app", github_runner_id=31, auto_start=True,
    )  # fmt: skip
    gpu = make_record(
        "gpu-box", repo="ml-models", labels=["gpu"], path=r"D:\runners\ml-models",
        github_runner_id=57, mode=RunnerMode.SERVICE,
        service_name="actions.runner.octocat-ml-models.gpu-box",
    )  # fmt: skip
    docs = make_record("docs-box", repo="docs", path=r"D:\runners\docs", github_runner_id=12)
    fake = FakeController()
    fake.config.runners = [web, gpu, docs]
    fake.set_state(web.id, LocalState.IDLE)
    fake.set_state(gpu.id, LocalState.BUSY, "train-model")
    fake.displays.update(
        {
            web.id: make_display(DisplayState.IDLE),
            gpu.id: make_display(DisplayState.BUSY, job="train-model"),
            docs.id: make_display(DisplayState.STOPPED),
        }
    )
    fake.versions = {r.id: "2.338.1" for r in (web, gpu, docs)}

    window = MainWindow(fake, prompt_login=False, tray=False)
    window.show()
    args.out.mkdir(parents=True, exist_ok=True)

    def shot(name: str) -> None:
        window.resize(*SIZES[name])
        QTest.qWait(600)  # let tab / navigation animations finish
        path = args.out / name
        window.grab().save(str(path))
        print(path)

    shot("dashboard.png")

    # detail page, Workflow tab: label check and recent runs
    now = datetime.now(UTC)
    CI, RELEASE = ".github/workflows/ci.yml", ".github/workflows/release.yml"  # noqa: N806
    insights = RepoInsights(
        matches=(
            JobMatch(JobTarget(CI, "build", ("self-hosted", "windows")), MatchKind.OK),
            JobMatch(
                JobTarget(CI, "gpu-tests", ("self-hosted", "gpu")),
                MatchKind.MISSING, ("gpu",),
            ),
            JobMatch(JobTarget(RELEASE, "package", None, dynamic=True), MatchKind.UNKNOWN),
        ),
        runs=tuple(
            WorkflowRun(
                id=900 + i, run_number=120 - i, display_title=title, status="completed",
                conclusion=conclusion, head_branch="main", created_at=now - timedelta(hours=i * 3),
                html_url=f"https://github.com/octocat/web-app/actions/runs/{900 + i}",
            )
            for i, (title, conclusion) in enumerate(
                [("Bump version", "success"), ("Fix flaky test", "failure"), ("Docs", "success")]
            )
        ),
        fetched_at=now,
    )  # fmt: skip
    window.open_detail(web.id)
    window.detailPage.switch_tab(TAB_WORKFLOW)
    fake.repo_insights_loaded.emit(web.id, insights)
    shot("detail-workflow.png")

    # add wizard, step 2: a service-mode runner with its plan summary
    window.switchTo(window.addPage)
    step = window.addPage.configStep
    step.set_repo("octocat/ml-models")
    step.nameEdit.setText("gpu-box")
    step.labelsEdit.setText("gpu")
    step.serviceBox.setChecked(True)
    step.show_plan(
        make_plan(
            "octocat/ml-models", name="gpu-box", labels=("gpu",),
            target_dir=Path(r"D:\runners\ml-models"), mode=RunnerMode.SERVICE,
        )
    )  # fmt: skip
    window.addPage.stack.setCurrentIndex(STEP_CONFIG)
    shot("add-service.png")

    # Not window.close(): with "running" runners it would ask whether to stop them.
    return 0


if __name__ == "__main__":
    sys.exit(main())
