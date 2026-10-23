"""Platform shims for running the Qt UI where it is not shipped (macOS dev machines)."""

from __future__ import annotations

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
