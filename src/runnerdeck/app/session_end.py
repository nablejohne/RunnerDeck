"""Notice a Windows logoff / shutdown that was cancelled (docs/m4-design.md §5).

Qt turns WM_QUERYENDSESSION into QApplication.commitDataRequest, where RunnerDeck signals every
runner to stop (the session must not wait for them). If the user then picks "cancel" on the
"these apps are preventing sign-out" screen, Windows sends WM_ENDSESSION with wParam FALSE and
nothing else happens in Qt: this filter catches that message so the runners can be started
again. It is sent to every top-level window, so the callback may run several times.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable
from typing import Any

from PyQt5.QtCore import QAbstractNativeEventFilter

log = logging.getLogger(__name__)

WM_ENDSESSION = 0x0016
_EVENT_TYPE = b"windows_generic_MSG"


class _MsgHead(ctypes.Structure):
    """The first fields of a Win32 MSG (hwnd, message, wParam): all this filter reads."""

    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint), ("wParam", ctypes.c_size_t)]


class SessionEndFilter(QAbstractNativeEventFilter):
    def __init__(self, on_cancelled: Callable[[], None]) -> None:
        super().__init__()
        self._on_cancelled = on_cancelled

    def nativeEventFilter(self, event_type: Any, message: Any) -> tuple[bool, int]:  # noqa: N802
        if bytes(event_type) == _EVENT_TYPE and message:
            msg = _MsgHead.from_address(int(message))
            if msg.message == WM_ENDSESSION and msg.wParam == 0:
                log.info("WM_ENDSESSION(FALSE): the logoff / shutdown was cancelled")
                try:
                    self._on_cancelled()
                except Exception:  # noqa: BLE001 - never break Qt's message loop
                    log.exception("handling the cancelled session end failed")
        return False, 0  # never swallow the message


def install(app: Any, on_cancelled: Callable[[], None]) -> SessionEndFilter | None:
    """Install the filter on Windows (None elsewhere). Keep the returned object alive."""
    if sys.platform != "win32":
        return None
    session_filter = SessionEndFilter(on_cancelled)
    app.installNativeEventFilter(session_filter)
    return session_filter
