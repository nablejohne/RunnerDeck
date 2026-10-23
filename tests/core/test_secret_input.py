"""secret_input.read_secret: Ctrl+V in the classic Windows console, pipes, editing keys."""

from __future__ import annotations

import io

import pytest

from runnerdeck.core import secret_input, secrets
from runnerdeck.core.errors import InvalidInputError

PAT = "github_pat_" + "A1b2C3d4E5" * 8


class Keys:
    """Feeds getwch() from a string and records what putwch() echoed."""

    def __init__(self, typed: str) -> None:
        self.typed = list(typed)
        self.echo: list[str] = []

    def getwch(self) -> str:
        return self.typed.pop(0)

    def putwch(self, c: str) -> None:
        self.echo.append(c)


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def read(typed: str, clipboard: str = "") -> tuple[str, Keys]:
    keys = Keys(typed)
    value = secret_input.read_secret(
        "PAT: ",
        stdin=Tty(),
        getwch=keys.getwch,
        putwch=keys.putwch,
        clipboard=lambda: clipboard,
        windows=True,
    )
    return value, keys


def test_ctrl_v_pastes_the_clipboard() -> None:
    # Real 2026-10-23: in powershell.exe's console Ctrl+V reaches getwch as U+0016.
    value, keys = read("\x16\r", clipboard=f"  {PAT}\r\n")
    assert value == PAT
    assert PAT not in "".join(keys.echo)  # never echoed
    assert "".join(keys.echo) == "PAT: \r\n"


def test_typed_and_right_click_pasted_text_is_read_as_is() -> None:
    assert read(PAT + "\r")[0] == PAT


def test_backspace_and_control_keys() -> None:
    assert read("abX\x08c\x1b\x01\r")[0] == "abc"  # ESC / Ctrl+A ignored
    assert read("\x08a\n")[0] == "a"  # backspace on empty input is harmless


def test_extended_keys_are_swallowed() -> None:
    assert read("a\xe0Hb\x00;c\r")[0] == "abc"  # arrow up, F1


def test_ctrl_c_interrupts() -> None:
    with pytest.raises(KeyboardInterrupt):
        read("ab\x03")


def test_piped_stdin_is_read_as_a_line() -> None:
    # Get-Content token.txt | runnerdeck-cli login
    value = secret_input.read_secret("PAT: ", stdin=io.StringIO(PAT + "\r\nrest\n"))
    assert value == PAT


def test_empty_pipe_is_eof() -> None:
    with pytest.raises(EOFError):
        secret_input.read_secret("PAT: ", stdin=io.StringIO(""))


def test_non_windows_tty_uses_getpass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_input.getpass, "getpass", lambda prompt: "from-getpass")
    assert secret_input.read_secret("PAT: ", stdin=Tty(), windows=False) == "from-getpass"


def test_clipboard_text_is_empty_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_input.sys, "platform", "linux")
    assert secret_input.clipboard_text() == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("\x16", ["实际读到 1 个字符", "1 个控制字符", "Ctrl+V"]),
        ("", ["实际读到 0 个字符", "什么都没读到"]),
        (PAT[:20], ["实际读到 20 个字符", "开头正确"]),
        (PAT[:30] + " " + PAT[30:], ["开头正确", "1 个空格或换行"]),
        ("my-token-name", ["实际读到 13 个字符", "2 个其他字符"]),
    ],
)
def test_bad_pat_message_describes_without_revealing(value: str, expected: list[str]) -> None:
    with pytest.raises(InvalidInputError) as info:
        secrets.validate_pat_format(value)
    message = str(info.value)
    for part in expected:
        assert part in message
    if len(value) > 3:
        assert value.strip() not in message
    assert "右键粘贴" in message


def test_bom_before_a_pasted_pat_is_tolerated() -> None:
    assert secrets.validate_pat_format("﻿" + PAT) == PAT


@pytest.mark.skipif(
    not __import__("sys").platform.startswith("win"), reason="Windows only: real clipboard API"
)
def test_clipboard_text_runs_on_windows() -> None:
    assert isinstance(secret_input.clipboard_text(), str)  # empty or whatever is there
