"""Tests for core/installer.py.

config.cmd output comes from the real captures in tests/fixtures/config (spike §3). Tokens
are obviously fake values generated per test.
"""

from __future__ import annotations

import functools
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import psutil
import pytest

from runnerdeck.core import installer, winproc
from runnerdeck.core.errors import InstallError, InvalidInputError
from runnerdeck.core.installer import (
    CONFIG_TIMEOUT_S,
    CommandResult,
    build_config_command,
    build_remove_command,
    configure,
    extract_runner,
    remove_runner_dir,
    run_command,
    unconfigure,
    write_env_file,
)
from runnerdeck.core.redact import MASK, redact

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "config"
CONFIG_OK = (FIXTURES / "config_ok.txt").read_bytes()
REMOVE_OK = (FIXTURES / "remove_ok.txt").read_bytes()
URL = "https://github.com/octo/sandbox"
PROXY = "http://127.0.0.1:7890"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def fake_token() -> str:
    """Unique, obviously fake registration token (29 chars like the real ones)."""
    return "FAKE" + uuid.uuid4().hex[:25].upper()


class FakeRun:
    """Stands in for subprocess.run: records the call, returns canned stdout or raises."""

    def __init__(
        self, stdout: bytes = b"", returncode: int = 0, exc: BaseException | None = None
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.exc = exc
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((args, kwargs))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, None)


@pytest.fixture
def runner_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runner"
    d.mkdir()
    (d / "config.cmd").write_text("@echo off\r\n", encoding="ascii")
    (d / "run.cmd").write_text("@echo off\r\n", encoding="ascii")
    return d


def make_zip(path: Path, members: dict[str, bytes], *, stored: bool = False) -> Path:
    compression = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


RUNNER_MEMBERS = {
    "config.cmd": b"@echo off\r\n",
    "run.cmd": b"@echo off\r\n",
    "bin/Runner.Listener.exe": b"MZ fake",
    "externals/node20/bin/node.exe": b"MZ fake node",
}


# --- build_config_command ------------------------------------------------------------------


def test_build_config_command_exact_list() -> None:
    token = fake_token()
    args = build_config_command(url=URL, token=token, name="box-sandbox", labels=["gpu", "big"])
    assert args == [
        winproc.cmd_exe(), "/c", "config.cmd", "--unattended",
        "--url", URL,
        "--token", token,
        "--name", "box-sandbox",
        "--labels", "gpu,big",
        "--work", "_work",
        "--replace",
    ]  # fmt: skip


@pytest.mark.parametrize("labels", [[], (), ["self-hosted", "Windows", "X64"], [" ", ""], ""])
def test_build_config_command_omits_empty_labels(labels: Any) -> None:
    args = build_config_command(url=URL, token=fake_token(), name="box", labels=labels)
    assert "--labels" not in args
    assert args[-3:] == ["--work", "_work", "--replace"]


def test_build_config_command_normalizes_labels_and_name() -> None:
    args = build_config_command(
        url=URL, token=fake_token(), name="  box  ", labels=["a", "A", "self-hosted", " b "]
    )
    assert args[args.index("--name") + 1] == "box"
    assert args[args.index("--labels") + 1] == "a,b"


def test_build_config_command_accepts_comma_string_labels() -> None:
    args = build_config_command(url=URL, token=fake_token(), name="box", labels="x, y")
    assert args[args.index("--labels") + 1] == "x,y"


def test_build_config_command_options() -> None:
    args = build_config_command(
        url=URL, token=fake_token(), name="box", labels=[], work_folder="w", replace=False
    )
    assert args[-2:] == ["--work", "w"]
    assert "--replace" not in args


@pytest.mark.parametrize(
    "name", ["a&b", "a|b", "a%PATH%", "a^b", "a b", "a>b", "a<b", '"a"', "a;b", "-a", ""]
)
def test_build_config_command_rejects_unsafe_name(name: str) -> None:
    with pytest.raises(InvalidInputError):
        build_config_command(url=URL, token=fake_token(), name=name, labels=[])


@pytest.mark.parametrize("label", ["a&b", "x|y", "%TEMP%", "a^", "two words", "a>b"])
def test_build_config_command_rejects_unsafe_label(label: str) -> None:
    with pytest.raises(InvalidInputError):
        build_config_command(url=URL, token=fake_token(), name="box", labels=["ok", label])


@pytest.mark.parametrize("suffix", ["&calc", "|x", "%X%", "^", " x", "<x"])
def test_build_config_command_rejects_unsafe_token_without_echo(suffix: str) -> None:
    token = fake_token() + suffix
    with pytest.raises(InvalidInputError) as info:
        build_config_command(url=URL, token=token, name="box", labels=[])
    assert token not in str(info.value)
    assert "FAKE" not in str(info.value)  # not even a prefix of the token is echoed


def test_build_config_command_rejects_short_token() -> None:
    with pytest.raises(InvalidInputError):
        build_config_command(url=URL, token="short", name="box", labels=[])


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r&calc",
        "https://github.com/o/r|x",
        "https://github.com/o/%r%",
        "https://github.com/o/r x",
        "ftp://github.com/o/r",
        "github.com/o/r",
        "https://github.com",
    ],
)
def test_build_config_command_rejects_unsafe_url(url: str) -> None:
    with pytest.raises(InvalidInputError):
        build_config_command(url=url, token=fake_token(), name="box", labels=[])


def test_build_config_command_accepts_ghes_url_with_port() -> None:
    url = "https://ghe.example.com:8443/team/repo.x/"
    args = build_config_command(url=url, token=fake_token(), name="box", labels=[])
    assert args[args.index("--url") + 1] == url


@pytest.mark.parametrize("work", ["", "a b", "w&x", "%TEMP%"])
def test_build_config_command_rejects_unsafe_work_folder(work: str) -> None:
    with pytest.raises(InvalidInputError):
        build_config_command(url=URL, token=fake_token(), name="box", labels=[], work_folder=work)


def test_build_config_command_registers_token_for_redaction() -> None:
    token = fake_token()
    build_config_command(url=URL, token=token, name="box", labels=[])
    assert redact(f"echo {token}") == f"echo {MASK}"


def test_build_remove_command() -> None:
    token = fake_token()
    assert build_remove_command(token=token) == [
        winproc.cmd_exe(), "/c", "config.cmd", "remove", "--token", token,
    ]  # fmt: skip


def test_config_commands_use_winproc_cmd_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    # A bare "cmd.exe" is looked up in our current directory before System32.
    absolute = "X:\\Windows\\System32\\cmd.exe"
    monkeypatch.setattr(winproc, "cmd_exe", lambda: absolute)
    token = fake_token()
    assert build_config_command(url=URL, token=token, name="box", labels=[])[:3] == [
        absolute, "/c", "config.cmd",
    ]  # fmt: skip
    assert build_remove_command(token=token)[:3] == [absolute, "/c", "config.cmd"]


# --- configure / unconfigure -----------------------------------------------------------------


def test_configure_success_with_real_output(runner_dir: Path) -> None:
    token = fake_token()
    run = FakeRun(CONFIG_OK)
    result = configure(runner_dir, url=URL, token=token, name="box", labels=["gpu"], run=run)

    assert isinstance(result, CommandResult)
    assert result.returncode == 0
    assert "Runner successfully added" in result.output
    assert "Settings Saved." in result.output
    assert "Self-hosted runner registration" in result.output  # banner kept for display
    assert "\r" not in result.output
    assert not result.output.startswith("\n") and not result.output.endswith("\n")

    [(args, kwargs)] = run.calls
    assert args == build_config_command(url=URL, token=token, name="box", labels=["gpu"])
    assert kwargs == {
        "cwd": runner_dir,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "timeout": CONFIG_TIMEOUT_S,
        "creationflags": winproc.CREATE_NO_WINDOW if winproc.IS_WINDOWS else 0,
        "env": winproc.child_environment(),
    }


def test_configure_child_env_drops_pat_and_keeps_the_rest(
    runner_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("RUNNERDECK_PAT", "RUNNERDECK_TEST_PAT", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.setenv(name, "ghp_" + "x" * 36)
    monkeypatch.setenv("RD_INHERITED", "kept")
    run = FakeRun(CONFIG_OK)
    configure(runner_dir, url=URL, token=fake_token(), name="box", labels=[], run=run)
    child_env = run.calls[0][1]["env"]
    assert child_env["RD_INHERITED"] == "kept"
    for name in ("RUNNERDECK_PAT", "RUNNERDECK_TEST_PAT", "GH_TOKEN", "GITHUB_TOKEN"):
        assert name not in child_env


def test_configure_and_unconfigure_pass_env_overrides(
    runner_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNNERDECK_PAT", "ghp_" + "x" * 36)
    monkeypatch.setenv("RD_INHERITED", "kept")
    overrides = {"https_proxy": PROXY, "http_proxy": PROXY}
    configure_run, remove_run = FakeRun(CONFIG_OK), FakeRun(REMOVE_OK)
    configure(
        runner_dir, url=URL, token=fake_token(), name="box", labels=[], run=configure_run,
        env=overrides,
    )  # fmt: skip
    unconfigure(runner_dir, token=fake_token(), run=remove_run, env=overrides)
    for run in (configure_run, remove_run):
        child_env = run.calls[0][1]["env"]
        assert child_env["https_proxy"] == PROXY
        assert child_env["http_proxy"] == PROXY
        assert child_env["RD_INHERITED"] == "kept"
        assert "RUNNERDECK_PAT" not in child_env


def test_configure_env_override_replaces_other_spelling_on_windows(
    runner_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(winproc, "IS_WINDOWS", True)
    monkeypatch.setenv("HTTPS_PROXY", "http://stale:1")
    run = FakeRun(CONFIG_OK)
    configure(
        runner_dir, url=URL, token=fake_token(), name="box", labels=[], run=run,
        env={"https_proxy": PROXY},
    )  # fmt: skip
    child_env = run.calls[0][1]["env"]
    assert [(k, v) for k, v in child_env.items() if k.upper() == "HTTPS_PROXY"] == [
        ("https_proxy", PROXY)
    ]


def test_configure_env_reaches_a_real_child(
    runner_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNNERDECK_PAT", "ghp_" + "y" * 36)
    code = (
        "import os; "
        "print('proxy=' + os.environ.get('https_proxy', '-')); "
        "print('pat=' + str('RUNNERDECK_PAT' in os.environ)); "
        "print('Runner successfully added'); print('Settings Saved.')"
    )

    def via_python(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return run_command([sys.executable, "-c", code], **kwargs)

    result = configure(
        runner_dir, url=URL, token=fake_token(), name="box", labels=[], run=via_python,
        timeout=60, env={"https_proxy": PROXY},
    )  # fmt: skip
    assert f"proxy={PROXY}" in result.output
    assert "pat=False" in result.output


def test_configure_uses_create_no_window_on_windows(
    runner_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(winproc, "IS_WINDOWS", True)
    run = FakeRun(CONFIG_OK)
    configure(runner_dir, url=URL, token=fake_token(), name="box", labels=[], run=run, timeout=5)
    kwargs = run.calls[0][1]
    assert kwargs["creationflags"] == winproc.CREATE_NO_WINDOW
    assert kwargs["timeout"] == 5


def test_configure_registers_token_and_redacts_output(runner_dir: Path) -> None:
    token = fake_token()
    echoed = CONFIG_OK + f"debug: using {token}\r\r\n".encode()
    result = configure(runner_dir, url=URL, token=token, name="box", labels=[], run=FakeRun(echoed))
    assert token not in result.output
    assert f"debug: using {MASK}" in result.output
    assert redact(token) == MASK


def test_configure_decodes_lines_that_are_not_utf8(runner_dir: Path) -> None:
    gbk_line = "已复制         1 个文件。".encode("gbk") + b"\r\n"
    result = configure(
        runner_dir,
        url=URL,
        token=fake_token(),
        name="box",
        labels=[],
        run=FakeRun(gbk_line + CONFIG_OK),
    )
    lines = result.output.splitlines()
    assert lines[0].strip()  # decoded with the console code page fallback, not dropped
    assert "Runner successfully added" in result.output


def test_configure_nonzero_exit_raises_with_redacted_output(runner_dir: Path) -> None:
    token = fake_token()
    out = (
        b"# Authentication\r\r\n\r\r\n"
        + f"Http response code: NotFound for token {token}\r\r\n".encode()
        + b"Response status code does not indicate success: 404 (Not Found).\r\r\n"
    )
    with pytest.raises(InstallError) as info:
        configure(
            runner_dir, url=URL, token=token, name="box", labels=[], run=FakeRun(out, returncode=1)
        )
    err = info.value
    assert err.returncode == 1
    assert "退出码 1" in str(err)
    assert "404 (Not Found)" in str(err)  # last non-empty line explains the failure
    assert "Http response code: NotFound" in err.output
    assert token not in str(err)
    assert token not in err.output


def test_configure_nonzero_exit_without_output(runner_dir: Path) -> None:
    with pytest.raises(InstallError) as info:
        configure(
            runner_dir, url=URL, token=fake_token(), name="box", labels=[],
            run=FakeRun(b"", returncode=2),
        )  # fmt: skip
    assert info.value.returncode == 2
    assert info.value.output == ""
    assert str(info.value).endswith("）")


def test_configure_exit_zero_without_success_lines_fails(runner_dir: Path) -> None:
    banner_only = CONFIG_OK.split(b"# Runner Registration")[0]
    with pytest.raises(InstallError) as info:
        configure(
            runner_dir, url=URL, token=fake_token(), name="box", labels=[], run=FakeRun(banner_only)
        )
    assert info.value.returncode == 0
    assert "Runner successfully added" in str(info.value)
    assert "Settings Saved" in str(info.value)
    assert "Connected to GitHub" in info.value.output


def test_configure_requires_settings_saved_too(runner_dir: Path) -> None:
    partial = CONFIG_OK.split(b"# Runner settings")[0]
    with pytest.raises(InstallError) as info:
        configure(
            runner_dir, url=URL, token=fake_token(), name="box", labels=[], run=FakeRun(partial)
        )
    assert "Settings Saved" in str(info.value)
    assert "Runner successfully added" not in str(info.value)


def test_configure_rejects_remove_output(runner_dir: Path) -> None:
    with pytest.raises(InstallError):
        configure(
            runner_dir, url=URL, token=fake_token(), name="box", labels=[], run=FakeRun(REMOVE_OK)
        )


def test_configure_timeout_raises_without_leaking_token(runner_dir: Path) -> None:
    token = fake_token()
    args = build_config_command(url=URL, token=token, name="box", labels=[])
    exc = subprocess.TimeoutExpired(args, 3, output=b"\r\r\n# Authentication\r\r\n")
    assert token in str(exc)  # the reason the installer must not chain it
    with pytest.raises(InstallError) as info:
        configure(
            runner_dir, url=URL, token=token, name="box", labels=[], run=FakeRun(exc=exc), timeout=3
        )
    err = info.value
    assert "超时" in str(err)
    assert "3 秒" in str(err)
    assert err.returncode is None
    assert err.output == "# Authentication"
    assert err.__cause__ is None and err.__suppress_context__
    assert token not in str(err)


def test_configure_timeout_without_output(runner_dir: Path) -> None:
    exc = subprocess.TimeoutExpired(["cmd.exe"], 1)
    with pytest.raises(InstallError) as info:
        configure(
            runner_dir, url=URL, token=fake_token(), name="box", labels=[], run=FakeRun(exc=exc)
        )
    assert info.value.output == ""


def test_configure_oserror_becomes_install_error(runner_dir: Path) -> None:
    exc = FileNotFoundError(2, "No such file or directory", "cmd.exe")
    with pytest.raises(InstallError) as info:
        configure(
            runner_dir, url=URL, token=fake_token(), name="box", labels=[], run=FakeRun(exc=exc)
        )
    assert "无法启动 config.cmd" in str(info.value)
    assert isinstance(info.value.__cause__, FileNotFoundError)


def test_configure_requires_config_cmd(tmp_path: Path) -> None:
    run = FakeRun(CONFIG_OK)
    with pytest.raises(InstallError, match="config.cmd"):
        configure(tmp_path, url=URL, token=fake_token(), name="box", labels=[], run=run)
    assert run.calls == []


def test_configure_invalid_name_does_not_run(runner_dir: Path) -> None:
    run = FakeRun(CONFIG_OK)
    with pytest.raises(InvalidInputError):
        configure(runner_dir, url=URL, token=fake_token(), name="a&b", labels=[], run=run)
    assert run.calls == []


def test_unconfigure_success_with_real_output(runner_dir: Path) -> None:
    token = fake_token()
    run = FakeRun(REMOVE_OK)
    result = unconfigure(runner_dir, token=token, run=run, timeout=7)
    assert result.returncode == 0
    assert "Runner removed successfully" in result.output
    [(args, kwargs)] = run.calls
    assert args == [winproc.cmd_exe(), "/c", "config.cmd", "remove", "--token", token]
    assert kwargs["cwd"] == runner_dir
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["timeout"] == 7
    assert redact(token) == MASK


def test_unconfigure_exit_zero_without_success_line_fails(runner_dir: Path) -> None:
    with pytest.raises(InstallError) as info:
        unconfigure(runner_dir, token=fake_token(), run=FakeRun(CONFIG_OK))
    assert "Runner removed successfully" in str(info.value)
    assert info.value.returncode == 0


def test_unconfigure_nonzero_exit(runner_dir: Path) -> None:
    token = fake_token()
    out = f"Failed: config.cmd remove --token {token}\r\r\n".encode()
    with pytest.raises(InstallError) as info:
        unconfigure(runner_dir, token=token, run=FakeRun(out, returncode=1))
    assert info.value.returncode == 1
    assert token not in str(info.value)
    assert token not in info.value.output
    assert "runner 注销失败" in str(info.value)


def test_unconfigure_timeout(runner_dir: Path) -> None:
    exc = subprocess.TimeoutExpired(["cmd.exe"], CONFIG_TIMEOUT_S)
    with pytest.raises(InstallError, match="超时"):
        unconfigure(runner_dir, token=fake_token(), run=FakeRun(exc=exc))


def test_unconfigure_rejects_unsafe_token(runner_dir: Path) -> None:
    run = FakeRun(REMOVE_OK)
    with pytest.raises(InvalidInputError):
        unconfigure(runner_dir, token="AAAAAAAAAA&del", run=run)
    assert run.calls == []


# --- run_command (real subprocesses, any OS) -------------------------------------------------


def test_run_command_collects_output() -> None:
    code = "import sys; print('hello'); sys.stderr.write('err\\n'); sys.exit(3)"
    proc = run_command(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    assert proc.returncode == 3
    assert b"hello" in proc.stdout
    assert b"err" in proc.stdout


def test_run_command_timeout_kills_grandchild_holding_the_pipe() -> None:
    # The child starts a grandchild that inherits stdout (like cmd.exe -> Runner.Listener.exe).
    code = textwrap.dedent(
        """
        import subprocess, sys, time
        g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        print("grandchild", g.pid, flush=True)
        time.sleep(60)
        """
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as info:
        run_command(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=2,
        )
    assert time.monotonic() - started < 30
    output = info.value.output or b""
    assert b"grandchild" in output
    pid = int(output.split()[1])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.1)
    else:
        pytest.fail("grandchild survived the timeout")


class _FakePopen:
    """Popen double whose communicate() raises the scripted exceptions in order."""

    script: list[BaseException] = []
    instances: list[_FakePopen] = []
    killed_trees: list[int] = []

    def __init__(self, args: list[str], **kwargs: Any) -> None:
        self.args = args
        self.pid = 999_999_999
        self.returncode: int | None = None
        self.killed = 0
        type(self).instances.append(self)

    def communicate(self, timeout: float | None = None) -> tuple[bytes, None]:
        if type(self).script:
            raise type(self).script.pop(0)
        return b"late", None

    def kill(self) -> None:
        self.killed += 1

    def wait(self, timeout: float | None = None) -> int:
        return -9


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch) -> type[_FakePopen]:
    monkeypatch.setattr(_FakePopen, "script", [])
    monkeypatch.setattr(_FakePopen, "instances", [])
    monkeypatch.setattr(_FakePopen, "killed_trees", [])
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(
        winproc, "kill_tree", lambda pid, **kw: _FakePopen.killed_trees.append(pid) or []
    )
    return _FakePopen


def test_run_command_gives_up_draining_when_pipe_stays_open(fake_popen: type[_FakePopen]) -> None:
    fake_popen.script = [
        subprocess.TimeoutExpired(["x"], 1, output=b"partial"),
        subprocess.TimeoutExpired(["x"], 5),
    ]
    with pytest.raises(subprocess.TimeoutExpired) as info:
        run_command(["x"], timeout=1)
    assert info.value.output == b"partial"
    assert info.value.timeout == 1
    [proc] = fake_popen.instances
    assert proc.killed == 1
    assert fake_popen.killed_trees == [proc.pid]


def test_run_command_returns_drained_output_after_timeout(fake_popen: type[_FakePopen]) -> None:
    fake_popen.script = [subprocess.TimeoutExpired(["x"], 1)]
    with pytest.raises(subprocess.TimeoutExpired) as info:
        run_command(["x"], timeout=1)
    assert info.value.output == b"late"


def test_run_command_kills_tree_on_interrupt(fake_popen: type[_FakePopen]) -> None:
    fake_popen.script = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        run_command(["x"], timeout=1)
    [proc] = fake_popen.instances
    assert proc.killed == 1
    assert fake_popen.killed_trees == [proc.pid]


# --- extract_runner ----------------------------------------------------------------------------


def test_extract_runner_into_new_dir(tmp_path: Path) -> None:
    zip_path = make_zip(tmp_path / "runner.zip", RUNNER_MEMBERS)
    target = tmp_path / "runners" / "sandbox"
    extract_runner(zip_path, target)
    for name, data in RUNNER_MEMBERS.items():
        assert (target / name).read_bytes() == data


def test_extract_runner_into_existing_empty_dir(tmp_path: Path) -> None:
    zip_path = make_zip(tmp_path / "runner.zip", RUNNER_MEMBERS)
    target = tmp_path / "t"
    target.mkdir()
    extract_runner(zip_path, target)
    assert (target / "run.cmd").is_file()


def test_extract_runner_rejects_non_empty_target(tmp_path: Path) -> None:
    zip_path = make_zip(tmp_path / "runner.zip", RUNNER_MEMBERS)
    target = tmp_path / "t"
    target.mkdir()
    (target / "keep.txt").write_text("mine")
    with pytest.raises(InstallError, match="不为空"):
        extract_runner(zip_path, target)
    assert sorted(p.name for p in target.iterdir()) == ["keep.txt"]


def test_extract_runner_rejects_file_target(tmp_path: Path) -> None:
    zip_path = make_zip(tmp_path / "runner.zip", RUNNER_MEMBERS)
    target = tmp_path / "t"
    target.write_text("x")
    with pytest.raises(InstallError, match="不是目录"):
        extract_runner(zip_path, target)


@pytest.mark.parametrize(
    "evil",
    [
        "../evil.txt",
        "bin/../../evil.txt",
        "..\\evil.txt",
        "/abs/evil.txt",
        "\\abs\\evil.txt",
        "C:/evil.txt",
        "c:evil.txt",
    ],
)
def test_extract_runner_rejects_unsafe_member(tmp_path: Path, evil: str) -> None:
    # The evil member comes last: validation must happen before anything is written.
    zip_path = make_zip(tmp_path / "runner.zip", {**RUNNER_MEMBERS, evil: b"pwned"})
    target = tmp_path / "work" / "t"
    with pytest.raises(InstallError, match="不安全的路径"):
        extract_runner(zip_path, target)
    assert not target.exists()
    assert not (tmp_path / "work" / "evil.txt").exists()
    assert not (tmp_path / "evil.txt").exists()


def test_extract_runner_rejects_unsafe_member_keeps_empty_target(tmp_path: Path) -> None:
    zip_path = make_zip(tmp_path / "runner.zip", {"../evil.txt": b"x", **RUNNER_MEMBERS})
    target = tmp_path / "t"
    target.mkdir()
    with pytest.raises(InstallError):
        extract_runner(zip_path, target)
    assert target.is_dir() and not any(target.iterdir())


def _corrupt_last_member(zip_path: Path, marker: bytes) -> None:
    data = zip_path.read_bytes()
    pos = data.rindex(marker)
    zip_path.write_bytes(data[:pos] + b"X" + data[pos + 1 :])


def test_extract_runner_cleans_up_partial_extraction_in_new_dir(tmp_path: Path) -> None:
    members = {**RUNNER_MEMBERS, "bin/zzz.dat": b"payload-" * 100}
    zip_path = make_zip(tmp_path / "runner.zip", members, stored=True)
    _corrupt_last_member(zip_path, b"payload-")  # CRC check fails on the last member
    target = tmp_path / "t"
    with pytest.raises(InstallError, match="解压 runner 失败") as info:
        extract_runner(zip_path, target)
    assert isinstance(info.value.__cause__, zipfile.BadZipFile)
    assert not target.exists()


def test_extract_runner_cleans_up_partial_extraction_in_existing_dir(tmp_path: Path) -> None:
    members = {**RUNNER_MEMBERS, "bin/zzz.dat": b"payload-" * 100}
    zip_path = make_zip(tmp_path / "runner.zip", members, stored=True)
    _corrupt_last_member(zip_path, b"payload-")
    target = tmp_path / "t"
    target.mkdir()
    with pytest.raises(InstallError):
        extract_runner(zip_path, target)
    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_extract_runner_cleanup_failure_keeps_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zip_path = make_zip(tmp_path / "x.zip", {"readme": b""})
    warnings: list[tuple[Any, ...]] = []

    def locked(path: Path) -> None:
        raise PermissionError(32, "being used by another process")

    monkeypatch.setattr(installer, "_rmtree", locked)
    monkeypatch.setattr(installer.log, "warning", lambda *args, **kw: warnings.append(args))
    with pytest.raises(InstallError, match="不是 runner"):
        extract_runner(zip_path, tmp_path / "t")
    assert warnings and "could not clean up" in warnings[0][0]


def test_extract_runner_requires_runner_layout(tmp_path: Path) -> None:
    zip_path = make_zip(tmp_path / "x.zip", {"actions-runner/config.cmd": b"", "readme": b""})
    target = tmp_path / "t"
    with pytest.raises(InstallError, match="不是 runner"):
        extract_runner(zip_path, target)
    assert not target.exists()


def test_extract_runner_bad_zip(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip at all")
    with pytest.raises(InstallError, match="无法打开"):
        extract_runner(bad, tmp_path / "t")
    assert not (tmp_path / "t").exists()


def test_extract_runner_missing_zip(tmp_path: Path) -> None:
    with pytest.raises(InstallError, match="无法打开"):
        extract_runner(tmp_path / "missing.zip", tmp_path / "t")


@pytest.mark.parametrize("existing", [False, True], ids=["new-dir", "empty-dir"])
def test_extract_runner_cleans_up_after_ctrl_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    zip_path = make_zip(tmp_path / "runner.zip", RUNNER_MEMBERS)
    target = tmp_path / "t"
    if existing:
        target.mkdir()
    written: list[str] = []

    def interrupted(self: zipfile.ZipFile, path: Any = None, *args: Any, **kwargs: Any) -> None:
        for info in self.infolist()[:2]:
            written.append(self.extract(info, path))
        raise KeyboardInterrupt

    monkeypatch.setattr(zipfile.ZipFile, "extractall", interrupted)
    with pytest.raises(KeyboardInterrupt):
        extract_runner(zip_path, target)
    assert len(written) == 2  # files were on disk when Ctrl+C arrived
    if existing:
        assert target.is_dir() and list(target.iterdir()) == []
    else:
        assert not target.exists()


@pytest.mark.parametrize("existing", [False, True], ids=["new-dir", "empty-dir"])
def test_extract_runner_hardens_target_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    zip_path = make_zip(tmp_path / "runner.zip", RUNNER_MEMBERS)
    target = tmp_path / "runners" / "t"
    if existing:
        target.mkdir(parents=True)
    seen: list[tuple[Path, bool, list[Path]]] = []

    def spy(path: Path, **kwargs: Any) -> bool:
        seen.append((path, path.is_dir(), list(path.iterdir())))
        return True

    monkeypatch.setattr(installer, "harden_runner_dir", spy)
    extract_runner(zip_path, target)
    assert seen == [(target, True, [])]
    assert (target / "run.cmd").is_file()


def test_extract_runner_continues_when_hardening_fails(
    tmp_path: Path, fake_windows: None, logged_warnings: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    zip_path = make_zip(tmp_path / "runner.zip", RUNNER_MEMBERS)
    target = tmp_path / "t"
    run = ScriptedRun((0, WHOAMI_OUT), (5, b"Access is denied.\r\n"))
    monkeypatch.setattr(
        installer, "harden_runner_dir", functools.partial(installer.harden_runner_dir, run=run)
    )
    extract_runner(zip_path, target)
    for name, data in RUNNER_MEMBERS.items():
        assert (target / name).read_bytes() == data
    assert len(run.calls) == 2
    assert any("Access is denied." in w for w in logged_warnings)


# --- harden_runner_dir (fake whoami / icacls; real ones in the Windows section) ----------------

WHOAMI_OUT = b'"desktop-box\\alice","S-1-5-21-1111-2222-3333-1001"\r\n'
USER_SID = "S-1-5-21-1111-2222-3333-1001"
FAKE_SYSTEM_ROOT = "C:\\Windows"


class ScriptedRun:
    """Stands in for run_command: one scripted (returncode, stdout) or exception per call."""

    def __init__(self, *results: tuple[int, bytes] | BaseException) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        returncode, stdout = result
        return subprocess.CompletedProcess(args, returncode, stdout, None)


@pytest.fixture
def fake_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winproc, "IS_WINDOWS", True)
    monkeypatch.setenv("SYSTEMROOT", FAKE_SYSTEM_ROOT)


@pytest.fixture
def logged_warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        installer.log, "warning", lambda msg, *args, **kwargs: seen.append(msg % args)
    )
    return seen


def _system32(name: str) -> str:
    return os.path.join(FAKE_SYSTEM_ROOT, "System32", name)


def test_harden_runner_dir_grants_only_user_system_and_admins(
    tmp_path: Path, fake_windows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNNERDECK_PAT", "ghp_" + "x" * 36)
    run = ScriptedRun((0, WHOAMI_OUT), (0, b""), (0, b""))
    assert installer.harden_runner_dir(tmp_path, run=run) is True
    assert [args for args, _ in run.calls] == [
        [_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"],
        [_system32("icacls.exe"), str(tmp_path), "/reset", "/q"],
        [
            _system32("icacls.exe"), str(tmp_path), "/inheritance:r", "/grant:r",
            f"*{USER_SID}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "/q",
        ],
    ]  # fmt: skip
    for _, kwargs in run.calls:
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["creationflags"] == winproc.CREATE_NO_WINDOW
        assert 0 < kwargs["timeout"] <= 60
        assert "RUNNERDECK_PAT" not in kwargs["env"]


def test_harden_runner_dir_is_a_noop_off_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(winproc, "IS_WINDOWS", False)
    run = ScriptedRun()
    assert installer.harden_runner_dir(tmp_path, run=run) is False
    assert run.calls == []


@pytest.mark.parametrize(
    ("script", "calls", "logged"),
    [
        ((FileNotFoundError(2, "No such file", "whoami.exe"),), 1, "No such file"),
        (((1, b"ERROR: access denied\r\n"),), 1, "ERROR: access denied"),
        (((0, b"INFO: no output\r\n"),), 1, "no SID"),
        (((0, WHOAMI_OUT), (5, b"Access is denied.\r\n")), 2, "Access is denied."),
        (((0, WHOAMI_OUT), (0, b""), (1332, b"No mapping\r\n")), 3, "No mapping"),
        (((0, WHOAMI_OUT), (0, b""), subprocess.TimeoutExpired(["icacls"], 30)), 3, "timed out"),
    ],
    ids=["whoami-missing", "whoami-fails", "whoami-no-sid", "reset-fails", "grant-fails", "hang"],
)
def test_harden_runner_dir_failure_only_warns(
    tmp_path: Path, fake_windows: None, logged_warnings: list[str],
    script: tuple[Any, ...], calls: int, logged: str,
) -> None:  # fmt: skip
    run = ScriptedRun(*script)
    assert installer.harden_runner_dir(tmp_path, run=run) is False
    assert len(run.calls) == calls
    assert len(logged_warnings) == 1
    assert "not restricting runner dir ACL" in logged_warnings[0]
    assert logged in logged_warnings[0]


def test_harden_root_dir_adds_list_only_access_for_authenticated_users(
    tmp_path: Path, fake_windows: None
) -> None:
    run = ScriptedRun((0, WHOAMI_OUT), (0, b""), (0, b""))
    assert installer.harden_root_dir(tmp_path, run=run) is True
    assert run.calls[2][0] == [
        _system32("icacls.exe"), str(tmp_path), "/inheritance:r", "/grant:r",
        f"*{USER_SID}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
        "*S-1-5-11:(RX)", "/q",
    ]  # fmt: skip


def test_harden_root_dir_failure_names_the_root(
    tmp_path: Path, fake_windows: None, logged_warnings: list[str]
) -> None:
    run = ScriptedRun((0, WHOAMI_OUT), (5, b"Access is denied.\r\n"))
    assert installer.harden_root_dir(tmp_path, run=run) is False
    assert logged_warnings and "not restricting runners root ACL" in logged_warnings[0]


def test_harden_root_dir_is_a_noop_off_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(winproc, "IS_WINDOWS", False)
    run = ScriptedRun()
    assert installer.harden_root_dir(tmp_path, run=run) is False
    assert run.calls == []


@pytest.mark.parametrize(
    ("candidate", "refused"),
    [
        ("runners", False),
        ("home/me/runners", False),
        ("home/me", True),  # the profile itself
        ("home", True),  # contains the profile
        ("sys/win", True),
        ("sys/win/Temp/runners", True),
        ("data/RunnerDeck", True),
        ("sys", True),  # contains SystemRoot
    ],
)
def test_root_hardening_refusal(tmp_path: Path, candidate: str, refused: bool) -> None:
    environ = {
        "USERPROFILE": str(tmp_path / "home/me"),
        "SystemRoot": str(tmp_path / "sys/win"),
        "ProgramData": str(tmp_path / "data"),
    }
    reason = installer.root_hardening_refusal(tmp_path / candidate, environ=environ)
    assert (reason is not None) is refused, reason


def test_root_hardening_refusal_drive_root_and_relative() -> None:
    assert "整个磁盘" in (installer.root_hardening_refusal(Path(Path.cwd().anchor)) or "")
    assert "绝对路径" in (installer.root_hardening_refusal(Path("runners")) or "")


# --- read_env_values -----------------------------------------------------------------------


def test_read_env_values(tmp_path: Path) -> None:
    assert installer.read_env_values(tmp_path) == {}
    (tmp_path / ".env").write_bytes(
        "\ufeffHTTPS_PROXY=http://a:1\r\nnot a pair\r\nhttps_proxy=http://b:2\r\nX=\r\n".encode()
    )
    assert installer.read_env_values(tmp_path) == {"https_proxy": "http://a:1", "x": ""}


def test_read_env_values_unreadable(tmp_path: Path) -> None:
    (tmp_path / ".env").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(InstallError, match="无法读取"):
        installer.read_env_values(tmp_path)


# --- write_env_file ----------------------------------------------------------------------------


def read_env(runner_dir: Path) -> str:
    return (runner_dir / ".env").read_bytes().decode("utf-8")


def test_write_env_file_creates_file(runner_dir: Path) -> None:
    write_env_file(runner_dir, {"PSExecutionPolicyPreference": "RemoteSigned", "B": "1"})
    assert read_env(runner_dir) == "PSExecutionPolicyPreference=RemoteSigned\nB=1\n"


def test_write_env_file_merges_deletes_and_keeps_order(runner_dir: Path) -> None:
    (runner_dir / ".env").write_bytes(b"A=1\n# comment\nB=2\n\nnot a pair\nC=3\n=odd\n")
    write_env_file(runner_dir, {"B": "20", "C": None, "D": "4", "MISSING": None})
    assert read_env(runner_dir) == "A=1\n# comment\nB=20\n\nnot a pair\n=odd\nD=4\n"


def test_write_env_file_keys_are_case_insensitive_and_dedup(runner_dir: Path) -> None:
    (runner_dir / ".env").write_bytes(b"https_proxy=old\nX=1\nHTTPS_PROXY=older\nx=2\n")
    write_env_file(runner_dir, {"HTTPS_PROXY": "http://proxy:8080"})
    assert read_env(runner_dir) == "HTTPS_PROXY=http://proxy:8080\nX=1\nx=2\n"


def test_write_env_file_delete_removes_all_duplicates(runner_dir: Path) -> None:
    (runner_dir / ".env").write_bytes(b"A=1\nB=2\na=3\n")
    write_env_file(runner_dir, {"A": None})
    assert read_env(runner_dir) == "B=2\n"


def test_write_env_file_preserves_crlf_bom_and_equals_in_value(runner_dir: Path) -> None:
    (runner_dir / ".env").write_bytes("\ufeffLANG=C\r\nA=1\r\n".encode())
    write_env_file(runner_dir, {"A": "x=y=z"})
    assert read_env(runner_dir) == "LANG=C\r\nA=x=y=z\r\n"


def test_write_env_file_without_trailing_newline(runner_dir: Path) -> None:
    (runner_dir / ".env").write_bytes(b"A=1")
    write_env_file(runner_dir, {"B": "2"})
    assert read_env(runner_dir) == "A=1\nB=2\n"


def test_write_env_file_deleting_last_key_leaves_empty_file(runner_dir: Path) -> None:
    (runner_dir / ".env").write_bytes(b"A=1\n")
    write_env_file(runner_dir, {"A": None})
    assert read_env(runner_dir) == ""


def test_write_env_file_noop_cases_do_not_create_file(runner_dir: Path) -> None:
    write_env_file(runner_dir, {})
    write_env_file(runner_dir, {"A": None})
    assert not (runner_dir / ".env").exists()


def test_write_env_file_empty_value(runner_dir: Path) -> None:
    write_env_file(runner_dir, {"A": ""})
    assert read_env(runner_dir) == "A=\n"


@pytest.mark.parametrize("key", ["", "A=B", "A B", "1A", "A-B", "A\nB"])
def test_write_env_file_rejects_bad_key(runner_dir: Path, key: str) -> None:
    (runner_dir / ".env").write_bytes(b"A=1\n")
    with pytest.raises(InvalidInputError):
        write_env_file(runner_dir, {"OK": "1", key: "v"})
    assert read_env(runner_dir) == "A=1\n"


@pytest.mark.parametrize("value", ["a\nB=2", "a\r", "\r\n"])
def test_write_env_file_rejects_newline_in_value(runner_dir: Path, value: str) -> None:
    with pytest.raises(InvalidInputError):
        write_env_file(runner_dir, {"A": value})
    assert not (runner_dir / ".env").exists()


def test_write_env_file_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(InstallError, match="不存在"):
        write_env_file(tmp_path / "nope", {"A": "1"})


def test_write_env_file_is_atomic(runner_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (runner_dir / ".env").write_bytes(b"A=1\n")

    def failing_replace(src: Any, dst: Any) -> None:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(installer.os, "replace", failing_replace)
    with pytest.raises(InstallError, match="无法写入"):
        write_env_file(runner_dir, {"A": "2"})
    monkeypatch.undo()
    assert read_env(runner_dir) == "A=1\n"
    assert sorted(p.name for p in runner_dir.iterdir()) == [".env", "config.cmd", "run.cmd"]


def test_write_env_file_unreadable_existing(runner_dir: Path) -> None:
    (runner_dir / ".env").write_bytes(b"A=\xff\xfe\n")
    with pytest.raises(InstallError, match="无法读取"):
        write_env_file(runner_dir, {"B": "1"})


# --- remove_runner_dir -------------------------------------------------------------------------


def populate(runner_dir: Path) -> None:
    (runner_dir / "bin").mkdir()
    (runner_dir / "bin" / "Runner.Listener.exe").write_bytes(b"MZ")
    (runner_dir / ".runner").write_text("{}")
    work = runner_dir / "_work" / "repo" / "repo"
    work.mkdir(parents=True)
    (work / "build.log").write_text("log")


def test_remove_runner_dir_removes_everything(runner_dir: Path) -> None:
    populate(runner_dir)
    remove_runner_dir(runner_dir)
    assert not runner_dir.exists()
    assert runner_dir.parent.is_dir()


def test_remove_runner_dir_keep_work(runner_dir: Path) -> None:
    populate(runner_dir)
    remove_runner_dir(runner_dir, keep_work=True)
    assert [p.name for p in runner_dir.iterdir()] == ["_work"]
    assert (runner_dir / "_work" / "repo" / "repo" / "build.log").read_text() == "log"


def test_remove_runner_dir_keep_work_without_work_dir(runner_dir: Path) -> None:
    remove_runner_dir(runner_dir, keep_work=True)
    assert runner_dir.is_dir() and list(runner_dir.iterdir()) == []


@pytest.mark.parametrize("present", [[], ["config.cmd"], ["run.cmd"], ["other.txt"]])
def test_remove_runner_dir_refuses_non_runner_dir(tmp_path: Path, present: list[str]) -> None:
    d = tmp_path / "precious"
    d.mkdir()
    for name in present:
        (d / name).write_text("x")
    with pytest.raises(InstallError, match="不像 runner 目录"):
        remove_runner_dir(d)
    assert sorted(p.name for p in d.iterdir()) == sorted(present)


def test_remove_runner_dir_refuses_file(tmp_path: Path) -> None:
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(InstallError):
        remove_runner_dir(f)
    assert f.exists()


def test_remove_runner_dir_refuses_symlink(runner_dir: Path, tmp_path: Path) -> None:
    link = tmp_path / "link"
    try:
        link.symlink_to(runner_dir, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks here")
    with pytest.raises(InstallError, match="符号链接"):
        remove_runner_dir(link)
    assert (runner_dir / "config.cmd").exists()


def test_remove_runner_dir_missing_is_noop(tmp_path: Path) -> None:
    remove_runner_dir(tmp_path / "gone")


def test_remove_runner_dir_read_only_file(runner_dir: Path) -> None:
    populate(runner_dir)
    ro = runner_dir / "bin" / "Runner.Listener.exe"
    os.chmod(ro, stat.S_IREAD)
    os.chmod(runner_dir / "config.cmd", stat.S_IREAD)
    remove_runner_dir(runner_dir)
    assert not runner_dir.exists()


def test_remove_runner_dir_read_only_file_keep_work(runner_dir: Path) -> None:
    populate(runner_dir)
    os.chmod(runner_dir / "run.cmd", stat.S_IREAD)
    remove_runner_dir(runner_dir, keep_work=True)
    assert [p.name for p in runner_dir.iterdir()] == ["_work"]


def test_remove_runner_dir_read_only_directory(runner_dir: Path) -> None:
    # On POSIX deleting a file needs a writable parent; on Windows the dir gets the R attribute.
    populate(runner_dir)
    locked = runner_dir / "bin"
    os.chmod(locked, stat.S_IREAD | stat.S_IEXEC)
    try:
        remove_runner_dir(runner_dir)
    finally:
        if locked.exists():
            os.chmod(locked, stat.S_IRWXU)
    assert not runner_dir.exists()


def test_remove_runner_dir_keep_work_read_only_runner_dir(runner_dir: Path) -> None:
    # POSIX: unlinking a top-level file needs the runner dir itself to be writable.
    populate(runner_dir)
    os.chmod(runner_dir, stat.S_IREAD | stat.S_IEXEC)
    try:
        remove_runner_dir(runner_dir, keep_work=True)
    finally:
        os.chmod(runner_dir, stat.S_IRWXU)
    assert [p.name for p in runner_dir.iterdir()] == ["_work"]


def test_remove_runner_dir_wraps_os_errors(
    runner_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(32, "being used by another process")

    monkeypatch.setattr(installer.shutil, "rmtree", busy)
    with pytest.raises(InstallError, match="删除 runner 目录失败"):
        remove_runner_dir(runner_dir)


def test_rmtree_handler_reraises_other_errors(
    runner_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    populate(runner_dir)
    real_unlink = os.unlink

    def flaky_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if str(path).endswith("build.log"):
            raise OSError(5, "I/O error")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", flaky_unlink)
    with pytest.raises(InstallError, match="I/O error"):
        remove_runner_dir(runner_dir)


def test_rmtree_handler_ignores_vanished_files(
    runner_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    populate(runner_dir)
    real_unlink = os.unlink

    def racing_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        real_unlink(path, *args, **kwargs)
        if str(path).endswith("build.log"):
            raise FileNotFoundError(2, "gone")

    monkeypatch.setattr(os, "unlink", racing_unlink)
    remove_runner_dir(runner_dir)
    assert not runner_dir.exists()


# --- Windows only ------------------------------------------------------------------------------

FAKE_CONFIG_CMD = textwrap.dedent(
    """\
    @echo off
    if not exist marker.txt (
      echo marker.txt not found: wrong cwd
      exit /b 3
    )
    set LINE=
    set /p LINE=
    if defined LINE (
      echo stdin was not empty
      exit /b 4
    )
    echo args: %*
    echo.
    echo # Runner Registration
    echo Runner successfully added
    echo.
    echo # Runner settings
    echo Settings Saved.
    exit /b 0
    """
)


def _write_batch(path: Path, text: str) -> None:
    path.write_bytes(text.replace("\n", "\r\n").encode("ascii"))


@windows_only
def test_configure_real_cmd_exe(tmp_path: Path) -> None:
    d = tmp_path / "runner"
    d.mkdir()
    _write_batch(d / "config.cmd", FAKE_CONFIG_CMD)
    _write_batch(d / "run.cmd", "@echo off\n")
    (d / "marker.txt").write_text("x")
    token = fake_token()
    seen: list[dict[str, Any]] = []

    def spy(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append(kwargs)
        return run_command(args, **kwargs)

    result = configure(d, url=URL, token=token, name="box", labels=["gpu"], run=spy, timeout=60)
    assert result.returncode == 0
    assert "Runner successfully added" in result.output
    assert f"--url {URL}" in result.output
    assert "--labels gpu" in result.output
    assert token not in result.output
    assert seen[0]["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert seen[0]["stdin"] == subprocess.DEVNULL


@windows_only
def test_configure_real_cmd_exe_failure(tmp_path: Path) -> None:
    d = tmp_path / "runner"
    d.mkdir()
    _write_batch(d / "config.cmd", "@echo off\necho Error: registration refused\nexit /b 5\n")
    _write_batch(d / "run.cmd", "@echo off\n")
    with pytest.raises(InstallError) as info:
        configure(d, url=URL, token=fake_token(), name="box", labels=[], timeout=60)
    assert info.value.returncode == 5
    assert "Error: registration refused" in str(info.value)


@windows_only
def test_configure_real_cmd_exe_timeout_kills_grandchild(tmp_path: Path) -> None:
    d = tmp_path / "runner"
    d.mkdir()
    # ping is a grandchild of cmd.exe that holds the output pipe, like Runner.Listener.exe.
    _write_batch(d / "config.cmd", "@echo off\necho waiting\nping -n 60 127.0.0.1\n")
    _write_batch(d / "run.cmd", "@echo off\n")
    started = time.monotonic()
    with pytest.raises(InstallError, match="超时"):
        configure(d, url=URL, token=fake_token(), name="box", labels=[], timeout=3)
    assert time.monotonic() - started < 30


def _write_fake_runner(d: Path, config_cmd: str) -> None:
    d.mkdir()
    _write_batch(d / "config.cmd", config_cmd)
    _write_batch(d / "run.cmd", "@echo off\n")
    (d / "marker.txt").write_text("x")


def _real_system32(name: str) -> str:
    return os.path.join(os.environ["SYSTEMROOT"], "System32", name)


@windows_only
def test_config_commands_use_absolute_system32_cmd_exe() -> None:
    token = fake_token()
    expected = os.path.normcase(_real_system32("cmd.exe"))
    for args in (
        build_config_command(url=URL, token=token, name="box", labels=[]),
        build_remove_command(token=token),
    ):
        assert os.path.isabs(args[0]) and os.path.isfile(args[0])
        assert os.path.normcase(args[0]) == expected


@windows_only
def test_configure_ignores_cmd_exe_planted_in_current_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CreateProcess searches our current directory before System32 for a bare "cmd.exe".
    planted = tmp_path / "downloads"
    planted.mkdir()
    shutil.copyfile(_real_system32("whoami.exe"), planted / "cmd.exe")
    monkeypatch.chdir(planted)
    d = tmp_path / "runner"
    _write_fake_runner(d, FAKE_CONFIG_CMD)
    result = configure(d, url=URL, token=fake_token(), name="box", labels=[], timeout=60)
    assert "Runner successfully added" in result.output


@windows_only
def test_configure_real_cmd_exe_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNNERDECK_PAT", "ghp_" + "z" * 36)
    monkeypatch.setenv("HTTPS_PROXY", "http://stale:1")
    d = tmp_path / "runner"
    script = (
        "@echo off\n"
        "echo proxy=[%https_proxy%]\n"
        "if defined RUNNERDECK_PAT echo PAT LEAKED\n"
        "echo Runner successfully added\n"
        "echo Settings Saved.\n"
    )
    _write_fake_runner(d, script)
    result = configure(
        d, url=URL, token=fake_token(), name="box", labels=[], timeout=60,
        env={"https_proxy": PROXY},
    )  # fmt: skip
    assert f"proxy=[{PROXY}]" in result.output
    assert "PAT LEAKED" not in result.output


def _icacls_aces(path: Path) -> list[str]:
    """ACE strings `icacls <path>` prints, e.g. "NT AUTHORITY\\SYSTEM:(I)(OI)(CI)(F)"."""
    out = subprocess.run(
        [_real_system32("icacls.exe"), str(path)], capture_output=True, check=True
    ).stdout.decode(winproc.console_codepage_codec(), errors="replace")
    aces: list[str] = []
    for i, line in enumerate(out.splitlines()):
        if not line.strip():
            break  # blank line before "Successfully processed 1 files; ..."
        if i == 0:
            assert line.startswith(str(path)), out
            line = line[len(str(path)) :]
        aces.append(line.strip())
    assert aces, out
    return aces


def _is_users_ace(ace: str) -> bool:
    return "Authenticated Users" in ace or ace.startswith(("BUILTIN\\Users:", "Everyone:"))


@windows_only
def test_extract_runner_restricts_acl_windows(tmp_path: Path) -> None:
    # Like the C:\\ root: Authenticated Users get Modify on everything created below.
    drive_root = tmp_path / "drive_root"
    drive_root.mkdir()
    subprocess.run(
        [_real_system32("icacls.exe"), str(drive_root), "/grant", "*S-1-5-11:(OI)(CI)M"],
        capture_output=True,
        check=True,
    )
    assert any(_is_users_ace(a) for a in _icacls_aces(drive_root))
    zip_path = make_zip(tmp_path / "runner.zip", RUNNER_MEMBERS)
    target = drive_root / "runners" / "sandbox"
    extract_runner(zip_path, target)

    aces = _icacls_aces(target)
    assert not [a for a in aces if "(I)" in a], aces  # inheritance from above is cut
    assert not [a for a in aces if _is_users_ace(a)], aces
    assert 1 <= len(aces) <= 3 and all(a.endswith(":(OI)(CI)(F)") for a in aces), aces
    assert any(os.environ["USERNAME"].casefold() in a.casefold() for a in aces), aces
    for name in ("run.cmd", "bin/Runner.Listener.exe"):
        file_aces = _icacls_aces(target / name)
        assert not [a for a in file_aces if _is_users_ace(a)], file_aces
        assert all(a.endswith("(I)(F)") for a in file_aces), file_aces

    for name, data in RUNNER_MEMBERS.items():
        assert (target / name).read_bytes() == data
    remove_runner_dir(target)
    assert not target.exists()


@windows_only
def test_harden_runner_dir_real_failure_is_a_warning_windows(
    tmp_path: Path, logged_warnings: list[str]
) -> None:
    assert installer.harden_runner_dir(tmp_path / "missing") is False
    assert len(logged_warnings) == 1 and "icacls" in logged_warnings[0]


@windows_only
def test_remove_runner_dir_read_only_attribute_windows(runner_dir: Path) -> None:
    populate(runner_dir)
    ro = runner_dir / "bin" / "Runner.Listener.exe"
    os.chmod(ro, stat.S_IREAD)
    assert os.stat(ro).st_file_attributes & stat.FILE_ATTRIBUTE_READONLY  # type: ignore[attr-defined]
    remove_runner_dir(runner_dir)
    assert not runner_dir.exists()


def test_configure_accepts_replaced_runner_output(tmp_path: Path) -> None:
    # --replace over an existing runner prints "Successfully replaced the runner" instead of
    # "Runner successfully added" (actions/runner source; synthetic output).
    (tmp_path / "config.cmd").write_text("@echo off\r\n", encoding="utf-8")
    output = (
        "# Runner Registration\r\r\n"
        "√ Successfully replaced the runner\r\r\n"
        "# Runner settings\r\r\n"
        "√ Settings Saved.\r\r\n"
    ).encode()

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=output)

    result = installer.configure(
        tmp_path,
        url="https://github.com/o/r",
        token="AABBCCDDEEFF0011",
        name="host-r",
        labels=[],
        run=fake_run,
    )
    assert "Successfully replaced the runner" in result.output
