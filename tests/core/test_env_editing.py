"""The `.env` editor's core: parse_env_text / read_env_text / replace_env_file."""

from __future__ import annotations

from pathlib import Path

import pytest

from runnerdeck.core import installer
from runnerdeck.core.errors import InstallError, InvalidInputError


def test_parse_env_text_keeps_values_verbatim() -> None:
    text = "A=1\r\n\n  \nGIT_CONFIG_VALUE_0=false\nURL=https://x/?a=b \nEMPTY=\n"
    assert installer.parse_env_text(text) == [
        ("A", "1"),
        ("GIT_CONFIG_VALUE_0", "false"),
        ("URL", "https://x/?a=b "),  # split at the first "=", nothing trimmed
        ("EMPTY", ""),
    ]
    assert installer.parse_env_text("") == []


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("A=1\n# note\n", "第 2 行：.env 不支持注释"),
        ("A=1\nJUSTTEXT\n", "第 2 行：应为 KEY=VALUE"),
        ("=1\n", "第 1 行：变量名 '' 不合法"),
        ("1A=1\n", "第 1 行：变量名 '1A' 不合法"),
        (" A=1\n", "第 1 行：变量名 ' A' 不合法"),
        ("http_proxy=a\nHTTP_PROXY=b\n", "第 2 行：HTTP_PROXY 和第 1 行重复"),
    ],
)
def test_parse_env_text_rejects(text: str, needle: str) -> None:
    with pytest.raises(InvalidInputError, match=needle):
        installer.parse_env_text(text)


def test_read_env_text(tmp_path: Path) -> None:
    assert installer.read_env_text(tmp_path) == ""
    (tmp_path / ".env").write_bytes("﻿A=中文\r\n".encode())
    assert installer.read_env_text(tmp_path) == "A=中文\r\n"
    (tmp_path / ".env").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(InstallError, match="无法读取"):
        installer.read_env_text(tmp_path)


def test_replace_env_file(tmp_path: Path) -> None:
    installer.replace_env_file(tmp_path, "B=2\r\n\r\nA=1")
    assert (tmp_path / ".env").read_bytes() == b"B=2\nA=1\n"
    assert installer.read_env_values(tmp_path) == {"b": "2", "a": "1"}
    with pytest.raises(InvalidInputError):
        installer.replace_env_file(tmp_path, "broken")
    assert (tmp_path / ".env").read_bytes() == b"B=2\nA=1\n"  # untouched on a bad edit
    installer.replace_env_file(tmp_path, "\n  \n")
    assert not (tmp_path / ".env").exists()
    installer.replace_env_file(tmp_path, "")  # nothing to delete: fine
    with pytest.raises(InstallError, match="不存在"):
        installer.replace_env_file(tmp_path / "gone", "A=1")
