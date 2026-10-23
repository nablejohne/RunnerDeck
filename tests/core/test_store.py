from __future__ import annotations

import json
import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from runnerdeck.core import store
from runnerdeck.core.errors import AmbiguousRunnerError, RunnerNotFoundError, StoreError
from runnerdeck.core.models import CONFIG_SCHEMA_VERSION, AppConfig, RunnerRecord
from runnerdeck.core.store import ConfigStore, resolve_runner


def _record(**kw: object) -> RunnerRecord:
    defaults: dict[str, object] = {
        "owner": "octo",
        "repo": "app",
        "name": "pc-app",
        "path": "D:\\runners\\app",
    }
    defaults.update(kw)
    return RunnerRecord.model_validate(defaults)


def _sample_config() -> AppConfig:
    config = AppConfig(account="octocat")
    config.settings.runners_root = "D:\\运行器"
    config.settings.default_labels = ["gpu"]
    config.upsert(
        _record(
            id="a" * 32,
            labels=["gpu", "big"],
            github_runner_id=42,
            created_at=datetime(2026, 10, 22, 8, 9, 45, tzinfo=UTC),
        )
    )
    return config


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    return tmp_path / "conf" / "config.json"


@pytest.fixture
def no_delay(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(store.time, "sleep", sleeps.append)
    return sleeps


# --- load -----------------------------------------------------------------------------


def test_default_path_follows_runnerdeck_home(isolated_home: Path) -> None:
    assert ConfigStore().path == isolated_home / "config.json"


def test_load_missing_file_returns_defaults_without_creating_it(cfg_path: Path) -> None:
    config = ConfigStore(cfg_path).load()
    assert config == AppConfig()
    assert config.runners == []
    assert config.schema_version == CONFIG_SCHEMA_VERSION
    assert not cfg_path.exists()
    assert not cfg_path.parent.exists()


def test_round_trip_preserves_everything(cfg_path: Path) -> None:
    original = _sample_config()
    s = ConfigStore(cfg_path)
    s.save(original)
    loaded = s.load()
    assert loaded == original
    assert loaded.runners[0].created_at == datetime(2026, 10, 22, 8, 9, 45, tzinfo=UTC)


def test_save_format_utf8_indent_iso_datetime(cfg_path: Path) -> None:
    ConfigStore(cfg_path).save(_sample_config())
    text = cfg_path.read_bytes().decode("utf-8")
    assert "D:\\\\运行器" in text  # ensure_ascii=False: not \u8fd0...
    assert "\\u" not in text
    assert text.startswith('{\n  "schema_version": 1,')
    assert text.endswith("}\n")
    created = json.loads(text)["runners"][0]["created_at"]
    assert datetime.fromisoformat(created) == datetime(2026, 10, 22, 8, 9, 45, tzinfo=UTC)


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "c" / "config.json"
    ConfigStore(path).save(AppConfig())
    assert path.is_file()


def test_save_overwrites_and_leaves_no_temp_files(cfg_path: Path) -> None:
    s = ConfigStore(cfg_path)
    s.save(AppConfig(account="first"))
    s.save(AppConfig(account="second"))
    assert s.load().account == "second"
    assert os.listdir(cfg_path.parent) == ["config.json"]


def test_load_accepts_utf8_bom(cfg_path: Path) -> None:
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"account": "bom"}).encode())
    assert ConfigStore(cfg_path).load().account == "bom"


def test_load_ignores_unknown_fields(cfg_path: Path) -> None:
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        json.dumps({"account": "x", "future_field": 1, "settings": {"nope": True}}),
        encoding="utf-8",
    )
    assert ConfigStore(cfg_path).load().account == "x"


def test_load_invalid_json_raises_with_path(cfg_path: Path) -> None:
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text('{"account": ', encoding="utf-8")
    with pytest.raises(StoreError) as exc:
        ConfigStore(cfg_path).load()
    assert str(cfg_path) in str(exc.value)
    assert isinstance(exc.value.__cause__, json.JSONDecodeError)
    assert cfg_path.read_text(encoding="utf-8") == '{"account": '  # not reset


def test_load_non_utf8_raises(cfg_path: Path) -> None:
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_bytes('{"account": "中文"}'.encode("gbk"))
    with pytest.raises(StoreError, match="UTF-8"):
        ConfigStore(cfg_path).load()


@pytest.mark.parametrize("payload", ["[]", '"text"', "null", "3"])
def test_load_non_object_raises(cfg_path: Path, payload: str) -> None:
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(payload, encoding="utf-8")
    with pytest.raises(StoreError, match="JSON 对象"):
        ConfigStore(cfg_path).load()


def test_load_validation_error_names_path_and_field(cfg_path: Path) -> None:
    cfg_path.parent.mkdir(parents=True)
    bad = {"settings": {"poll_interval_s": 1}, "runners": [{"owner": "o"}]}
    cfg_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(StoreError) as exc:
        ConfigStore(cfg_path).load()
    message = str(exc.value)
    assert str(cfg_path) in message
    assert "settings.poll_interval_s" in message
    assert "另有" in message  # 1 + 3 missing record fields > 3 reported


def test_validation_message_does_not_echo_values(cfg_path: Path) -> None:
    cfg_path.parent.mkdir(parents=True)
    leaked = "value-that-must-not-appear-in-errors"
    cfg_path.write_text(json.dumps({"settings": {"theme": leaked}}), encoding="utf-8")
    with pytest.raises(StoreError) as exc:
        ConfigStore(cfg_path).load()
    assert "settings.theme" in str(exc.value)
    assert leaked not in str(exc.value)


@pytest.mark.parametrize("version", [CONFIG_SCHEMA_VERSION + 1, 99, 2.5])
def test_load_newer_schema_refuses(cfg_path: Path, version: float) -> None:
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(json.dumps({"schema_version": version}), encoding="utf-8")
    with pytest.raises(StoreError, match="升级"):
        ConfigStore(cfg_path).load()


def test_load_bool_schema_version_is_not_treated_as_newer(cfg_path: Path) -> None:
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(json.dumps({"schema_version": True}), encoding="utf-8")
    assert ConfigStore(cfg_path).load().schema_version == 1


def test_load_unreadable_path_raises(tmp_path: Path) -> None:
    directory = tmp_path / "config.json"
    directory.mkdir()
    with pytest.raises(StoreError, match="无法读取"):
        ConfigStore(directory).load()


# --- atomic save -------------------------------------------------------------------------


def test_failure_during_write_keeps_old_file_and_removes_temp(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = ConfigStore(cfg_path)
    s.save(AppConfig(account="old"))
    before = cfg_path.read_bytes()

    def broken_fsync(fd: int) -> None:
        raise OSError(5, "disk on fire")

    monkeypatch.setattr(store.os, "fsync", broken_fsync)
    with pytest.raises(StoreError, match="disk on fire") as exc:
        s.save(AppConfig(account="new"))
    assert isinstance(exc.value.__cause__, OSError)
    assert cfg_path.read_bytes() == before
    assert os.listdir(cfg_path.parent) == ["config.json"]


def test_non_oserror_during_write_also_cleans_up(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = ConfigStore(cfg_path)
    s.save(AppConfig(account="old"))

    def interrupted(fd: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(store.os, "fsync", interrupted)
    with pytest.raises(KeyboardInterrupt):
        s.save(AppConfig(account="new"))
    assert s.load().account == "old"
    assert os.listdir(cfg_path.parent) == ["config.json"]


def test_replace_failure_other_than_permission_is_not_retried(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch, no_delay: list[float]
) -> None:
    s = ConfigStore(cfg_path)
    s.save(AppConfig(account="old"))
    calls: list[tuple[str, str]] = []

    def failing_replace(src: str, dst: str) -> None:
        calls.append((str(src), str(dst)))
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store.os, "replace", failing_replace)
    with pytest.raises(StoreError, match="No space left"):
        s.save(AppConfig(account="new"))
    assert len(calls) == 1
    assert no_delay == []
    assert os.listdir(cfg_path.parent) == ["config.json"]


def test_replace_permission_error_is_retried_then_succeeds(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch, no_delay: list[float]
) -> None:
    s = ConfigStore(cfg_path)
    s.save(AppConfig(account="old"))
    real_replace = os.replace
    attempts = 0

    def flaky_replace(src: str, dst: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise PermissionError(13, "file in use")
        real_replace(src, dst)

    monkeypatch.setattr(store.os, "replace", flaky_replace)
    s.save(AppConfig(account="new"))
    assert attempts == 4
    assert no_delay == [store.REPLACE_RETRY_DELAY_S] * 3
    assert s.load().account == "new"
    assert os.listdir(cfg_path.parent) == ["config.json"]


def test_replace_permission_error_gives_up_after_retries(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch, no_delay: list[float]
) -> None:
    s = ConfigStore(cfg_path)
    s.save(AppConfig(account="old"))
    attempts = 0

    def locked_replace(src: str, dst: str) -> None:
        nonlocal attempts
        attempts += 1
        raise PermissionError(13, "file in use")

    monkeypatch.setattr(store.os, "replace", locked_replace)
    with pytest.raises(StoreError, match="占用") as exc:
        s.save(AppConfig(account="new"))
    assert isinstance(exc.value.__cause__, PermissionError)
    assert attempts == store.REPLACE_RETRIES + 1
    assert len(no_delay) == store.REPLACE_RETRIES
    monkeypatch.undo()
    assert s.load().account == "old"
    assert os.listdir(cfg_path.parent) == ["config.json"]


def test_save_into_unwritable_location_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(StoreError, match="无法在"):
        ConfigStore(blocker / "config.json").save(AppConfig())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_open_handle_blocks_replace_until_released(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "REPLACE_RETRY_DELAY_S", 0.2)
    s = ConfigStore(cfg_path)
    s.save(AppConfig(account="old"))

    # Python's open() on Windows does not pass FILE_SHARE_DELETE, so os.replace onto it fails.
    with open(cfg_path, "rb"), pytest.raises(StoreError, match="占用"):
        s.save(AppConfig(account="locked"))
    assert s.load().account == "old"
    assert os.listdir(cfg_path.parent) == ["config.json"]

    handle = open(cfg_path, "rb")  # noqa: SIM115 - closed by the timer
    timer = threading.Timer(0.3, handle.close)
    timer.start()
    try:
        s.save(AppConfig(account="new"))
    finally:
        timer.join()
        handle.close()
    assert s.load().account == "new"


# --- resolve_runner ------------------------------------------------------------------------


@pytest.fixture
def runners() -> AppConfig:
    config = AppConfig()
    for record in (
        _record(id="abcdef0000000000000000000000000a", name="pc-app", repo="app"),
        _record(id="abcdef1111111111111111111111111b", name="pc-api", repo="api"),
        _record(id="1234560000000000000000000000000c", name="Build-Box", repo="web"),
        _record(id="9999990000000000000000000000000d", name="shared", owner="other", repo="lib"),
        _record(id="8888880000000000000000000000000e", name="shared", owner="third", repo="lib"),
        _record(id="7777770000000000000000000000000f", name="docs-1", repo="docs"),
        _record(id="7777771111111111111111111111111f", name="docs-2", repo="docs"),
    ):
        config.upsert(record)
    return config


def test_resolve_exact_id(runners: AppConfig) -> None:
    assert resolve_runner(runners, "abcdef1111111111111111111111111b").name == "pc-api"


def test_resolve_strips_whitespace(runners: AppConfig) -> None:
    assert resolve_runner(runners, "  pc-app\n").name == "pc-app"


def test_resolve_unique_id_prefix(runners: AppConfig) -> None:
    assert resolve_runner(runners, "123456").name == "Build-Box"
    assert resolve_runner(runners, "abcdef1").name == "pc-api"


def test_resolve_short_prefix_is_not_an_id(runners: AppConfig) -> None:
    with pytest.raises(RunnerNotFoundError):
        resolve_runner(runners, "12345")


def test_resolve_ambiguous_prefix_lists_candidates(runners: AppConfig) -> None:
    with pytest.raises(AmbiguousRunnerError) as exc:
        resolve_runner(runners, "abcdef")
    message = str(exc.value)
    assert "abcdef0000000000000000000000000a" in message
    assert "abcdef1111111111111111111111111b" in message
    assert "pc-app" in message and "octo/api" in message


def test_resolve_name_case_insensitive(runners: AppConfig) -> None:
    assert resolve_runner(runners, "build-BOX").id.startswith("123456")


def test_resolve_owner_repo_case_insensitive(runners: AppConfig) -> None:
    assert resolve_runner(runners, "OCTO/Web").name == "Build-Box"


def test_resolve_duplicate_name_is_ambiguous(runners: AppConfig) -> None:
    with pytest.raises(AmbiguousRunnerError) as exc:
        resolve_runner(runners, "shared")
    assert "other/lib" in str(exc.value) and "third/lib" in str(exc.value)


def test_resolve_repo_with_two_runners_is_ambiguous(runners: AppConfig) -> None:
    with pytest.raises(AmbiguousRunnerError, match="docs-1"):
        resolve_runner(runners, "octo/docs")


def test_resolve_not_found(runners: AppConfig) -> None:
    with pytest.raises(RunnerNotFoundError, match="nope"):
        resolve_runner(runners, "nope")


@pytest.mark.parametrize("key", ["", "   "])
def test_resolve_empty_key(runners: AppConfig, key: str) -> None:
    with pytest.raises(RunnerNotFoundError):
        resolve_runner(runners, key)


def test_resolve_on_empty_config() -> None:
    with pytest.raises(RunnerNotFoundError):
        resolve_runner(AppConfig(), "anything")


def test_resolve_precedence_id_before_prefix_before_name_before_repo() -> None:
    config = AppConfig()
    target = _record(id="deadbeef" * 4, name="x1", repo="r1")
    named_like_id = _record(id="0" * 32, name="deadbeef" * 4, repo="r2")
    config.upsert(named_like_id)
    config.upsert(target)
    assert resolve_runner(config, "deadbeef" * 4) is target  # exact id beats name
    named_like_prefix = _record(id="1" * 32, name="deadbe", repo="r3")
    config.upsert(named_like_prefix)
    assert resolve_runner(config, "deadbe") is target  # unique prefix beats name
    # a name equal to another runner's owner/repo: name wins
    repo_named = _record(id="2" * 32, name="octo/r1", repo="r4")
    config.upsert(repo_named)
    assert resolve_runner(config, "octo/r1") is repo_named


def test_resolve_name_wins_over_ambiguous_prefix() -> None:
    config = AppConfig()
    config.upsert(_record(id="cafe00" + "0" * 26, name="a", repo="r1"))
    config.upsert(_record(id="cafe00" + "1" * 26, name="b", repo="r2"))
    wanted = _record(id="f" * 32, name="cafe00", repo="r3")
    config.upsert(wanted)
    assert resolve_runner(config, "cafe00") is wanted


def test_resolve_error_message_redacts_secret_shaped_keys(runners: AppConfig) -> None:
    fake = "ghp_" + "Z" * 36
    with pytest.raises(RunnerNotFoundError) as exc:
        resolve_runner(runners, fake)
    assert fake not in str(exc.value)
