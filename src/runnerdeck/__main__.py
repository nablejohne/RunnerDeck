"""GUI entry point (docs/m2-design.md §6).

    python -m runnerdeck                # the window
    python -m runnerdeck --version      # "RunnerDeck <version>", exit 0
    python -m runnerdeck --smoke-test   # offscreen: build controller + window, switch pages, exit 0

Also the script PyInstaller freezes into RunnerDeck.exe (RunnerDeck.spec) and the target of
the `runnerdeck` gui-script in pyproject. Qt, qfluentwidgets, the controller and the window
are imported lazily inside main() so that `--version` works without them and tests can
inject fakes through `controller_factory` / `window_factory`.
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
from types import TracebackType
from typing import Any

from runnerdeck import __version__
from runnerdeck.core import paths
from runnerdeck.core.errors import RunnerDeckError
from runnerdeck.core.redact import redact

PROG = "runnerdeck"
APP_NAME = "RunnerDeck"
LOCK_FILE_NAME = "runnerdeck.lock"
LOG_FILE_NAME = "runnerdeck.log"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
# Pages the smoke test switches to (docs/m2-design.md §5): attribute names tried on the
# window, and the objectNames to look for when no attribute matches.
SMOKE_PAGE_ATTRS = (
    ("dashboard_page", "dashboardPage", "dashboard"),
    ("add_page", "addPage", "add_wizard_page", "add"),
    ("settings_page", "settingsPage", "settings"),
)
SMOKE_PAGE_OBJECT_NAMES = ("dashboard", "add", "settings")

ControllerFactory = Callable[[], Any]
WindowFactory = Callable[[Any], Any]

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
        "--smoke-test",
        action="store_true",
        help="离屏创建主窗口并切换各页面后退出（打包产物自检）",
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


# --- default factories (lazy: lanes A / B) --------------------------------------------------


def _default_controller_factory() -> Any:
    from runnerdeck.app.controller import RunnerController
    from runnerdeck.core.process import Launch

    return RunnerController(launch=Launch.HELPER)


def _default_window_factory(controller: Any) -> Any:
    from runnerdeck.ui.main_window import MainWindow

    return MainWindow(controller)


def _smoke_window_factory(controller: Any) -> Any:
    # No modal login dialog: nobody is there to answer it, and exec() would never return.
    from runnerdeck.ui.main_window import MainWindow

    return MainWindow(controller, prompt_login=False)


# --- --smoke-test ---------------------------------------------------------------------------


def _smoke_pages(window: Any) -> list[Any]:
    """The three pages of the window, found defensively (attribute names, then objectName)."""
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


def _smoke_test(controller_factory: ControllerFactory, window_factory: WindowFactory) -> int:
    """Create everything offscreen under a throwaway RUNNERDECK_HOME; never calls exec()."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    home = tempfile.mkdtemp(prefix="runnerdeck-smoke-")
    os.environ[paths.ENV_HOME] = home
    try:
        from PyQt5.QtWidgets import QApplication

        from runnerdeck.ui.compat import disable_mac_native_frameless

        disable_mac_native_frameless()  # dev machines only; a no-op on Windows
        app = QApplication.instance() or QApplication([APP_NAME])
        controller = controller_factory()
        window = window_factory(controller)
        window.show()
        app.processEvents()
        switch_to = getattr(window, "switchTo", None)
        if switch_to is not None:
            for page in _smoke_pages(window):
                switch_to(page)
                app.processEvents()
        window.close()
        app.processEvents()
        shutdown = getattr(controller, "shutdown", None)
        if callable(shutdown):
            shutdown()
        return EXIT_OK
    except BaseException:  # noqa: BLE001 - report everything, exit 1 (§6)
        sys.stderr.write(traceback.format_exc())
        return EXIT_ERROR
    finally:
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
            from qfluentwidgets import MessageBox

            app = QApplication.instance()
            if app is None:
                return
            box = MessageBox(
                "RunnerDeck 出错了", f"{_format_uncaught(exc)}\n{log_file}", app.activeWindow()
            )
            box.cancelButton.hide()
            box.exec()
        except Exception:  # noqa: BLE001 - never raise from inside the hook
            log.exception("could not show the error dialog")

    sys.excepthook = excepthook


def _run_gui(controller_factory: ControllerFactory, window_factory: WindowFactory) -> int:
    from runnerdeck.core import logs

    log_file = logs.setup_logging(file_name=LOG_FILE_NAME, level=logging.INFO)
    log.info("%s %s starting", APP_NAME, __version__)

    from PyQt5.QtCore import QLockFile, Qt
    from PyQt5.QtWidgets import QApplication
    from qfluentwidgets import MessageBox, Theme, setTheme

    # High-DPI attributes must be set before the QApplication exists.
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    app = QApplication([APP_NAME])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(True)
    _install_excepthook(log_file)

    lock_dir = paths.local_dir()
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_dir / LOCK_FILE_NAME))
    if not lock.tryLock(0):
        log.info("another instance holds %s; exiting", lock.fileName())
        box = MessageBox("RunnerDeck", "RunnerDeck 已经在运行。", None)
        box.cancelButton.hide()
        box.exec()
        return EXIT_OK

    try:
        setTheme(Theme.AUTO)
        controller = controller_factory()
        window = window_factory(controller)
        window.show()
        code = int(app.exec_())
        log.info("%s exited with code %s", APP_NAME, code)
        return code
    finally:
        lock.unlock()


# --- main -----------------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    controller_factory: ControllerFactory | None = None,
    window_factory: WindowFactory | None = None,
) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(args_list)
    except SystemExit as e:  # argparse: --help (0) or bad arguments (2)
        return e.code if isinstance(e.code, int) else EXIT_USAGE

    if args.version:
        return _print_version()

    controller_factory = controller_factory or _default_controller_factory
    if args.smoke_test:
        window_factory = window_factory or _smoke_window_factory
    window_factory = window_factory or _default_window_factory
    if args.smoke_test:
        return _smoke_test(controller_factory, window_factory)
    return _run_gui(controller_factory, window_factory)


if __name__ == "__main__":
    sys.exit(main())
