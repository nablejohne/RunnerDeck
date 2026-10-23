"""actions/runner release metadata and the local download cache.

Spike §2: a release carries two SHA-256 sources, the asset `digest` ("sha256:...") and a
`<!-- BEGIN SHA win-x64 -->` marker in the release notes. The digest wins, the body is the
fallback, and a disagreement between them is treated as a broken release.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from runnerdeck.core import paths
from runnerdeck.core.errors import (
    ChecksumMismatchError,
    InvalidInputError,
    ReleaseError,
)
from runnerdeck.core.models import ReleaseInfo

log = logging.getLogger(__name__)

RUNNER_PLATFORM = "win-x64"
PART_SUFFIX = ".part"

_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# Any platform: prune() orders whatever runner zips are in the cache.
_CACHED_ZIP_RE = re.compile(r"^actions-runner-.+-(\d+)\.(\d+)\.(\d+)\.zip$")
_CHUNK = 1024 * 1024
# Windows: a freshly written file may be held briefly (antivirus, indexer) -> PermissionError.
_REPLACE_ATTEMPTS = 5
_REPLACE_DELAY_S = 0.1

DownloadProgress = Callable[[int, int | None], None]


class Downloader(Protocol):
    """The part of GitHubClient that ReleaseCache uses."""

    def download(
        self, url: str, dest: Path, on_progress: DownloadProgress | None = None
    ) -> str: ...


# --- release metadata --------------------------------------------------------------


def extract_sha256_from_body(body: str, platform: str = RUNNER_PLATFORM) -> str | None:
    """Checksum embedded as `<!-- BEGIN SHA {platform} -->hex<!-- END SHA {platform} -->`."""
    p = re.escape(platform)
    m = re.search(
        rf"<!--\s*BEGIN SHA {p}\s*-->\s*([0-9a-fA-F]{{64}})\s*<!--\s*END SHA {p}\s*-->",
        body,
    )
    return m.group(1).lower() if m else None


def _digest_sha256(asset: Mapping[str, Any]) -> str | None:
    """`"sha256:<hex>"` -> hex; anything else (absent, other algorithm, malformed) -> None."""
    digest = asset.get("digest")
    if not isinstance(digest, str) or not digest:
        return None
    algo, _, value = digest.partition(":")
    if algo.lower() != "sha256" or not _HEX64_RE.fullmatch(value):
        log.warning("ignoring unusable asset digest for %s", asset.get("name"))
        return None
    return value.lower()


def parse_release(data: Mapping[str, Any], platform: str = RUNNER_PLATFORM) -> ReleaseInfo:
    """Pick the `{platform}` zip out of a GitHub release JSON.

    The asset's version must equal the tag (minus its leading "v"), otherwise ReleaseError.
    """
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        raise ReleaseError("GitHub 返回的 runner 发布信息缺少版本号（tag_name）")
    tag = tag.strip()
    version = tag.removeprefix("v")

    pattern = re.compile(rf"^actions-runner-{re.escape(platform)}-(\d+\.\d+\.\d+)\.zip$")
    assets = data.get("assets")
    asset: Mapping[str, Any] | None = None
    asset_version = ""
    if isinstance(assets, list):
        for candidate in assets:
            if not isinstance(candidate, Mapping):
                continue
            m = pattern.match(str(candidate.get("name") or ""))
            if m:
                asset, asset_version = candidate, m.group(1)
                break
    if asset is None:
        raise ReleaseError(f"runner 版本 {tag} 没有 {platform} 安装包")
    name = str(asset["name"])
    if asset_version != version:
        raise ReleaseError(f"runner 版本 {tag} 的安装包 {name} 版本号与发布版本不一致")

    url = asset.get("browser_download_url")
    size = asset.get("size")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ReleaseError(f"安装包 {name} 缺少下载地址")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ReleaseError(f"安装包 {name} 缺少文件大小")

    from_digest = _digest_sha256(asset)
    body = data.get("body")
    from_body = extract_sha256_from_body(body, platform) if isinstance(body, str) else None
    if from_digest and from_body and from_digest != from_body:
        raise ReleaseError(
            f"安装包 {name} 的两个 SHA-256 来源不一致（资产 digest 与发布说明），"
            "为安全起见不使用该版本"
        )
    sha256 = from_digest or from_body
    if not sha256:
        raise ReleaseError(f"安装包 {name} 没有可用的 SHA-256 校验值，无法安全下载")

    return ReleaseInfo(version=version, asset_name=name, download_url=url, size=size, sha256=sha256)


def sha256_file(path: Path) -> str:
    """Lowercase hex SHA-256 of a file. OSError -> ReleaseError."""
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while chunk := f.read(_CHUNK):
                h.update(chunk)
    except OSError as e:
        raise ReleaseError(f"无法读取文件 {path}：{e.strerror or e}") from e
    return h.hexdigest()


def _version_key(name: str) -> tuple[int, int, int] | None:
    m = _CACHED_ZIP_RE.match(name)
    if m is None:
        return None
    major, minor, patch = m.groups()
    return int(major), int(minor), int(patch)


# --- cache ------------------------------------------------------------------------------


class ReleaseCache:
    """Verified runner zips under %LOCALAPPDATA%\\RunnerDeck\\cache.

    Not safe for two concurrent ensure() calls on the same version (they share `<name>.part`);
    callers serialize installs.
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir if cache_dir is not None else paths.cache_dir()

    def cached_path(self, info: ReleaseInfo) -> Path:
        name = info.asset_name
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ReleaseError(f"安装包文件名不合法：{name!r}")
        return self.cache_dir / name

    def is_cached(self, info: ReleaseInfo) -> bool:
        """The zip exists and its SHA-256 matches `info` (re-hashed on every call)."""
        path = self.cached_path(info)
        if not path.is_file():
            return False
        try:
            return sha256_file(path) == info.sha256.lower()
        except ReleaseError:
            log.warning("cached runner zip unreadable: %s", path)
            return False

    def ensure(
        self,
        client: Downloader,
        info: ReleaseInfo,
        on_progress: DownloadProgress | None = None,
    ) -> Path:
        """Return the verified zip, downloading it first if missing or corrupt.

        Downloads to `<name>.part`, compares the SHA-256 that client.download() computed while
        streaming, then moves it into place. On any failure no `.part` is left behind.
        on_progress(done, total) runs in the calling thread (GitHubClient.download contract).
        """
        path = self.cached_path(info)
        if self.is_cached(info):
            log.info("runner %s already cached: %s", info.version, path)
            return path

        part = path.with_name(path.name + PART_SUFFIX)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            part.unlink(missing_ok=True)  # leftover from an interrupted run
        except OSError as e:
            raise ReleaseError(f"无法准备下载缓存目录 {self.cache_dir}：{e.strerror or e}") from e

        log.info("downloading runner %s (%d bytes)", info.version, info.size)
        try:
            actual = client.download(info.download_url, part, on_progress)
        except OSError as e:
            _discard(part)
            raise ReleaseError(f"保存安装包 {info.asset_name} 失败：{e.strerror or e}") from e
        except BaseException:  # NetworkError from the client, KeyboardInterrupt, ...
            _discard(part)
            raise

        expected = info.sha256.lower()
        if not isinstance(actual, str) or actual.lower() != expected:
            _discard(part)
            raise ChecksumMismatchError(
                f"安装包 {info.asset_name} 校验失败（期望 SHA-256 {expected}，实际 {actual}），"
                "已删除下载的文件。可能是下载不完整或被代理篡改，请重试。"
            )

        try:
            _replace_with_retry(part, path)
        except OSError as e:
            _discard(part)
            raise ReleaseError(f"无法保存安装包到 {path}：{e.strerror or e}") from e
        log.info("runner %s cached: %s", info.version, path)
        return path

    def prune(self, keep: int = 2) -> list[Path]:
        """Delete all but the `keep` newest `actions-runner-*.zip` (numeric version order).

        Files without a parseable version are never touched; files that cannot be deleted
        (e.g. in use on Windows) are skipped. Returns the paths actually deleted.
        """
        if keep < 0:
            raise InvalidInputError(f"keep 不能为负数：{keep}")
        if not self.cache_dir.is_dir():
            return []
        versioned: list[tuple[tuple[int, int, int], str, Path]] = []
        for entry in self.cache_dir.glob("actions-runner-*.zip"):
            key = _version_key(entry.name)
            if key is not None and entry.is_file():
                versioned.append((key, entry.name, entry))
        versioned.sort(reverse=True)

        removed: list[Path] = []
        for _, _, entry in versioned[keep:]:
            try:
                entry.unlink()
            except OSError as e:
                log.warning("could not prune %s: %s", entry, e)
                continue
            log.info("pruned cached runner zip %s", entry.name)
            removed.append(entry)
        return removed


def _discard(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _replace_with_retry(src: Path, dst: Path) -> None:
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS:
                raise
            time.sleep(_REPLACE_DELAY_S)
