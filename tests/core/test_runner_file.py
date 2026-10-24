"""Tests for runnerdeck.core.runner_file (.runner parsing, URL parsing, directory checks)."""

from __future__ import annotations

import builtins
import codecs
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from runnerdeck.core import runner_file
from runnerdeck.core.errors import InvalidInputError, RunnerDeckError, RunnerFileError
from runnerdeck.core.runner_file import (
    CREDENTIALS_FILE,
    RUNNER_FILE,
    is_configured,
    looks_like_runner_dir,
    parse_github_url,
    read_runner_file,
    scan_runner_dirs,
)

# SYNTHETIC sample: the round-1 spike did not save a real `.runner` (spike-notes §3).
# Field names follow the runner source (m1-design §6); values are made up.
SAMPLE: dict[str, Any] = {
    "agentId": 42,
    "agentName": "DESKTOP-TEST-runnerdeck-sandbox",
    "poolId": 1,
    "poolName": "Default",
    "serverUrl": "https://pipelines.actions.githubusercontent.com/FAKEFAKEFAKE/",
    "gitHubUrl": "https://github.com/nablejohne/runnerdeck-sandbox",
    "workFolder": "_work",
    "useV2Flow": True,
    "serverUrlV2": "https://broker.actions.githubusercontent.com/",
}


def write_runner(runner_dir: Path, data: Any, *, bom: bool = True) -> Path:
    runner_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2).encode("utf-8")
    path = runner_dir / RUNNER_FILE
    path.write_bytes((codecs.BOM_UTF8 if bom else b"") + payload)
    return path


def make_configured(runner_dir: Path) -> Path:
    write_runner(runner_dir, SAMPLE)  # creates runner_dir
    (runner_dir / CREDENTIALS_FILE).write_bytes(b"\x00\xffnot json, never read")
    return runner_dir


# --- read_runner_file -------------------------------------------------------------------------


def test_read_runner_file_with_bom(tmp_path):
    write_runner(tmp_path, SAMPLE, bom=True)
    info = read_runner_file(tmp_path)
    assert info is not None
    assert info.agent_id == 42
    assert info.agent_name == "DESKTOP-TEST-runnerdeck-sandbox"
    assert info.pool_id == 1
    assert info.server_url == SAMPLE["serverUrl"]
    assert info.github_url == "https://github.com/nablejohne/runnerdeck-sandbox"
    assert info.work_folder == "_work"
    assert info.raw == SAMPLE  # unknown keys (poolName, useV2Flow, ...) kept


def test_read_runner_file_without_bom(tmp_path):
    write_runner(tmp_path, SAMPLE, bom=False)
    info = read_runner_file(tmp_path)
    assert info is not None and info.agent_id == 42


def test_read_runner_file_minimal_fields_use_defaults(tmp_path):
    data = {"agentId": "7", "agentName": "r", "gitHubUrl": "https://github.com/o/r"}
    write_runner(tmp_path, data)
    info = read_runner_file(tmp_path)
    assert info is not None
    assert info.agent_id == 7
    assert info.pool_id is None and info.server_url is None
    assert info.work_folder == "_work"
    assert info.raw == data


def test_read_runner_file_missing(tmp_path):
    assert read_runner_file(tmp_path) is None
    assert read_runner_file(tmp_path / "does-not-exist") is None


def test_read_runner_file_dir_is_a_file(tmp_path):
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    assert read_runner_file(not_a_dir) is None


@pytest.mark.parametrize(
    ("content", "match"),
    [
        (codecs.BOM_UTF8 + b'{"agentId": 42, "agentName": ', "JSON"),
        (b"", "JSON"),
        (b"[1, 2, 3]", "JSON 对象"),
        (b'"just a string"', "JSON 对象"),
        (b"\xff\xfe{\x00}\x00", "UTF-8"),
    ],
)
def test_read_runner_file_corrupt(tmp_path, content, match):
    (tmp_path / RUNNER_FILE).write_bytes(content)
    with pytest.raises(RunnerFileError, match=match) as exc:
        read_runner_file(tmp_path)
    assert str(tmp_path / RUNNER_FILE) in str(exc.value)


@pytest.mark.parametrize("missing", ["agentId", "agentName", "gitHubUrl"])
def test_read_runner_file_missing_required_field(tmp_path, missing):
    write_runner(tmp_path, {k: v for k, v in SAMPLE.items() if k != missing})
    with pytest.raises(RunnerFileError, match=missing):
        read_runner_file(tmp_path)


def test_read_runner_file_invalid_field_type(tmp_path):
    write_runner(tmp_path, {**SAMPLE, "agentId": "not-a-number"})
    with pytest.raises(RunnerFileError, match="agentId"):
        read_runner_file(tmp_path)


def test_read_runner_file_unreadable(tmp_path):
    # A directory in place of the file: IsADirectoryError on POSIX, PermissionError on Windows.
    (tmp_path / RUNNER_FILE).mkdir()
    with pytest.raises(RunnerFileError, match="无法读取"):
        read_runner_file(tmp_path)


# --- parse_github_url -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/nablejohne/runnerdeck-sandbox", ("nablejohne", "runnerdeck-sandbox")),
        ("https://github.com/owner/repo/", ("owner", "repo")),
        ("  https://github.com/owner/repo \n", ("owner", "repo")),
        ("https://github.com/owner/repo.git", ("owner", "repo")),
        ("https://github.com/owner/repo.git/", ("owner", "repo")),
        ("HTTPS://GitHub.COM/Owner/Repo", ("Owner", "Repo")),
        ("https://github.com/owner/my.repo_name-2", ("owner", "my.repo_name-2")),
        ("https://github.com/owner/repo?tab=readme#top", ("owner", "repo")),
    ],
)
def test_parse_github_url_accepts(url, expected):
    assert parse_github_url(url) == expected


@pytest.mark.parametrize(
    ("url", "match"),
    [
        ("https://github.com/myorg", "组织"),
        ("https://github.com/myorg/", "组织"),
        ("https://github.com/enterprises/acme", "企业或组织"),
        ("https://github.com/orgs/myorg", "企业或组织"),
        ("https://github.com", "仓库级"),
        ("https://github.com/", "仓库级"),
        ("https://github.com/owner/repo/tree/main", "仓库级"),
        ("https://github.com/owner//repo", "仓库级"),
        ("https://github.com/owner/repo//", "仓库级"),
        ("http://github.com/owner/repo", "https://github.com"),
        ("github.com/owner/repo", "https://github.com"),
        ("owner/repo", "https://github.com"),
        ("", "https://github.com"),
        ("https://gitlab.com/owner/repo", "https://github.com"),
        ("https://github.com.evil.example/owner/repo", "https://github.com"),
        ("https://api.github.com/repos/owner/repo", "https://github.com"),
        ("https://github.com:8443/owner/repo", "https://github.com"),
        ("https://github.com:notaport/owner/repo", "不是有效"),
        ("https://github.com//repo", "所有者"),
        ("https://github.com/-bad/repo", "所有者"),
        ("https://github.com/owner/.git", "仓库名"),
        ("https://github.com/owner/re%20po", "仓库名"),
    ],
)
def test_parse_github_url_rejects(url, match):
    with pytest.raises(InvalidInputError, match=match):
        parse_github_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://x-access-token:s3cretValue123@github.com/owner/repo",
        "https://s3cretValue123@github.com/owner/repo",
    ],
)
def test_parse_github_url_never_echoes_userinfo(url):
    with pytest.raises(InvalidInputError) as exc:
        parse_github_url(url)
    assert "s3cretValue123" not in str(exc.value)


# --- is_configured / looks_like_runner_dir ----------------------------------------------------


def test_is_configured_needs_both_files(tmp_path):
    assert not is_configured(tmp_path)
    write_runner(tmp_path, SAMPLE)
    assert not is_configured(tmp_path)
    (tmp_path / CREDENTIALS_FILE).write_bytes(b"x")
    assert is_configured(tmp_path)
    (tmp_path / RUNNER_FILE).unlink()
    assert not is_configured(tmp_path)


def test_is_configured_missing_dir(tmp_path):
    assert not is_configured(tmp_path / "nope")


def test_is_configured_requires_regular_files(tmp_path):
    write_runner(tmp_path, SAMPLE)
    (tmp_path / CREDENTIALS_FILE).mkdir()
    assert not is_configured(tmp_path)


def test_is_configured_never_opens_credentials(tmp_path, monkeypatch):
    runner_dir = make_configured(tmp_path / "runners" / "repo")
    opened: list[str] = []

    def guard(real):
        def wrapper(file, *args, **kwargs):
            name = os.path.basename(os.fspath(file)) if not isinstance(file, int) else ""
            if name.startswith(CREDENTIALS_FILE):
                opened.append(name)
                raise AssertionError(f"opened {name}")
            return real(file, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(io, "open", guard(io.open))
    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(os, "open", guard(os.open))

    assert is_configured(runner_dir)
    assert scan_runner_dirs(runner_dir.parent) == [runner_dir]
    assert read_runner_file(runner_dir) is not None  # .runner itself may be opened
    assert opened == []


@pytest.mark.skipif(
    sys.platform == "win32" or getattr(os, "geteuid", lambda: 0)() == 0,
    reason="needs POSIX permissions as a non-root user",
)
def test_is_configured_with_unreadable_credentials(tmp_path):
    make_configured(tmp_path)
    creds = tmp_path / CREDENTIALS_FILE
    creds.chmod(0)
    try:
        with pytest.raises(PermissionError):
            creds.read_bytes()
        assert is_configured(tmp_path)
    finally:
        creds.chmod(0o600)


def test_is_configured_stat_error_counts_as_absent(tmp_path, monkeypatch):
    make_configured(tmp_path)
    real_is_file = Path.is_file

    def is_file(self):
        if self.name == CREDENTIALS_FILE:
            raise PermissionError(13, "denied")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", is_file)
    assert not is_configured(tmp_path)


def test_looks_like_runner_dir(tmp_path):
    assert not looks_like_runner_dir(tmp_path)
    (tmp_path / "config.cmd").write_text("@echo off")
    assert not looks_like_runner_dir(tmp_path)
    (tmp_path / "run.cmd").write_text("@echo off")
    assert looks_like_runner_dir(tmp_path)
    assert not looks_like_runner_dir(tmp_path / "missing")


def test_looks_like_runner_dir_rejects_directories(tmp_path):
    (tmp_path / "config.cmd").mkdir()
    (tmp_path / "run.cmd").write_text("@echo off")
    assert not looks_like_runner_dir(tmp_path)


# --- scan_runner_dirs -------------------------------------------------------------------------


def test_scan_runner_dirs(tmp_path):
    root = tmp_path / "runners"
    make_configured(root / "b-repo")
    make_configured(root / "A-repo")
    make_configured(root / "c-repo")
    write_runner(root / "only-runner-file", SAMPLE)  # not configured: no .credentials
    (root / "extracted-only").mkdir()
    (root / "extracted-only" / "config.cmd").write_text("@echo off")
    make_configured(root / "nested" / "deeper")  # only one level is scanned
    (root / "stray.txt").write_text("x")

    assert scan_runner_dirs(root) == [root / "A-repo", root / "b-repo", root / "c-repo"]


def test_scan_runner_dirs_missing_root(tmp_path):
    assert scan_runner_dirs(tmp_path / "nope") == []


def test_scan_runner_dirs_root_is_a_file(tmp_path):
    root = tmp_path / "runners"
    root.write_text("x")
    assert scan_runner_dirs(root) == []


def test_scan_runner_dirs_empty_root(tmp_path):
    assert scan_runner_dirs(tmp_path) == []


def test_scan_runner_dirs_unreadable_root(tmp_path, monkeypatch):
    make_configured(tmp_path / "repo")

    def iterdir(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "iterdir", iterdir)
    with pytest.raises(RunnerDeckError, match="无法读取目录"):
        scan_runner_dirs(tmp_path)


def test_scan_runner_dirs_skips_child_it_cannot_stat(tmp_path, monkeypatch):
    make_configured(tmp_path / "good")
    make_configured(tmp_path / "locked")
    real_is_dir = Path.is_dir

    def is_dir(self):
        if self.name == "locked":
            raise PermissionError(13, "denied")
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    assert scan_runner_dirs(tmp_path) == [tmp_path / "good"]


# --- read_runner_version -------------------------------------------------------------------


def _listener(runner_dir: Path) -> Path:
    exe = runner_dir / "bin" / "Runner.Listener.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    return exe


def test_read_runner_version_uses_the_listener_file_version(tmp_path: Path) -> None:
    exe = _listener(tmp_path)
    seen: list[Path] = []

    def reader(path: Path) -> tuple[int, int, int, int]:
        seen.append(path)
        return 2, 338, 1, 0

    assert runner_file.read_runner_version(tmp_path, reader=reader) == "2.338.1"
    assert seen == [exe]


def test_read_runner_version_is_cached_until_the_exe_changes(tmp_path: Path) -> None:
    exe = _listener(tmp_path)
    versions = iter([(2, 338, 1, 0), (2, 339, 0, 0)])
    calls: list[Path] = []

    def reader(path: Path) -> tuple[int, int, int, int]:
        calls.append(path)
        return next(versions)

    assert runner_file.read_runner_version(tmp_path, reader=reader) == "2.338.1"
    assert runner_file.read_runner_version(tmp_path, reader=reader) == "2.338.1"
    assert len(calls) == 1
    st = os.stat(exe)
    os.utime(exe, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # self-update
    assert runner_file.read_runner_version(tmp_path, reader=reader) == "2.339.0"
    assert len(calls) == 2


def test_read_runner_version_unknown(tmp_path: Path) -> None:
    assert runner_file.read_runner_version(tmp_path, reader=lambda _p: (1, 0, 0, 0)) is None
    _listener(tmp_path)
    assert runner_file.read_runner_version(tmp_path, reader=lambda _p: None) is None


def test_pe_file_version_off_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_file.sys, "platform", "darwin")
    assert runner_file.pe_file_version(_listener(tmp_path)) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows version resources")
def test_pe_file_version_of_a_real_exe() -> None:
    parts = runner_file.pe_file_version(Path(sys.executable))
    assert parts is not None and parts[0] == 3
