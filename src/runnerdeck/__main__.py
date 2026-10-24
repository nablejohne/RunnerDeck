"""GUI entry point (docs/m2-design.md §6, docs/m3-design.md §6.2).

    python -m runnerdeck                # the window (or only the tray, see should_show_window)
    python -m runnerdeck --autostart    # what the HKCU Run key starts: tray only, no window
    python -m runnerdeck --version      # "RunnerDeck <version>", exit 0
    python -m runnerdeck --smoke-test   # offscreen: build controller + window + tray, switch
                                        # pages, exit 0

Also the script PyInstaller freezes into RunnerDeck.exe (RunnerDeck.spec) and the target of
the `runnerdeck` gui-script in pyproject. Qt, qfluentwidgets, the controller, the window, the
poller and the single-instance guard are imported lazily inside functions so that `--version`
works without them and tests can inject fakes through the `*_factory` arguments of main().
The decisions of the normal start (show the window or not, what to tell a running instance)
are small functions with their own tests; `_run_gui` only builds the QApplication around
`_run_app`, which tests drive with a fake application object.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from runnerdeck import __version__
from runnerdeck.core import paths
from runnerdeck.core.errors import RunnerDeckError
from runnerdeck.core.redact import redact

PROG = "runnerdeck"
APP_NAME = "RunnerDeck"
LOG_FILE_NAME = "runnerdeck.log"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
# Pages the smoke test switches to (docs/m2-design.md §5, m3-design §6.2): attribute names
# tried on the window, and the objectNames to look for when no attribute matches.
SMOKE_PAGE_ATTRS = (
    ("dashboard_page", "dashboardPage", "dashboard"),
    ("add_page", "addPage", "add_wizard_page", "add"),
    ("settings_page", "settingsPage", "settings"),
    ("detail_page", "detailPage", "detail"),
)
SMOKE_PAGE_OBJECT_NAMES = ("dashboard", "add", "settings", "detail")
ALREADY_RUNNING_TEXT = (
    "RunnerDeck 已经在运行，但没能切换到它的窗口。请在任务栏右下角的托盘图标里打开它。"
)

ControllerFactory = Callable[[], Any]
WindowFactory = Callable[[Any], Any]
InstanceFactory = Callable[[], Any]
PollerFactory = Callable[[Any], Any]
ThemeApplier = Callable[[str], None]

log = logging.getLogger(__name__)


# --- argument parsing --------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """argparse with Chinese error text; exit code 2 on bad arguments (like the CLI)."""

    def error(self, message: str) -> Any:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}：错误：{message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG, description="RunnerDeck：管理 GitHub Actions self-hosted runner")
    parser.add_argument("--version", action="store_true", help="显示版本并退出")
    parser.add_argument(
        "--autostart",
        action="store_true",
        help="开机自启时使用：只在托盘运行，不显示主窗口",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="离屏创建主窗口和托盘并切换各页面后退出（打包产物自检）",
    )
    return parser


# --- --version ------------------------------------------------------------------------------


def _stdout_usable() -> bool:
    """False for the windowed exe (PyInstaller installs a NullWriter) or a missing stdout."""
    try:
        sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    return True


def _attach_parent_console() -> None:
    """Windows, windowed subsystem: best effort to write to the console that started us."""
    if sys.platform != "win32" or _stdout_usable():
        return
    try:
        import ctypes

        attach_parent_process = -1
        if not ctypes.windll.kernel32.AttachConsole(attach_parent_process):  # type: ignore[attr-defined]
            return
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
    except Exception:  # noqa: BLE001 - purely cosmetic; never fail --version over it
        return


def _print_version() -> int:
    _attach_parent_console()
    try:
        print(f"{APP_NAME} {__version__}")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - no console at all: still exit 0 (§6)
        pass
    return EXIT_OK


# --- default factories (lazy: lanes B / C and single_instance) -----------------------------


def _default_controller_factory() -> Any:
    from runnerdeck.app.controller import RunnerController
    from runnerdeck.core.process import Launch

    return RunnerController(launch=Launch.HELPER)


def _default_window_factory(controller: Any) -> Any:
    # tray=None: MainWindow decides by QSystemTrayIcon.isSystemTrayAvailable().
    from runnerdeck.ui.main_window import MainWindow

    return MainWindow(controller)


def _smoke_window_factory(controller: Any) -> Any:
    # No modal login dialog: nobody is there to answer it, and exec() would never return.
    # tray=True: offscreen has no system tray, but the smoke test must build the TrayIcon.
    from runnerdeck.ui.main_window import MainWindow

    return MainWindow(controller, prompt_login=False, tray=True)


def _default_instance_factory() -> Any:
    from runnerdeck.app.single_instance import SingleInstance

    return SingleInstance()


def _default_poller_factory(controller: Any) -> Any:
    from runnerdeck.app.poller import StatusPoller

    return StatusPoller(controller)


def _default_apply_theme(name: str) -> None:
    from runnerdeck.ui.theme import apply_theme

    apply_theme(name)


@dataclass(frozen=True)
class _Factories:
    controller: ControllerFactory
    window: WindowFactory
    instance: InstanceFactory
    poller: PollerFactory
    apply_theme: ThemeApplier = _default_apply_theme


# --- startup decisions (pure) -------------------------------------------------------------


def should_show_window(*, autostart: bool, start_minimized: bool, has_tray: bool) -> bool:
    """Show the main window at startup? (§6.2)

    Without a tray the window is the only way in, so it is always shown. With one, the Run
    key's `--autostart` and the start_minimized setting both mean tray only.
    """
    if not has_tray:
        return True
    return not (autostart or start_minimized)


def message_for_primary(*, autostart: bool) -> str | None:
    """What a second instance tells the running one; None = nothing, just exit.

    `--autostart` did not come from the user (the Run key fired while RunnerDeck was already
    running, e.g. started by hand right after logging in), so it must not pop the window up.
    """
    if autostart:
        return None
    from runnerdeck.app.single_instance import SHOW_MESSAGE

    return SHOW_MESSAGE


def _hand_off(instance: Any, *, autostart: bool) -> bool:
    """Second instance: tell the primary. False only when it should have heard us but did not."""
    message = message_for_primary(autostart=autostart)
    if message is None:
        log.info("another instance is running; --autostart exits quietly")
        return True
    delivered = bool(instance.send_to_primary(message))
    if delivered:
        log.info("another instance is running; asked it to show its window")
    else:
        log.warning("another instance holds the lock but did not answer")
    return delivered


def _has_tray(window: Any) -> bool:
    return getattr(window, "tray", None) is not None


# --- application lifecycle hooks ---------------------------------------------------------------


class _Lifecycle:
    """Slots the QApplication and the single-instance server are connected to (§6.2).

    session_ending is set on commitDataRequest (Windows: WM_QUERYENDSESSION). Qt then emits
    aboutToQuit from WM_ENDSESSION right before Windows ends the process; the runners were
    already told to stop, and waiting for them there would hold up the logoff.
    """

    def __init__(self, controller: Any, window: Any) -> None:
        self._controller = controller
        self._window = window
        self.session_ending = False

    def on_commit_data(self, _manager: object = None) -> None:
        """Logoff / shutdown: signal every runner to stop, without waiting. Never raises.

        Connected with Qt.DirectConnection; an exception here would surface as a modal error
        box in the middle of the logoff.
        """
        self.session_ending = True
        try:
            self._window.on_session_end()
        except Exception:  # noqa: BLE001 - see docstring
            log.exception("on_session_end failed")

    def on_about_to_quit(self) -> None:
        """Safety net: quitting by any route other than quit_app() still stops the runners."""
        if self.session_ending:
            log.info("session is ending; runners were signalled to stop, not waiting for them")
            return
        try:
            running = list(self._controller.running_ids())
            if running:
                log.warning("quitting with %d runner(s) still running; stopping them", len(running))
                self._controller.shutdown()
        except Exception:  # noqa: BLE001 - the app is going away; just record it
            log.exception("stopping runners on quit failed")

    def on_message(self, message: str) -> None:
        """A second instance was started: bring the window up."""
        from runnerdeck.app.single_instance import SHOW_MESSAGE

        if message == SHOW_MESSAGE:
            self._window.show_and_raise()
        else:
            log.warning("single instance: ignoring unknown message %r", message)


def _connect_app(app: Any, lifecycle: _Lifecycle, *, has_tray: bool) -> None:
    from PyQt5.QtCore import Qt

    # With a tray, hiding (closing to tray) the last window must not end the app.
    app.setQuitOnLastWindowClosed(not has_tray)
    # Direct: the session manager needs the answer before the handler returns.
    app.commitDataRequest.connect(lifecycle.on_commit_data, Qt.DirectConnection)
    app.aboutToQuit.connect(lifecycle.on_about_to_quit)


def _show_message(title: str, text: str, parent: Any = None) -> None:
    """One-button message box. qfluentwidgets' MessageBox is an overlay sized to its parent
    (MaskDialogBase calls parent.width()), so without a visible window use QMessageBox."""
    if parent is not None and parent.isVisible():
        from qfluentwidgets import MessageBox

        box = MessageBox(title, text, parent)
        box.cancelButton.hide()
        box.exec()
        return
    from PyQt5.QtWidgets import QMessageBox

    QMessageBox.warning(None, title, text)


def _show_already_running() -> None:
    _show_message(APP_NAME, ALREADY_RUNNING_TEXT)


def _run_app(app: Any, *, autostart: bool, factories: _Factories) -> int:
    """Everything after the QApplication exists (§6.2 startup order); returns the exit code."""
    instance = factories.instance()
    if not instance.acquire():
        if not _hand_off(instance, autostart=autostart):
            _show_already_running()
        return EXIT_OK

    poller: Any = None
    try:
        controller = factories.controller()
        settings = controller.config.settings
        factories.apply_theme(settings.theme)
        window = factories.window(controller)
        has_tray = _has_tray(window)
        lifecycle = _Lifecycle(controller, window)
        _connect_app(app, lifecycle, has_tray=has_tray)
        instance.message_received.connect(lifecycle.on_message)

        poller = factories.poller(controller)
        poller.start()
        controller.reconcile_autostart()
        controller.discover()
        controller.start_auto_runners()

        if should_show_window(
            autostart=autostart, start_minimized=settings.start_minimized, has_tray=has_tray
        ):
            window.show()
        else:
            log.info("starting in the tray (autostart=%s)", autostart)
        code = int(app.exec_())
        log.info("%s exited with code %s", APP_NAME, code)
        return code
    finally:
        if poller is not None:
            poller.stop()
        instance.release()


# --- --smoke-test ---------------------------------------------------------------------------


def _smoke_pages(window: Any) -> list[Any]:
    """The pages of the window, found defensively (attribute names, then objectName)."""
    pages: list[Any] = []
    for names in SMOKE_PAGE_ATTRS:
        for name in names:
            page = getattr(window, name, None)
            if page is not None and not callable(page):
                pages.append(page)
                break
    if pages:
        return pages
    from PyQt5.QtWidgets import QWidget

    find_child = getattr(window, "findChild", None)
    if find_child is None:
        return []
    for object_name in SMOKE_PAGE_OBJECT_NAMES:
        page = find_child(QWidget, object_name)
        if page is not None:
            pages.append(page)
    return pages


def _smoke_test(factories: _Factories) -> int:
    """Create everything offscreen under a throwaway RUNNERDECK_HOME; never calls exec()."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    home = tempfile.mkdtemp(prefix="runnerdeck-smoke-")
    os.environ[paths.ENV_HOME] = home
    instance: Any = None
    try:
        from PyQt5.QtWidgets import QApplication

        from runnerdeck.ui.compat import disable_mac_native_frameless

        disable_mac_native_frameless()  # dev machines only; a no-op on Windows
        app = QApplication.instance() or QApplication([APP_NAME])
        # Lock + local server (QtNetwork) under the throwaway home: proves the bundle has them.
        instance = factories.instance()
        if not instance.acquire():
            raise RuntimeError("single instance: could not acquire the lock in a fresh home")
        controller = factories.controller()
        window = factories.window(controller)
        window.show()
        app.processEvents()
        switch_to = getattr(window, "switchTo", None)
        if switch_to is not None:
            for page in _smoke_pages(window):
                show_record = getattr(page, "show_record", None)
                if callable(show_record):
                    show_record(None)  # detail page: the empty state
                switch_to(page)
                app.processEvents()
        window.close()
        app.processEvents()
        tray = getattr(window, "tray", None)
        if tray is not None:
            tray.hide()
        shutdown = getattr(controller, "shutdown", None)
        if callable(shutdown):
            shutdown()
        return EXIT_OK
    except BaseException:  # noqa: BLE001 - report everything, exit 1 (§6)
        sys.stderr.write(traceback.format_exc())
        return EXIT_ERROR
    finally:
        if instance is not None:
            instance.release()
        shutil.rmtree(home, ignore_errors=True)


# --- normal start ---------------------------------------------------------------------------


def _format_uncaught(exc: BaseException) -> str:
    if isinstance(exc, RunnerDeckError):
        return str(exc)
    return f"内部错误：{type(exc).__name__}: {redact(str(exc))}，详情见日志"


def _install_excepthook(log_file: Any) -> None:
    def excepthook(
        exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:
        log.critical("uncaught exception", exc_info=(exc_type, exc, tb))
        try:
            from PyQt5.QtWidgets import QApplication

            app = QApplication.instance()
            if app is None:
                return
            _show_message(
                "RunnerDeck 出错了", f"{_format_uncaught(exc)}\n{log_file}", app.activeWindow()
            )
        except Exception:  # noqa: BLE001 - never raise from inside the hook
            log.exception("could not show the error dialog")

    sys.excepthook = excepthook


def _run_gui(*, autostart: bool, factories: _Factories) -> int:
    from runnerdeck.core import logs

    log_file = logs.setup_logging(file_name=LOG_FILE_NAME, level=logging.INFO)
    log.info("%s %s starting%s", APP_NAME, __version__, " (--autostart)" if autostart else "")

    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QGuiApplication
    from PyQt5.QtWidgets import QApplication

    # Both must be set before the QApplication exists. Without fallback session management
    # Qt does not close every window on logoff (which would run closeEvent -> quit_app and
    # its confirmation box); commitDataRequest -> on_session_end handles the logoff instead.
    QGuiApplication.setFallbackSessionManagementEnabled(False)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    app = QApplication([APP_NAME])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    _install_excepthook(log_file)
    return _run_app(app, autostart=autostart, factories=factories)


# --- main -----------------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    controller_factory: ControllerFactory | None = None,
    window_factory: WindowFactory | None = None,
    instance_factory: InstanceFactory | None = None,
    poller_factory: PollerFactory | None = None,
) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(args_list)
    except SystemExit as e:  # argparse: --help (0) or bad arguments (2)
        return e.code if isinstance(e.code, int) else EXIT_USAGE

    if args.version:
        return _print_version()

    default_window = _smoke_window_factory if args.smoke_test else _default_window_factory
    factories = _Factories(
        controller=controller_factory or _default_controller_factory,
        window=window_factory or default_window,
        instance=instance_factory or _default_instance_factory,
        poller=poller_factory or _default_poller_factory,
    )
    if args.smoke_test:
        return _smoke_test(factories)
    return _run_gui(autostart=args.autostart, factories=factories)


if __name__ == "__main__":
    sys.exit(main())
