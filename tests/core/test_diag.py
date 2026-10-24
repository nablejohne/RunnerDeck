"""Tests for runnerdeck.core.diag (listing / tail-reading a runner's `_diag` logs)."""

from __future__ import annotations

import codecs
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from runnerdeck.core import diag
from runnerdeck.core.diag import DIAG_DIR, MAX_TAIL_BYTES, list_diag_logs, read_tail
from runnerdeck.core.errors import RunnerDeckError
from runnerdeck.core.models import DiagLog
from runnerdeck.core.redact import MASK

FAKE_PAT = "github_pat_" + "0FAKE0" * 10  # right shape, obviously not real


def write(path: Path, text: str, *, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# --- list_diag_logs -------------------------------------------------------------------------


def test_list_missing_diag_dir(tmp_path: Path) -> None:
    assert list_diag_logs(tmp_path) == []
    assert list_diag_logs(tmp_path / "no-such-runner") == []


def test_list_diag_dir_that_is_a_file(tmp_path: Path) -> None:
    write(tmp_path / DIAG_DIR, "x")
    assert list_diag_logs(tmp_path) == []


def test_list_sorts_newest_first_and_classifies(tmp_path: Path) -> None:
    d = tmp_path / DIAG_DIR
    base = time.time() - 1000
    runner = write(d / "Runner_20261024-080000-utc.log", "r" * 10, mtime=base + 30)
    worker = write(d / "Worker_20261024-080100-utc.log", "w" * 20, mtime=base + 20)
    other = write(d / "SelfUpdate-20261024.log", "o", mtime=base + 10)
    tie = write(d / "a-same-time.LOG", "t", mtime=base + 30)
    write(d / "Runner_notes.txt", "not a log", mtime=base + 40)
    (d / "pages").mkdir()
    (d / "pages" / "step.log").write_text("nested logs are not listed", encoding="utf-8")
    (d / "dir.log").mkdir()  # a directory with a .log name is not a file

    logs = list_diag_logs(tmp_path)
    assert [entry.path for entry in logs] == [tie, runner, worker, other]
    assert [entry.kind for entry in logs] == ["other", "runner", "worker", "other"]
    first = logs[1]
    assert isinstance(first, DiagLog)
    assert first.name == runner.name
    assert first.size == 10
    assert first.modified == datetime.fromtimestamp(base + 30, UTC)
    assert first.modified.tzinfo is UTC


def test_list_kind_prefix_is_case_insensitive(tmp_path: Path) -> None:
    write(tmp_path / DIAG_DIR / "runner_lower.log", "x")
    write(tmp_path / DIAG_DIR / "WORKER_UPPER.log", "x")
    kinds = {entry.name: entry.kind for entry in list_diag_logs(tmp_path)}
    assert kinds == {"runner_lower.log": "runner", "WORKER_UPPER.log": "worker"}


def test_list_skips_entries_that_cannot_be_stat_ed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path / DIAG_DIR / "Runner_ok.log", "x")
    write(tmp_path / DIAG_DIR / "Runner_gone.log", "x")
    real_scandir = os.scandir

    class Entry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self._entry = entry
            self.name = entry.name
            self.path = entry.path

        def is_file(self) -> bool:
            is_file = self._entry.is_file()
            if "gone" in self.name:
                os.remove(self.path)  # deleted between the listing and the stat
            return is_file

    class Scan:
        def __init__(self, path: Path) -> None:
            self._it = real_scandir(path)

        def __enter__(self) -> list[Entry]:
            return [Entry(e) for e in self._it]

        def __exit__(self, *exc: object) -> None:
            self._it.close()

    monkeypatch.setattr(diag.os, "scandir", Scan)
    assert [entry.name for entry in list_diag_logs(tmp_path)] == ["Runner_ok.log"]


def test_list_unreadable_diag_dir_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(path: Path) -> None:
        raise PermissionError(13, "拒绝访问")

    monkeypatch.setattr(diag.os, "scandir", denied)
    with pytest.raises(RunnerDeckError, match="无法读取诊断日志目录 .*拒绝访问"):
        list_diag_logs(tmp_path)


# --- read_tail ------------------------------------------------------------------------------


def test_read_small_file_whole(tmp_path: Path) -> None:
    path = write(tmp_path / "Runner_1.log", "line 1\r\nline 2\r\n")
    assert read_tail(path) == "line 1\nline 2\n"


def test_read_strips_bom_and_replaces_bad_bytes(tmp_path: Path) -> None:
    path = tmp_path / "Runner_1.log"
    path.write_bytes(codecs.BOM_UTF8 + "中文\n".encode() + b"\xff\xfe broken\n")
    assert read_tail(path) == "中文\n�� broken\n"


def test_read_tail_drops_partial_first_line_and_says_so(tmp_path: Path) -> None:
    lines = [f"{i:04d} {'x' * 20}" for i in range(100)]
    path = write(tmp_path / "Runner_1.log", "\n".join(lines) + "\n")
    text = read_tail(path, max_bytes=100)
    head, *rest = text.split("\n")
    assert head == "…（只显示最后 100 字节）"
    assert rest[-1] == ""  # file ends with a newline
    kept = rest[:-1]
    assert kept == lines[-len(kept) :]  # only whole lines, the newest ones
    assert 0 < len(kept) < 5


def test_read_tail_note_uses_mb_for_the_default(tmp_path: Path) -> None:
    line = "y" * 99 + "\n"
    path = write(tmp_path / "Runner_1.log", line * (MAX_TAIL_BYTES // 100 + 50))
    text = read_tail(path)
    assert text.startswith("…（只显示最后 1 MB）\n")
    assert len(text.encode("utf-8")) <= MAX_TAIL_BYTES + 100
    assert text.split("\n")[1] == "y" * 99


def test_read_tail_note_in_kb(tmp_path: Path) -> None:
    path = write(tmp_path / "Runner_1.log", "z\n" * 2000)
    assert read_tail(path, max_bytes=2048).startswith("…（只显示最后 2 KB）\n")


def test_read_tail_without_any_newline_keeps_the_fragment(tmp_path: Path) -> None:
    path = write(tmp_path / "Runner_1.log", "a" * 50)
    assert read_tail(path, max_bytes=10) == "…（只显示最后 10 字节）\n" + "a" * 10


def test_read_tail_cut_inside_a_multibyte_char_on_the_first_line(tmp_path: Path) -> None:
    path = write(tmp_path / "Runner_1.log", "开头中文\n第二行\n")
    limit = len("文\n第二行\n".encode()) - 1  # starts in the middle of 文
    assert read_tail(path, max_bytes=limit) == f"…（只显示最后 {limit} 字节）\n第二行\n"


def test_read_redacts(tmp_path: Path) -> None:
    path = write(tmp_path / "Worker_1.log", f"token is {FAKE_PAT}\n--token ABCDEFGH12345\n")
    text = read_tail(path)
    assert FAKE_PAT not in text and "ABCDEFGH12345" not in text
    assert text.count(MASK) == 2


def test_read_missing_file_is_a_runnerdeck_error(tmp_path: Path) -> None:
    with pytest.raises(RunnerDeckError, match=r"无法读取 .*Runner_x\.log："):
        read_tail(tmp_path / "Runner_x.log")


def test_read_rejects_non_positive_limit(tmp_path: Path) -> None:
    path = write(tmp_path / "Runner_1.log", "x\n")
    with pytest.raises(ValueError):
        read_tail(path, max_bytes=0)


def test_read_while_another_handle_keeps_writing(tmp_path: Path) -> None:
    # Like the running Listener: the file is open for writing (and growing) while we read.
    path = tmp_path / "Runner_live.log"
    with path.open("w", encoding="utf-8") as writer:
        writer.write("first\n")
        writer.flush()
        assert read_tail(path) == "first\n"
        writer.write("second, not finished")
        writer.flush()
        assert read_tail(path) == "first\nsecond, not finished"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only: share modes")
def test_windows_read_while_open_for_writing_by_another_process(tmp_path: Path) -> None:
    import subprocess

    path = tmp_path / "Runner_shared.log"
    code = (
        "import sys, time\n"
        "f = open(sys.argv[1], 'w', encoding='utf-8')\n"
        "f.write('hello from the writer\\n'); f.flush()\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        assert read_tail(path) == "hello from the writer\n"
    finally:
        child.kill()
        child.wait(10)
