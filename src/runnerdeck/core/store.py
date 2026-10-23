"""config.json persistence (atomic writes) and runner lookup by a user-supplied key."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from runnerdeck.core import paths
from runnerdeck.core.errors import AmbiguousRunnerError, RunnerNotFoundError, StoreError
from runnerdeck.core.models import CONFIG_SCHEMA_VERSION, AppConfig, RunnerRecord
from runnerdeck.core.redact import redact

log = logging.getLogger(__name__)

# On Windows os.replace raises PermissionError while another process (antivirus, an editor,
# a second RunnerDeck) holds config.json open without FILE_SHARE_DELETE; it is usually brief.
REPLACE_RETRIES = 5  # retries after the first attempt
REPLACE_RETRY_DELAY_S = 0.1
MIN_ID_PREFIX = 6
_MAX_REPORTED_ERRORS = 3


class ConfigStore:
    """Loads and atomically saves config.json. No locking: the app is the only writer."""

    def __init__(self, path: Path | None = None) -> None:
        self.path: Path = path if path is not None else paths.config_file()

    def load(self) -> AppConfig:
        """Missing file -> defaults. Anything unreadable raises StoreError instead of resetting."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return AppConfig()
        except OSError as e:
            raise StoreError(f"无法读取配置文件 {self.path}：{_os_reason(e)}") from e

        try:
            # utf-8-sig: tolerate a BOM left by Notepad / PowerShell 5 when edited by hand.
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise StoreError(f"配置文件不是有效的 UTF-8 JSON：{self.path}（{e}）") from e
        if not isinstance(data, dict):
            raise StoreError(f"配置文件格式错误：{self.path}（顶层必须是 JSON 对象）")

        version = data.get("schema_version")
        if (
            isinstance(version, int | float)
            and not isinstance(version, bool)
            and version > CONFIG_SCHEMA_VERSION
        ):
            raise StoreError(
                f"配置文件 {self.path} 由更新版本的 RunnerDeck 写入"
                f"（schema_version={version}，本版本支持 {CONFIG_SCHEMA_VERSION}）。"
                "为避免覆盖其中的内容，请升级 RunnerDeck 后再使用"
            )

        try:
            return AppConfig.model_validate(data)
        except ValidationError as e:
            raise StoreError(f"配置文件内容不合法：{self.path}（{_describe(e)}）") from e

    def save(self, config: AppConfig) -> None:
        """Atomic write: temp file in the same dir -> fsync -> os.replace (retried on Windows)."""
        payload = json.dumps(config.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
        data = payload.encode("utf-8")
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=f".{self.path.name}.", suffix=".tmp")
        except OSError as e:
            raise StoreError(f"无法在 {parent} 中写入配置文件：{_os_reason(e)}") from e

        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            _replace_with_retry(tmp, self.path)
        except OSError as e:
            _discard(tmp)
            if isinstance(e, PermissionError):
                reason = "文件被其他程序占用或没有写权限"
            else:
                reason = _os_reason(e)
            raise StoreError(f"无法写入配置文件 {self.path}：{reason}") from e
        except BaseException:
            _discard(tmp)
            raise
        log.debug("saved %s (%d runners)", self.path, len(config.runners))


def resolve_runner(config: AppConfig, key: str) -> RunnerRecord:
    """Find exactly one runner by id, id prefix (>= 6 chars, unique), name, or owner/repo.

    The first rule that matches anything wins; name and owner/repo compare case-insensitively.
    An id prefix shared by several runners only becomes an error if no later rule matches.
    """
    key = key.strip()
    if not key:
        raise RunnerNotFoundError("请指定 runner（id、id 前缀、名称或 owner/repo）")
    runners = config.runners

    exact = [r for r in runners if r.id == key]
    if exact:
        return _single(key, exact)

    by_prefix = [r for r in runners if r.id.startswith(key)] if len(key) >= MIN_ID_PREFIX else []
    if len(by_prefix) == 1:
        return by_prefix[0]

    folded = key.casefold()
    by_name = [r for r in runners if r.name.casefold() == folded]
    if by_name:
        return _single(key, by_name)
    by_repo = [r for r in runners if r.full_name.casefold() == folded]
    if by_repo:
        return _single(key, by_repo)

    if by_prefix:
        raise _ambiguous(key, by_prefix)
    raise RunnerNotFoundError(f"找不到 runner：{redact(key)}（可以用 id、名称或 owner/repo 指定）")


# --- helpers -------------------------------------------------------------------------


def _replace_with_retry(src: Path, dst: Path) -> None:
    for attempt in range(REPLACE_RETRIES + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == REPLACE_RETRIES:
                raise
            log.debug("%s is busy, retrying replace (%d/%d)", dst, attempt + 1, REPLACE_RETRIES)
            time.sleep(REPLACE_RETRY_DELAY_S)


def _discard(tmp: Path) -> None:
    with contextlib.suppress(OSError):
        tmp.unlink()


def _os_reason(e: OSError) -> str:
    return e.strerror or str(e) or type(e).__name__


def _describe(e: ValidationError) -> str:
    errors = e.errors(include_url=False, include_input=False)
    parts = [
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in errors[:_MAX_REPORTED_ERRORS]
    ]
    if len(errors) > _MAX_REPORTED_ERRORS:
        parts.append(f"另有 {len(errors) - _MAX_REPORTED_ERRORS} 处")
    return redact("；".join(parts))


def _single(key: str, matches: Sequence[RunnerRecord]) -> RunnerRecord:
    if len(matches) == 1:
        return matches[0]
    raise _ambiguous(key, matches)


def _ambiguous(key: str, matches: Sequence[RunnerRecord]) -> AmbiguousRunnerError:
    lines = "\n".join(f"  {r.id}  {r.name}  ({r.full_name})" for r in matches)
    return AmbiguousRunnerError(
        f"“{redact(key)}”匹配到 {len(matches)} 个 runner，请改用 id 指定：\n{lines}"
    )
