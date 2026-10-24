"""Platform shims for running the Qt UI where it is not shipped (macOS, offscreen CI)."""

from __future__ import annotations

import os
import sys


def disable_mac_native_frameless() -> None:
    """Make qframelesswindow's macOS backend a no-op.

    Its Cocoa code dereferences winId() as an NSView, which segfaults under the offscreen
    platform (tests, `--smoke-test`). The shipped app runs on Windows; on a Mac only the Qt
    side of FluentWindow is needed.
    """
    if sys.platform != "darwin":
        return
    from qframelesswindow.mac import MacFramelessWindow

    for name in (
        "updateFrameless",
        "_hideSystemTitleBar",
        "setSystemTitleBarButtonVisible",
        "_updateSystemButtonRect",
    ):
        setattr(MacFramelessWindow, name, lambda self, *args, **kwargs: None)


def tolerate_offscreen_window_handles() -> None:
    """Windows + offscreen platform (CI tests): keep qframelesswindow from crashing.

    Its Windows backend calls GetWindowPlacement(winId()) on every WindowStateChange; under
    the offscreen platform winId() is not a real HWND, so that raises inside Qt's event loop
    (Win32 error 1400). A window that cannot be asked is simply "not maximized". Real Windows
    sessions use the windows platform and are left untouched.
    """
    if sys.platform != "win32" or os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        return
    from qframelesswindow.utils import win32_utils

    original = win32_utils.isMaximized
    if getattr(original, "_runnerdeck_tolerant", False):
        return

    def is_maximized(hwnd: object) -> bool:
        try:
            return bool(original(hwnd))
        except Exception:  # noqa: BLE001 - pywintypes.error: no such window
            return False

    is_maximized._runnerdeck_tolerant = True  # type: ignore[attr-defined]
    win32_utils.isMaximized = is_maximized
