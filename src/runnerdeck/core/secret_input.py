"""Reading a PAT from the terminal without echo, in a way that works with Windows paste.

getpass on Windows reads raw key events (msvcrt.getwch). In the classic console host
(powershell.exe / cmd.exe windows) Ctrl+V is not turned into a paste for such programs: it
arrives as the single character U+0016 and the clipboard never reaches us (real report,
2026-10-23). Windows Terminal and right-click paste do deliver the text. This reader treats
U+0016 as "paste the clipboard". When stdin is not a console (piped:
`Get-Content token.txt | runnerdeck-cli login`) it reads one line from stdin instead, which
getpass on Windows would ignore.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable
from typing import TextIO

CTRL_C = "\x03"
CTRL_V = "\x16"
BACKSPACE = "\x08"
_EXTENDED_KEY_PREFIXES = ("\x00", "\xe0")  # arrows / F-keys: prefix + one more code


def read_secret(
    prompt: str,
    *,
    stdin: TextIO | None = None,
    getwch: Callable[[], str] | None = None,
    putwch: Callable[[str], None] | None = None,
    clipboard: Callable[[], str] | None = None,
    windows: bool | None = None,
) -> str:
    """Prompt and read one secret line. Raises EOFError if a pipe is empty, KeyboardInterrupt
    on Ctrl+C. The injectable parameters exist for tests."""
    stream = sys.stdin if stdin is None else stdin
    if stream is not None and not stream.isatty():
        line = stream.readline()
        if not line:
            raise EOFError
        return line.rstrip("\r\n")
    if not (sys.platform == "win32" if windows is None else windows):
        return getpass.getpass(prompt)
    if getwch is None or putwch is None:
        import msvcrt

        getwch = getwch or msvcrt.getwch
        putwch = putwch or msvcrt.putwch
    read_clipboard = clipboard or clipboard_text
    for ch in prompt:
        putwch(ch)
    chars: list[str] = []
    while True:
        c = getwch()
        if c in ("\r", "\n"):
            break
        if c == CTRL_C:
            raise KeyboardInterrupt
        if c == BACKSPACE:
            if chars:
                chars.pop()
        elif c == CTRL_V:
            chars.extend(read_clipboard().strip())
        elif c in _EXTENDED_KEY_PREFIXES:
            getwch()  # swallow the key code that follows
        elif ord(c) >= 0x20:
            chars.append(c)
        # other control characters (Ctrl+letters, ESC) are ignored
    putwch("\r")
    putwch("\n")
    return "".join(chars)


def clipboard_text() -> str:
    """Unicode text on the Windows clipboard, or "" if there is none / it is unavailable."""
    if sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    cf_unicodetext = 13

    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(cf_unicodetext)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
