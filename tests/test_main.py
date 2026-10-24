"""Tests for runnerdeck.__main__ (docs/m2-design.md §6, docs/m3-design.md §6.2 / §6.4).

The controller, window, single-instance guard and poller are injected through the `*_factory`
arguments, so nothing here needs the controller, the poller or the ui package, keyring or
the network. `_run_gui` (logging, the real QApplication) is never run: QApplication.exec_ is
patched to fail loudly, and the normal start after the QApplication (`_run_app`) is driven
with a FakeApp whose exec_ returns at once.
"""

from __future__ import annotations

import os
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PyQt5.QtCore import QObject, pyqtSignal
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


class FakeDetailPage(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("detail")
        self.shown: list[str | None] = []

    def show_record(self, record_id: str | None) -> None:
        self.shown.append(record_id)


class FakeTray:
    def __init__(self) -> None:
        self.hidden = False

    def hide(self) -> None:
        self.hidden = True


class FakeWindow(QWidget):
    """Stands in for MainWindow: page attributes, a tray and FluentWindow's switchTo."""

    def __init__(self, controller: Any) -> None:
        super().__init__()
        self.controller = controller
        self.dashboard_page = QWidget(self)
        self.dashboard_page.setObjectName("dashboard")
        self.add_page = QWidget(self)
        self.add_page.setObjectName("add")
        self.settings_page = QWidget(self)
        self.settings_page.setObjectName("settings")
        self.detailPage = FakeDetailPage(self)
        self.tray = FakeTray()
        self.switched: list[str] = []

    def switchTo(self, page: QWidget) -> None:  # noqa: N802 - Qt naming
        self.switched.append(page.objectName())


class FakeInstance(QObject):
    """Stands in for SingleInstance; records calls into a shared list."""

    message_received = pyqtSignal(str)

    def __init__(
        self, *, primary: bool = True, delivered: bool = True, calls: list[str] | None = None
    ) -> None:
        super().__init__()
        self.primary = primary
        self.delivered = delivered
        self.calls = [] if calls is None else calls
        self.sent: list[str] = []

    def acquire(self) -> bool:
        self.calls.append("acquire")
        return self.primary

    def send_to_primary(self, message: str = "show", timeout_ms: int = 2000) -> bool:
        self.calls.append(f"send:{message}")
        self.sent.append(message)
        return self.delivered

    def release(self) -> None:
        self.calls.append("release")


class FakePoller:
    def __init__(self, controller: Any, calls: list[str]) -> None:
        self.controller = controller
        self.calls = calls

    def start(self) -> None:
        self.calls.append("poller.start")

    def stop(self) -> None:
        self.calls.append("poller.stop")


class FakeApp(QObject):
    """The QApplication surface _run_app uses; exec_ runs `during_exec` and returns."""

    commitDataRequest = pyqtSignal(object)  # noqa: N815 - Qt naming
    aboutToQuit = pyqtSignal()  # noqa: N815 - Qt naming

    def __init__(self, calls: list[str], during_exec: Callable[[], None] | None = None) -> None:
        super().__init__()
        self.calls = calls
        self.during_exec = during_exec
        self.quit_on_last_window_closed: bool | None = None
        self.native_filters: list[object] = []

    def setQuitOnLastWindowClosed(self, value: bool) -> None:  # noqa: N802 - Qt naming
        self.quit_on_last_window_closed = value

    def installNativeEventFilter(self, event_filter: object) -> None:  # noqa: N802 - Qt naming
        self.native_filters.append(event_filter)  # Windows only (app/session_end.install)

    def exec_(self) -> int:
        self.calls.append("exec")
        if self.during_exec is not None:
            self.during_exec()
        return 3


class StartupController(FakeController):
    """FakeController plus the M3 startup methods and the settings _run_app reads."""

    def __init__(self, calls: list[str], **settings: Any) -> None:
        super().__init__()
        from runnerdeck.core.models import AppConfig, AppSettings

        self.calls = calls
        self.config = AppConfig(settings=AppSettings(**settings))
        self.running: list[str] = []
        calls.append("controller")

    def reconcile_autostart(self) -> None:
        self.calls.append("reconcile_autostart")

    def discover(self) -> None:
        self.calls.append("discover")

    def start_auto_runners(self) -> None:
        self.calls.append("start_auto_runners")

    def running_ids(self) -> list[str]:
        return list(self.running)

    def shutdown(self, timeout: float | None = None) -> list[str]:
        self.calls.append("shutdown")
        return super().shutdown(timeout)


class StartupWindow:
    def __init__(self, controller: Any, calls: list[str], *, tray: bool) -> None:
        self.controller = controller
        self.calls = calls
        self.tray = object() if tray else None
        self.session_end_error: BaseException | None = None
        calls.append("window")

    def show(self) -> None:
        self.calls.append("show")

    def show_and_raise(self) -> None:
        self.calls.append("show_and_raise")

    def on_session_end(self) -> None:
        self.calls.append("on_session_end")
        if self.session_end_error is not None:
            raise self.session_end_error


def fail_factory(*_: Any) -> Any:
    raise AssertionError("this factory must not be called")


def install_fake_module(
    monkeypatch: pytest.MonkeyPatch, module_name: str, attr: str
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Put a stand-in module in sys.modules whose `attr` records how it was called.

    The default factories import other lanes' modules lazily; this checks what they ask for
    without importing (or depending on) those modules.
    """
    made: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def record(*args: Any, **kwargs: Any) -> object:
        made.append((args, kwargs))
        return object()

    module = types.ModuleType(module_name)
    setattr(module, attr, record)
    monkeypatch.setitem(sys.modules, module_name, module)
    return made


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


# --- --autostart ---------------------------------------------------------------------------


def test_autostart_flag_parses() -> None:
    parser = entry.build_parser()
    assert parser.parse_args(["--autostart"]).autostart is True
    assert parser.parse_args([]).autostart is False


def test_autostart_routes_to_the_gui_with_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[bool, Any]] = []

    def fake_run_gui(*, autostart: bool, factories: Any) -> int:
        seen.append((autostart, factories))
        return 7

    monkeypatch.setattr(entry, "_run_gui", fake_run_gui)
    controller_factory, window_factory = FakeController, FakeWindow

    def instance_factory() -> FakeInstance:
        return FakeInstance()

    def poller_factory(controller: Any) -> FakePoller:
        return FakePoller(controller, [])

    assert (
        entry.main(
            ["--autostart"],
            controller_factory=controller_factory,
            window_factory=window_factory,
            instance_factory=instance_factory,
            poller_factory=poller_factory,
        )
        == 7
    )
    assert entry.main([]) == 7
    (autostart, factories), (plain, defaults) = seen
    assert autostart is True and plain is False
    assert factories.controller is controller_factory
    assert factories.window is window_factory
    assert factories.instance is instance_factory
    assert factories.poller is poller_factory
    assert defaults.window is entry._default_window_factory
    assert defaults.instance is entry._default_instance_factory
    assert defaults.poller is entry._default_poller_factory


# --- argument errors ------------------------------------------------------------------------


def test_unknown_argument_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["--bogus"]) == 2
    err = capsys.readouterr().err
    assert "usage:" in err
    assert "错误" in err


def test_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "--smoke-test" in out
    assert "--autostart" in out


# --- --smoke-test ---------------------------------------------------------------------------


def test_smoke_test_builds_window_and_switches_pages(
    qapp: QApplication, clean_env: None, tmp_path: Path
) -> None:
    created: dict[str, Any] = {}
    calls: list[str] = []

    def controller_factory() -> FakeController:
        calls.append("controller")
        created["controller"] = FakeController()
        return created["controller"]

    def window_factory(controller: Any) -> FakeWindow:
        created["window"] = FakeWindow(controller)
        return created["window"]

    rc = entry.main(
        ["--smoke-test"],
        controller_factory=controller_factory,
        window_factory=window_factory,
        instance_factory=lambda: FakeInstance(calls=calls),
        poller_factory=fail_factory,
    )

    assert rc == 0
    window = created["window"]
    assert window.controller is created["controller"]
    assert window.switched == ["dashboard", "add", "settings", "detail"]
    assert window.detailPage.shown == [None]  # the empty state, no records
    assert window.tray.hidden
    assert created["controller"].shutdown_calls == 1
    assert calls == ["acquire", "controller", "release"]
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


def test_smoke_test_uses_the_real_single_instance_in_the_temporary_home(
    qapp: QApplication, clean_env: None
) -> None:
    # No instance_factory: the real lock + local server, released before the home is removed.
    rc = entry.main(["--smoke-test"], controller_factory=FakeController, window_factory=FakeWindow)
    assert rc == 0


def test_smoke_test_fails_when_the_lock_cannot_be_taken(
    qapp: QApplication, clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = entry.main(
        ["--smoke-test"],
        controller_factory=fail_factory,
        window_factory=fail_factory,
        instance_factory=lambda: FakeInstance(primary=False),
    )
    assert rc == 1
    assert "single instance" in capsys.readouterr().err


def test_smoke_window_factory_builds_the_tray_without_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = install_fake_module(monkeypatch, "runnerdeck.ui.main_window", "MainWindow")
    controller = object()
    entry._smoke_window_factory(controller)
    entry._default_window_factory(controller)
    assert made == [
        ((controller,), {"prompt_login": False, "tray": True}),
        ((controller,), {}),
    ]


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
    monkeypatch.setattr(
        entry, "_show_message", lambda title, text, parent=None: shown.append((title, text))
    )
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


def test_show_message_without_a_visible_window_uses_qmessagebox(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    # qfluentwidgets' MessageBox needs a parent window (MaskDialogBase calls parent.width()).
    from PyQt5.QtWidgets import QMessageBox

    native: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda _p, title, text: native.append((title, text)))
    )
    import qfluentwidgets

    monkeypatch.setattr(qfluentwidgets, "MessageBox", lambda *a, **k: pytest.fail("fluent box"))
    entry._show_message("RunnerDeck", "已经在运行")
    hidden = QWidget()  # exists but is not shown, like a window closed to the tray
    entry._show_message("RunnerDeck", "出错了", hidden)
    assert native == [("RunnerDeck", "已经在运行"), ("RunnerDeck", "出错了")]


# --- startup decisions ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("autostart", "start_minimized", "has_tray", "shown"),
    [
        (False, False, True, True),
        (True, False, True, False),
        (False, True, True, False),
        (True, True, True, False),
        # Without a tray the window is the only way in.
        (False, False, False, True),
        (True, False, False, True),
        (False, True, False, True),
        (True, True, False, True),
    ],
)
def test_should_show_window(
    autostart: bool, start_minimized: bool, has_tray: bool, shown: bool
) -> None:
    assert (
        entry.should_show_window(
            autostart=autostart, start_minimized=start_minimized, has_tray=has_tray
        )
        is shown
    )


def test_message_for_primary() -> None:
    from runnerdeck.app.single_instance import SHOW_MESSAGE

    assert entry.message_for_primary(autostart=False) == SHOW_MESSAGE == "show"
    assert entry.message_for_primary(autostart=True) is None


# --- the normal start after the QApplication (_run_app) ----------------------------------------


def run_app(
    calls: list[str],
    *,
    autostart: bool = False,
    tray: bool = True,
    instance: FakeInstance | None = None,
    during_exec: Callable[[dict[str, Any]], None] | None = None,
    **settings: Any,
) -> tuple[int, dict[str, Any]]:
    made: dict[str, Any] = {}
    instance = instance or FakeInstance(calls=calls)
    made["instance"] = instance

    def controller_factory() -> StartupController:
        made["controller"] = StartupController(calls, **settings)
        return made["controller"]

    def window_factory(controller: Any) -> StartupWindow:
        made["window"] = StartupWindow(controller, calls, tray=tray)
        return made["window"]

    def poller_factory(controller: Any) -> FakePoller:
        assert controller is made["controller"]
        calls.append("poller")
        return FakePoller(controller, calls)

    app = FakeApp(calls, (lambda: during_exec(made)) if during_exec else None)
    made["app"] = app
    factories = entry._Factories(
        controller=controller_factory,
        window=window_factory,
        instance=lambda: instance,
        poller=poller_factory,
        apply_theme=lambda name: calls.append(f"theme:{name}"),
    )
    code = entry._run_app(app, autostart=autostart, factories=factories)
    return code, made


def test_run_app_startup_order(qapp: QApplication) -> None:
    calls: list[str] = []
    code, made = run_app(calls, theme="dark")
    assert code == 3
    assert calls == [
        "acquire",
        "controller",
        "theme:dark",
        "window",
        "poller",
        "poller.start",
        "reconcile_autostart",
        "discover",
        "start_auto_runners",
        "show",
        "exec",
        "poller.stop",
        "release",
    ]
    assert made["app"].quit_on_last_window_closed is False  # the tray keeps the app alive


@pytest.mark.parametrize(
    ("autostart", "start_minimized"), [(True, False), (False, True), (True, True)]
)
def test_run_app_starts_in_the_tray(
    qapp: QApplication, autostart: bool, start_minimized: bool
) -> None:
    calls: list[str] = []
    run_app(calls, autostart=autostart, start_minimized=start_minimized)
    assert "show" not in calls
    assert calls.index("start_auto_runners") < calls.index("exec")


def test_run_app_without_tray_always_shows_the_window(qapp: QApplication) -> None:
    calls: list[str] = []
    _, made = run_app(calls, autostart=True, start_minimized=True, tray=False)
    assert "show" in calls
    assert made["app"].quit_on_last_window_closed is True


def test_second_instance_asks_the_primary_to_show(qapp: QApplication) -> None:
    calls: list[str] = []
    code, _ = run_app(calls, instance=FakeInstance(primary=False, calls=calls))
    assert code == 0
    assert calls == ["acquire", "send:show"]  # no controller, no window, no exec


def test_second_instance_with_autostart_exits_quietly(qapp: QApplication) -> None:
    calls: list[str] = []
    code, _ = run_app(calls, autostart=True, instance=FakeInstance(primary=False, calls=calls))
    assert code == 0
    assert calls == ["acquire"]


def test_second_instance_tells_the_user_when_the_primary_does_not_answer(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(entry, "_show_already_running", lambda: calls.append("already_running"))
    instance = FakeInstance(primary=False, delivered=False, calls=calls)
    assert run_app(calls, instance=instance)[0] == 0
    assert calls == ["acquire", "send:show", "already_running"]


def test_primary_shows_the_window_when_a_second_instance_starts(qapp: QApplication) -> None:
    calls: list[str] = []

    def during_exec(made: dict[str, Any]) -> None:
        made["instance"].message_received.emit("show")
        made["instance"].message_received.emit("bogus")  # ignored

    run_app(calls, autostart=True, during_exec=during_exec)
    assert calls.count("show_and_raise") == 1
    assert "show" not in calls  # started in the tray; only the message brought it up


def test_quit_safety_net_stops_running_runners(qapp: QApplication) -> None:
    calls: list[str] = []

    def during_exec(made: dict[str, Any]) -> None:
        made["controller"].running = ["r1"]
        made["app"].aboutToQuit.emit()

    run_app(calls, during_exec=during_exec)
    assert calls.count("shutdown") == 1
    assert calls.index("shutdown") < calls.index("poller.stop")


def test_quit_safety_net_without_running_runners_does_nothing(qapp: QApplication) -> None:
    calls: list[str] = []
    run_app(calls, during_exec=lambda made: made["app"].aboutToQuit.emit())
    assert "shutdown" not in calls


def test_session_end_signals_runners_and_does_not_wait(qapp: QApplication) -> None:
    calls: list[str] = []

    def during_exec(made: dict[str, Any]) -> None:
        made["controller"].running = ["r1"]
        made["app"].commitDataRequest.emit(object())  # the QSessionManager
        made["app"].aboutToQuit.emit()  # WM_ENDSESSION: must not block on shutdown()

    run_app(calls, during_exec=during_exec)
    assert calls.count("on_session_end") == 1
    assert "shutdown" not in calls


def test_session_end_never_raises(qapp: QApplication) -> None:
    calls: list[str] = []

    def during_exec(made: dict[str, Any]) -> None:
        made["window"].session_end_error = RuntimeError("boom")
        made["app"].commitDataRequest.emit(object())

    run_app(calls, during_exec=during_exec)  # the exception is logged, not raised
    assert "on_session_end" in calls


def test_commit_data_request_is_connected_directly() -> None:
    from PyQt5.QtCore import Qt

    connections: list[tuple[str, Any]] = []

    class RecordingApp:
        class _Signal:
            def __init__(self, name: str) -> None:
                self.name = name

            def connect(self, slot: Any, kind: Any = None) -> None:
                connections.append((self.name, kind))

        def __init__(self) -> None:
            self.commitDataRequest = self._Signal("commitDataRequest")
            self.aboutToQuit = self._Signal("aboutToQuit")
            self.quit_on_last: bool | None = None

        def setQuitOnLastWindowClosed(self, value: bool) -> None:  # noqa: N802
            self.quit_on_last = value

    lifecycle = entry._Lifecycle(controller=object(), window=object())
    app = RecordingApp()
    entry._connect_app(app, lifecycle, has_tray=False)
    assert ("commitDataRequest", Qt.DirectConnection) in connections
    assert ("aboutToQuit", None) in connections
    assert app.quit_on_last is True


def test_startup_failure_still_releases_the_instance(qapp: QApplication) -> None:
    calls: list[str] = []
    instance = FakeInstance(calls=calls)

    def broken_window(controller: Any) -> Any:
        raise RuntimeError("no window")

    factories = entry._Factories(
        controller=lambda: StartupController(calls),
        window=broken_window,
        instance=lambda: instance,
        poller=fail_factory,
        apply_theme=lambda name: None,
    )
    with pytest.raises(RuntimeError, match="no window"):
        entry._run_app(FakeApp(calls), autostart=False, factories=factories)
    assert calls[-1] == "release"
    assert "exec" not in calls


# --- default factories ------------------------------------------------------------------------


def test_default_poller_and_theme_factories(monkeypatch: pytest.MonkeyPatch) -> None:
    pollers = install_fake_module(monkeypatch, "runnerdeck.app.poller", "StatusPoller")
    themes = install_fake_module(monkeypatch, "runnerdeck.ui.theme", "apply_theme")
    controller = object()
    entry._default_poller_factory(controller)
    entry._default_apply_theme("light")
    assert pollers == [((controller,), {})]
    assert themes == [(("light",), {})]


def test_default_instance_factory_uses_the_data_directory(
    qapp: QApplication, clean_env: None
) -> None:
    from runnerdeck.app.single_instance import SingleInstance, server_name

    instance = entry._default_instance_factory()
    assert isinstance(instance, SingleInstance)
    assert instance.name == server_name(paths.local_dir())


def test_lifecycle_resumes_after_a_cancelled_logoff() -> None:
    calls: list[Any] = []

    class Controller:
        def resume_after_cancelled_logoff(self) -> int:
            calls.append("resume")
            return 2

    class Window:
        def on_session_end(self) -> None:
            calls.append("end")

        def on_session_resumed(self, count: int) -> None:
            calls.append(("resumed", count))

    lifecycle = entry._Lifecycle(controller=Controller(), window=Window())
    lifecycle.on_session_cancelled()  # no logoff in progress: ignored
    assert calls == []
    lifecycle.on_commit_data()
    lifecycle.on_session_cancelled()
    lifecycle.on_session_cancelled()  # the message reaches every top-level window
    assert calls == ["end", "resume", ("resumed", 2)]
    assert not lifecycle.session_ending
