"""Tests for runnerdeck.__main__ (docs/m2-design.md §6).

The controller and the window are injected through `controller_factory` / `window_factory`, so
nothing here needs runnerdeck.app / runnerdeck.ui, keyring or the network. The normal start
(QLockFile, logging, app.exec_) is never exercised: every test patches QApplication.exec_ to
fail loudly if it is reached.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from PyQt5.QtCore import QObject
from PyQt5.QtWidgets import QApplication, QWidget

from runnerdeck import __main__ as entry
from runnerdeck import __version__
from runnerdeck.core import paths
from runnerdeck.core.errors import RunnerDeckError


class FakeController(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.shutdown_calls = 0

    def shutdown(self, timeout: float | None = None) -> list[str]:
        self.shutdown_calls += 1
        return []


class FakeWindow(QWidget):
    """Stands in for MainWindow: three page attributes and FluentWindow's switchTo."""

    def __init__(self, controller: Any) -> None:
        super().__init__()
        self.controller = controller
        self.dashboard_page = QWidget(self)
        self.dashboard_page.setObjectName("dashboard")
        self.add_page = QWidget(self)
        self.add_page.setObjectName("add")
        self.settings_page = QWidget(self)
        self.settings_page.setObjectName("settings")
        self.switched: list[str] = []

    def switchTo(self, page: QWidget) -> None:  # noqa: N802 - Qt naming
        self.switched.append(page.objectName())


@pytest.fixture(autouse=True)
def no_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """The normal path must never run under tests."""

    def boom(*_: Any) -> int:
        raise AssertionError("app.exec_() must not be called")

    monkeypatch.setattr(QApplication, "exec_", boom)
    monkeypatch.setattr(QApplication, "exec", boom)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Touch the variables main() writes so monkeypatch restores them afterwards."""
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "untouched"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")


# --- --version ---------------------------------------------------------------------------


def test_version_prints_name_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["--version"]) == 0
    out, err = capsys.readouterr()
    assert out == f"RunnerDeck {__version__}\n"
    assert err == ""


def test_version_does_not_import_app_or_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_: Any) -> Any:
        raise AssertionError("factories must not run for --version")

    assert entry.main(["--version"], controller_factory=fail, window_factory=fail) == 0


def test_version_survives_unusable_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class NullWriter:  # what PyInstaller installs in a windowed exe
        def write(self, _: str) -> None:
            raise OSError("no console")

        def flush(self) -> None:
            pass

    monkeypatch.setattr("sys.stdout", NullWriter())
    assert entry.main(["--version"]) == 0


# --- argument errors ------------------------------------------------------------------------


def test_unknown_argument_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["--bogus"]) == 2
    err = capsys.readouterr().err
    assert "usage:" in err
    assert "错误" in err


def test_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["--help"]) == 0
    assert "--smoke-test" in capsys.readouterr().out


# --- --smoke-test ---------------------------------------------------------------------------


def test_smoke_test_builds_window_and_switches_pages(
    qapp: QApplication, clean_env: None, tmp_path: Path
) -> None:
    created: dict[str, Any] = {}

    def controller_factory() -> FakeController:
        created["controller"] = FakeController()
        return created["controller"]

    def window_factory(controller: Any) -> FakeWindow:
        created["window"] = FakeWindow(controller)
        return created["window"]

    rc = entry.main(
        ["--smoke-test"], controller_factory=controller_factory, window_factory=window_factory
    )

    assert rc == 0
    window = created["window"]
    assert window.controller is created["controller"]
    assert window.switched == ["dashboard", "add", "settings"]
    assert created["controller"].shutdown_calls == 1
    assert os.environ["QT_QPA_PLATFORM"] == "offscreen"
    home = os.environ[paths.ENV_HOME]
    assert home != str(tmp_path / "untouched")
    assert "runnerdeck-smoke-" in home
    assert not Path(home).exists()  # temporary home removed afterwards


def test_smoke_test_sets_home_before_factories_run(qapp: QApplication, clean_env: None) -> None:
    seen: dict[str, str] = {}

    def controller_factory() -> FakeController:
        seen["home"] = os.environ[paths.ENV_HOME]
        seen["platform"] = os.environ["QT_QPA_PLATFORM"]
        return FakeController()

    rc = entry.main(
        ["--smoke-test"], controller_factory=controller_factory, window_factory=FakeWindow
    )
    assert rc == 0
    assert seen["platform"] == "offscreen"
    assert Path(seen["home"]).name.startswith("runnerdeck-smoke-")


def test_smoke_test_finds_pages_by_object_name(qapp: QApplication, clean_env: None) -> None:
    class WindowWithoutAttributes(QWidget):
        def __init__(self, controller: Any) -> None:
            super().__init__()
            self.switched: list[str] = []
            for name in ("settings", "add", "dashboard"):
                QWidget(self).setObjectName(name)

        def switchTo(self, page: QWidget) -> None:  # noqa: N802
            self.switched.append(page.objectName())

    windows: list[WindowWithoutAttributes] = []

    def window_factory(controller: Any) -> WindowWithoutAttributes:
        windows.append(WindowWithoutAttributes(controller))
        return windows[-1]

    rc = entry.main(
        ["--smoke-test"], controller_factory=FakeController, window_factory=window_factory
    )
    assert rc == 0
    assert windows[0].switched == ["dashboard", "add", "settings"]


def test_smoke_test_tolerates_window_without_switch_to(qapp: QApplication, clean_env: None) -> None:
    rc = entry.main(
        ["--smoke-test"], controller_factory=lambda: object(), window_factory=lambda c: QWidget()
    )
    assert rc == 0


def test_smoke_test_reports_failure_with_traceback(
    qapp: QApplication, clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    def window_factory(controller: Any) -> Any:
        raise RuntimeError("widget exploded")

    rc = entry.main(
        ["--smoke-test"], controller_factory=FakeController, window_factory=window_factory
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "widget exploded" in err


# --- uncaught exceptions -----------------------------------------------------------------


def test_format_uncaught_messages() -> None:
    assert entry._format_uncaught(RunnerDeckError("已经在运行")) == "已经在运行"
    text = entry._format_uncaught(ValueError("boom ghp_abcdefghijklmnopqrstuvwxyz0123456789"))
    assert text.startswith("内部错误：ValueError: boom")
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in text
    assert "详情见日志" in text


def test_excepthook_logs_and_shows_dialog(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    shown: list[tuple[str, str]] = []

    class FakeBox:
        def __init__(self, title: str, content: str, parent: Any = None) -> None:
            shown.append((title, content))
            self.cancelButton = QWidget()

        def exec(self) -> int:
            return 1

    import qfluentwidgets

    monkeypatch.setattr(qfluentwidgets, "MessageBox", FakeBox)
    original = entry.sys.excepthook
    try:
        entry._install_excepthook(Path("C:/logs/runnerdeck.log"))
        hook = entry.sys.excepthook
        assert hook is not original
        with caplog.at_level("CRITICAL", logger="runnerdeck"):
            try:
                raise ValueError("bad")
            except ValueError as e:
                hook(type(e), e, e.__traceback__)
    finally:
        entry.sys.excepthook = original
    assert shown and shown[0][0] == "RunnerDeck 出错了"
    assert "内部错误：ValueError: bad" in shown[0][1]
    assert "runnerdeck.log" in shown[0][1]
    assert any("uncaught exception" in r.message for r in caplog.records)
