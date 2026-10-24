"""app/session_end.py: a cancelled logoff (docs/m4-design.md §5)."""

from __future__ import annotations

import ctypes
import sys
from typing import Any

from PyQt5.QtCore import QByteArray

from runnerdeck.app import session_end
from runnerdeck.app.session_end import WM_ENDSESSION, SessionEndFilter


def _msg(message: int, wparam: int) -> Any:
    head = session_end._MsgHead(None, message, wparam)
    return head, ctypes.addressof(head)


def test_only_wm_endsession_false_counts() -> None:
    calls: list[str] = []
    f = SessionEndFilter(lambda: calls.append("cancelled"))
    event = QByteArray(b"windows_generic_MSG")
    _keep, cancelled = _msg(WM_ENDSESSION, 0)
    _keep2, ending = _msg(WM_ENDSESSION, 1)
    _keep3, other = _msg(0x0011, 0)  # WM_QUERYENDSESSION
    assert f.nativeEventFilter(event, ending) == (False, 0)
    assert f.nativeEventFilter(event, other) == (False, 0)
    assert f.nativeEventFilter(QByteArray(b"xcb_generic_event_t"), cancelled) == (False, 0)
    assert calls == []
    assert f.nativeEventFilter(event, cancelled) == (False, 0)  # never swallowed
    assert calls == ["cancelled"]


def test_a_failing_callback_never_breaks_the_message_loop() -> None:
    def boom() -> None:
        raise RuntimeError("bug")

    _keep, cancelled = _msg(WM_ENDSESSION, 0)
    assert SessionEndFilter(boom).nativeEventFilter(
        QByteArray(b"windows_generic_MSG"), cancelled
    ) == (False, 0)


def test_install(qapp: Any) -> None:
    installed = session_end.install(qapp, lambda: None)
    if sys.platform == "win32":
        assert isinstance(installed, SessionEndFilter)
        qapp.removeNativeEventFilter(installed)
    else:
        assert installed is None
