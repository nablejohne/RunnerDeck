"""Tests for the M1 foundation modules: models, paths, redact, logs, winproc."""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from pydantic import ValidationError

from runnerdeck.core import logs, paths, redact, winproc
from runnerdeck.core.errors import InvalidInputError, ProcessError
from runnerdeck.core.models import (
    CONFIG_SCHEMA_VERSION,
    AppConfig,
    RemoteRunner,
    Repo,
    RunnerFileInfo,
    RunnerRecord,
    RunnerToken,
    default_runner_name,
    normalize_labels,
    parse_full_name,
    validate_owner_repo,
    validate_runner_name,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _unique_secret(tag: str) -> str:
    """Not token-shaped: only an explicit register_secret() can mask it."""
    return f"{tag}-{uuid.uuid4().hex}"


# --- models: validation -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("runner1", "runner1"),
        ("  DESKTOP-01_ci.x  ", "DESKTOP-01_ci.x"),
        ("a" * 64, "a" * 64),
        ("9", "9"),
    ],
)
def test_validate_runner_name_accepts(raw: str, expected: str) -> None:
    assert validate_runner_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "-lead", ".lead", "_lead", "a b", "a&b", "a|b", "a^b", "a%b", "a" * 65, "电脑"],
)
def test_validate_runner_name_rejects(raw: str) -> None:
    with pytest.raises(InvalidInputError, match="runner 名称不合法"):
        validate_runner_name(raw)


def test_normalize_labels_from_string_drops_auto_and_dupes() -> None:
    raw = " gpu, Linux ,gpu,GPU,self-hosted,Windows,X64,, big.mem "
    assert normalize_labels(raw) == ["gpu", "Linux", "big.mem"]


def test_normalize_labels_from_list_and_tuple() -> None:
    assert normalize_labels(["a", " b ", "", "A"]) == ["a", "b"]
    assert normalize_labels(("x64", "self-hosted", "build_1")) == ["build_1"]
    assert normalize_labels([]) == []
    assert normalize_labels("") == []


@pytest.mark.parametrize("bad", ["has space", "a;b", "a&b", "-x", "标签", "a" * 101])
def test_normalize_labels_rejects_unsafe(bad: str) -> None:
    with pytest.raises(InvalidInputError, match="标签不合法"):
        normalize_labels(["ok", bad])


@pytest.mark.parametrize(
    ("owner", "repo"),
    [("octo", "app"), ("octo-org", "my.repo_1"), ("a" * 39, "r" * 100), ("x", ".github")],
)
def test_validate_owner_repo_accepts(owner: str, repo: str) -> None:
    assert validate_owner_repo(owner, repo) == (owner, repo)


@pytest.mark.parametrize(
    ("owner", "repo", "match"),
    [
        ("octo", ".", "仓库名"),
        ("octo", "..", "仓库名"),
        ("octo", "", "仓库名"),
        ("octo", "a/b", "仓库名"),
        ("octo", "r" * 101, "仓库名"),
        ("octo", "a b", "仓库名"),
        ("-octo", "app", "所有者"),
        ("oc_to", "app", "所有者"),
        ("a" * 40, "app", "所有者"),
        ("", "app", "所有者"),
    ],
)
def test_validate_owner_repo_rejects(owner: str, repo: str, match: str) -> None:
    with pytest.raises(InvalidInputError, match=match):
        validate_owner_repo(owner, repo)


def test_parse_full_name() -> None:
    assert parse_full_name("  octo/app \n") == ("octo", "app")
    for bad in ["octoapp", "/app", "octo/", "octo/app/extra", "octo/.."]:
        with pytest.raises(InvalidInputError):
            parse_full_name(bad)


@pytest.mark.parametrize(
    ("hostname", "repo", "expected"),
    [
        ("DESKTOP-ABC", "app", "DESKTOP-ABC-app"),
        ("DESKTOP-ABC", "my repo!", "DESKTOP-ABC-my-repo"),
        ("电脑", "repo", "repo"),
        ("", "", "runner"),
        ("..", "..", "runner"),
        ("a" * 63, "b", "a" * 63),  # cut lands on '-', which is stripped
        ("h", "x" * 80, "h-" + "x" * 62),
    ],
)
def test_default_runner_name(hostname: str, repo: str, expected: str) -> None:
    name = default_runner_name(hostname, repo)
    assert name == expected
    assert validate_runner_name(name) == name


# --- models: AppConfig and records ---------------------------------------------------------


def _rec(record_id: str, name: str = "n") -> RunnerRecord:
    return RunnerRecord(id=record_id, owner="octo", repo="app", name=name, path="D:\\r")


def test_runner_record_defaults() -> None:
    record = RunnerRecord(owner="octo", repo="app", name="n", path="D:\\r")
    assert len(record.id) == 32 and int(record.id, 16) >= 0
    assert record.created_at.tzinfo is not None
    assert record.full_name == "octo/app"
    assert record.repo_url == "https://github.com/octo/app"
    assert record.labels == [] and record.github_runner_id is None


def test_app_config_defaults() -> None:
    config = AppConfig()
    assert config.schema_version == CONFIG_SCHEMA_VERSION
    assert config.account is None
    assert config.runners == []
    assert config.settings.runners_root == str(paths.default_runners_root())
    assert config.settings.poll_interval_s == 30


def test_app_config_get_upsert_remove() -> None:
    config = AppConfig()
    first, second = _rec("1" * 32, "first"), _rec("2" * 32, "second")
    config.upsert(first)
    config.upsert(second)
    assert [r.name for r in config.runners] == ["first", "second"]
    assert config.get("2" * 32) is second
    assert config.get("missing") is None

    replacement = _rec("1" * 32, "renamed")
    config.upsert(replacement)
    assert [r.name for r in config.runners] == ["renamed", "second"]  # replaced in place

    assert config.remove("1" * 32) is replacement
    assert [r.id for r in config.runners] == ["2" * 32]
    assert config.remove("1" * 32) is None
    assert len(config.runners) == 1


def test_repo_owner_dict_becomes_login() -> None:
    api = {
        "id": 1,
        "name": "app",
        "full_name": "octo/app",
        "owner": {"login": "octo", "id": 9, "type": "User"},
        "private": True,
        "html_url": "https://github.com/octo/app",
        "visibility": "private",
    }
    repo = Repo.model_validate(api)
    assert repo.owner == "octo"
    assert repo.private is True and repo.fork is False and repo.archived is False
    assert Repo.model_validate({**api, "owner": "plain"}).owner == "plain"


def test_remote_runner_label_dicts_become_names() -> None:
    runner = RemoteRunner.model_validate(
        {
            "id": 7,
            "name": "pc-app",
            "os": "Windows",
            "status": "online",
            "busy": True,
            "labels": [
                {"id": 1, "name": "self-hosted", "type": "read-only"},
                {"id": 2, "name": "Windows", "type": "read-only"},
                {"id": 3, "name": "gpu", "type": "custom"},
                "plain",
            ],
        }
    )
    assert runner.labels == ["self-hosted", "Windows", "gpu", "plain"]
    assert runner.busy is True
    with pytest.raises(ValidationError):
        RemoteRunner.model_validate({"id": 1, "name": "x", "status": "idle"})
    with pytest.raises(ValidationError):
        RemoteRunner.model_validate({"id": 1, "name": "x", "status": "online", "labels": "gpu"})


def test_runner_token_parses_offset_and_hides_value() -> None:
    value = "AABBCCFAKEREGISTRATIONTOKEN12"
    token = RunnerToken.model_validate(
        {"token": value, "expires_at": "2026-10-22T17:16:05.123+08:00"}
    )
    assert token.expires_at.utcoffset() == timedelta(hours=8)
    assert token.expires_at.astimezone(UTC) == datetime(2026, 10, 22, 9, 16, 5, 123000, UTC)
    assert token.token.get_secret_value() == value
    for text in (repr(token), str(token), token.model_dump_json()):
        assert value not in text


def test_runner_token_requires_timezone() -> None:
    with pytest.raises(ValidationError):
        RunnerToken.model_validate({"token": "x" * 29, "expires_at": "2026-10-22T17:16:05"})


def test_runner_file_info_aliases() -> None:
    raw = {
        "agentId": 12,
        "agentName": "pc-app",
        "poolId": 1,
        "poolName": "Default",
        "serverUrl": "https://pipelines.actions.githubusercontent.com/x",
        "gitHubUrl": "https://github.com/octo/app",
        "workFolder": "_work",
        "useV2Flow": True,
    }  # synthetic sample, fields per docs/m1-design.md §6
    info = RunnerFileInfo.model_validate({**raw, "raw": raw})
    assert (info.agent_id, info.agent_name, info.github_url) == (
        12,
        "pc-app",
        "https://github.com/octo/app",
    )
    assert info.raw["useV2Flow"] is True
    assert "raw" not in info.model_dump()


# --- paths ------------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for var in (paths.ENV_HOME, "APPDATA", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_runnerdeck_home_override(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(isolated_home / "ignored"))
    assert paths.config_dir() == isolated_home
    assert paths.config_file() == isolated_home / "config.json"
    assert paths.local_dir() == isolated_home
    assert paths.cache_dir() == isolated_home / "cache"
    assert paths.log_dir() == isolated_home / "logs"


def test_appdata_based_paths(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clean_env.setenv("APPDATA", str(tmp_path / "Roaming"))
    clean_env.setenv("LOCALAPPDATA", f"  {tmp_path / 'Local'}  ")
    clean_env.setenv(paths.ENV_HOME, "   ")  # blank override is ignored
    assert paths.config_dir() == tmp_path / "Roaming" / "RunnerDeck"
    assert paths.config_file() == tmp_path / "Roaming" / "RunnerDeck" / "config.json"
    assert paths.local_dir() == tmp_path / "Local" / "RunnerDeck"
    assert paths.cache_dir() == tmp_path / "Local" / "RunnerDeck" / "cache"
    assert paths.log_dir() == tmp_path / "Local" / "RunnerDeck" / "logs"


def test_fallback_without_appdata(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clean_env.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert paths.config_dir() == tmp_path / ".runnerdeck"
    assert paths.local_dir() == tmp_path / ".runnerdeck"


def test_default_runners_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.ENV_HOME, raising=False)
    root = paths.default_runners_root()
    if sys.platform == "win32":
        assert root in {Path("D:\\runners"), Path("C:\\runners")}
    else:
        assert root == Path.home() / "runners"


def test_default_runners_root_follows_home_override(isolated_home: Path) -> None:
    assert paths.default_runners_root() == isolated_home / "runners"


def test_runner_dir_for_suffixes(tmp_path: Path) -> None:
    root = tmp_path / "runners"
    assert paths.runner_dir_for(root, "app") == root / "app"  # root does not exist yet

    (root / "app").mkdir(parents=True)
    assert paths.runner_dir_for(root, "app") == root / "app"  # empty dir is reusable

    (root / "app" / "config.cmd").write_text("x", encoding="utf-8")
    assert paths.runner_dir_for(root, "app") == root / "app-2"

    (root / "app-2").mkdir()
    (root / "app-2" / "x").write_text("x", encoding="utf-8")
    (root / "app-3").write_text("a file, not a dir", encoding="utf-8")
    assert paths.runner_dir_for(root, "app") == root / "app-4"


# --- redact -------------------------------------------------------------------------------


def test_redactor_masks_registered_secrets() -> None:
    r = redact.Redactor()
    r.add("s3cret-value-123")
    r.add(None)
    r.add("")
    r.add("short")  # < 8 chars: ignored, would mask ordinary words
    assert r("pat s3cret-value-123 end") == "pat *** end"
    assert r("a short word") == "a short word"


def test_redactor_masks_longest_secret_first() -> None:
    r = redact.Redactor()
    r.add("abcdefgh")
    r.add("abcdefgh1234")
    assert r("x abcdefgh1234 y abcdefgh z") == "x *** y *** z"


@pytest.mark.parametrize(
    "token",
    [
        "github_pat_" + "A1b2_" * 10,
        "ghp_" + "Z" * 36,
        "gho_" + "a" * 20,
        "ghs_" + "9" * 30,
        "ghr_" + "q" * 40,
        "ghu_" + "x" * 25,
    ],
)
def test_redactor_masks_unregistered_token_shapes(token: str) -> None:
    assert redact.Redactor()(f"before {token} after") == "before *** after"


def test_redactor_leaves_short_token_lookalikes() -> None:
    assert redact.Redactor()("ghp_short github_pat_x") == "ghp_short github_pat_x"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("config.cmd --token AABBCC --name x", "config.cmd --token *** --name x"),
        ("remove --TOKEN AABBCC", "remove --TOKEN ***"),
        ("Authorization: Bearer abc.def", "Authorization: Bearer ***"),
        ("authorization: token abc", "authorization: token ***"),
        ("token=abc123 next", "token=*** next"),
        ("token=abc123&x=1", "token=***"),  # over-masks to the next separator: safe side
        (
            '{"token": "AABB", "expires_at": "2026-10-22T17:16:05+08:00"}',
            '{"token": "***", "expires_at": "2026-10-22T17:16:05+08:00"}',
        ),
        ("token: 'abc'", "token: '***'"),
    ],
)
def test_redactor_keyed_patterns(text: str, expected: str) -> None:
    assert redact.Redactor()(text) == expected


def test_global_register_and_redact() -> None:
    secret = _unique_secret("global")
    assert redact.redact(f"v={secret}") == f"v={secret}"
    redact.register_secret(secret)
    assert redact.redact(f"v={secret}") == "v=***"
    redact.register_secret(None)  # tolerated


def test_redacting_formatter_masks_message_and_traceback() -> None:
    secret = _unique_secret("formatter")
    redact.register_secret(secret)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(redact.RedactingFormatter("%(levelname)s %(message)s"))
    logger = logging.getLogger(f"test.redact.{uuid.uuid4().hex}")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.warning("using %s", secret)
        try:
            raise ValueError(f"boom with {secret}")
        except ValueError:
            logger.exception("failed for %s", "ghp_" + "Q" * 36)
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert secret not in output
    assert "ghp_" + "Q" * 36 not in output
    assert "WARNING using ***" in output
    assert "ValueError: boom with ***" in output
    assert "Traceback" in output


# --- logs ---------------------------------------------------------------------------------


@pytest.fixture
def restore_runnerdeck_logger() -> Iterator[logging.Logger]:
    logger = logging.getLogger("runnerdeck")
    saved = (list(logger.handlers), logger.level, logger.propagate)
    yield logger
    for handler in list(logger.handlers):
        if handler not in saved[0]:
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(saved[1])
    logger.propagate = saved[2]


def _tagged(logger: logging.Logger) -> list[logging.Handler]:
    return [h for h in logger.handlers if getattr(h, "_runnerdeck_handler", False)]


def test_setup_logging_creates_file_in_given_dir(
    tmp_path: Path, restore_runnerdeck_logger: logging.Logger
) -> None:
    log_dir = tmp_path / "deep" / "logs"
    log_file = logs.setup_logging(log_dir, level=logging.DEBUG)
    assert log_file == log_dir / "runnerdeck.log"
    logging.getLogger("runnerdeck.core.test").debug("hello %s", "world")
    for handler in _tagged(restore_runnerdeck_logger):
        handler.flush()
    assert "hello world" in log_file.read_text(encoding="utf-8")
    assert restore_runnerdeck_logger.level == logging.DEBUG
    assert restore_runnerdeck_logger.propagate is False


def test_setup_logging_defaults_to_app_log_dir(
    isolated_home: Path, restore_runnerdeck_logger: logging.Logger
) -> None:
    assert logs.setup_logging() == isolated_home / "logs" / "runnerdeck.log"
    assert (isolated_home / "logs").is_dir()


def test_setup_logging_is_idempotent(
    tmp_path: Path, restore_runnerdeck_logger: logging.Logger
) -> None:
    foreign = logging.NullHandler()
    restore_runnerdeck_logger.addHandler(foreign)
    try:
        logs.setup_logging(tmp_path)
        logs.setup_logging(tmp_path)
        assert len(_tagged(restore_runnerdeck_logger)) == 1
        logs.setup_logging(tmp_path, stream=True)
        assert len(_tagged(restore_runnerdeck_logger)) == 2
        logs.setup_logging(tmp_path)
        assert len(_tagged(restore_runnerdeck_logger)) == 1
        assert foreign in restore_runnerdeck_logger.handlers  # only our own handlers replaced
    finally:
        restore_runnerdeck_logger.removeHandler(foreign)


def test_log_file_is_redacted(tmp_path: Path, restore_runnerdeck_logger: logging.Logger) -> None:
    secret = _unique_secret("logfile")
    redact.register_secret(secret)
    log_file = logs.setup_logging(tmp_path)
    log = logging.getLogger("runnerdeck.core.anything")
    log.info("pat is %s", secret)
    log.info("cmd: config.cmd --token REGTOKENFAKE --name x")
    try:
        raise RuntimeError(f"failed: {secret}")
    except RuntimeError:
        log.exception("oops")
    for handler in _tagged(restore_runnerdeck_logger):
        handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert secret not in text
    assert "REGTOKENFAKE" not in text
    assert "pat is ***" in text
    assert "--token ***" in text
    assert "RuntimeError: failed: ***" in text


# --- winproc: decoding and paths ------------------------------------------------------------


def test_decode_line_gbk_fixture_matches_decoded_capture() -> None:
    raw = (FIXTURES / "stdout" / "job_then_ctrl_break.cp936.raw").read_bytes()
    expected = (
        (FIXTURES / "stdout" / "job_then_ctrl_break.txt").read_text(encoding="utf-8").splitlines()
    )
    decoded = [winproc.decode_line(line, fallback="gbk") for line in raw.split(b"\n")]
    if decoded and decoded[-1] == "":
        decoded.pop()  # trailing newline
    assert decoded == expected
    assert "终止批处理操作吗(Y/N)? " in decoded
    assert "已复制         1 个文件。" in decoded
    assert "Current runner version: '2.338.0'" in decoded


def test_decode_line_prefers_utf8_and_strips_newlines() -> None:
    assert winproc.decode_line("√ Connected to GitHub\r\n".encode(), fallback="gbk") == (
        "√ Connected to GitHub"
    )
    assert winproc.decode_line(b"plain ascii\n") == "plain ascii"
    assert winproc.decode_line(b"\r\n") == ""
    assert winproc.decode_line(b"trailing space \r\n") == "trailing space "


def test_decode_line_fallback_never_raises() -> None:
    assert winproc.decode_line("√".encode("gbk"), fallback="gbk") == "√"
    assert winproc.decode_line(b"\xff\xfe bad", fallback="ascii") == "\ufffd\ufffd bad"
    assert isinstance(winproc.decode_line(b"\xd2\xd1\xb8\xb4"), str)  # default codec


def test_console_codepage_codec() -> None:
    expected = "oem" if sys.platform == "win32" else "latin-1"
    assert winproc.console_codepage_codec() == expected
    assert "x".encode(winproc.console_codepage_codec()) == b"x"


def test_is_under_respects_boundaries(tmp_path: Path) -> None:
    root = tmp_path / "runners" / "foo"
    assert winproc.is_under(root / "bin" / "Runner.Listener.exe", root)
    assert winproc.is_under(root, root)
    assert winproc.is_under(str(root / "x"), str(root) + os.sep)  # trailing separator on root
    assert not winproc.is_under(tmp_path / "runners" / "foobar" / "x.exe", root)
    assert not winproc.is_under(tmp_path / "runners" / "fo", root)
    assert not winproc.is_under(tmp_path / "runners", root)
    assert not winproc.is_under(root / ".." / "bar" / "x.exe", root)  # normalised first


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_is_under_is_case_insensitive_on_windows() -> None:
    assert winproc.is_under("D:\\Runners\\Foo\\bin\\Runner.Listener.exe", "d:\\runners\\foo")
    assert winproc.is_under("d:/runners/foo/bin/x.exe", "D:\\RUNNERS\\FOO\\")
    assert not winproc.is_under("D:\\runners\\foobar\\x.exe", "D:\\runners\\foo")


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows behaviour")
def test_console_helpers_off_windows() -> None:
    assert winproc.has_console() is False
    with pytest.raises(ProcessError):
        winproc.send_ctrl_break(os.getpid())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_has_console_returns_bool_on_windows() -> None:
    assert isinstance(winproc.has_console(), bool)


# --- winproc: processes -----------------------------------------------------------------------


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


def test_kill_tree_kills_child_and_grandchild() -> None:
    script = (
        "import subprocess, sys, time\n"
        "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(gc.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE
    )
    try:
        assert child.stdout is not None
        grandchild = int(child.stdout.readline())
        killed = winproc.kill_tree(child.pid, timeout=10)
        assert set(killed) >= {child.pid, grandchild}
        assert child.wait(timeout=10) is not None
        assert _wait_gone(grandchild)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        if child.stdout is not None:
            child.stdout.close()


def test_kill_tree_on_single_sleeping_child() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL
    )
    try:
        assert winproc.kill_tree(child.pid, timeout=10) == [child.pid]
        # psutil may already have reaped it on POSIX, so the exit code is not reliable here.
        child.wait(timeout=10)
        assert _wait_gone(child.pid)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_kill_tree_missing_pid_returns_empty() -> None:
    pid = max(psutil.pids()) + 100_000
    while psutil.pid_exists(pid):
        pid += 4
    assert winproc.kill_tree(pid) == []


def test_kill_processes_tolerates_already_dead() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL
    )
    proc = psutil.Process(child.pid)
    child.kill()
    child.wait(timeout=10)
    assert winproc.kill_processes([proc], timeout=1) == [child.pid]


def test_find_runner_processes_empty_dir(tmp_path: Path) -> None:
    assert winproc.find_runner_processes(tmp_path) == []


def test_find_runner_processes_filters_by_name_and_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner_dir = tmp_path / "runners" / "app"
    inside = str(runner_dir / "bin" / "Runner.Listener.exe")

    def fake(pid: int, name: str | None, exe: str | None) -> SimpleNamespace:
        return SimpleNamespace(pid=pid, info={"name": name, "exe": exe})

    procs = [
        fake(1, "Runner.Listener.exe", inside),
        fake(2, "runner.worker.exe", str(runner_dir / "bin" / "Runner.Worker.exe")),
        fake(3, "Runner.PluginHost.exe", str(runner_dir / "bin" / "Runner.PluginHost.exe")),
        fake(4, "Runner.Listener.exe", str(tmp_path / "runners" / "app-2" / "bin" / "x.exe")),
        fake(5, "cmd.exe", str(runner_dir / "run.cmd")),
        fake(6, "Runner.Listener.exe", None),  # access denied -> no exe
        fake(7, None, inside),
    ]
    seen_attrs: list[list[str]] = []

    def fake_iter(attrs: list[str]) -> list[SimpleNamespace]:
        seen_attrs.append(attrs)
        return procs

    monkeypatch.setattr(psutil, "process_iter", fake_iter)
    found = winproc.find_runner_processes(runner_dir)
    assert [p.pid for p in found] == [1, 2, 3]
    assert seen_attrs == [["name", "exe"]]
