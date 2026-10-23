"""Tests for runnerdeck.core.release (metadata parsing and the download cache)."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

import pytest

from runnerdeck.core import release
from runnerdeck.core.errors import (
    ChecksumMismatchError,
    InvalidInputError,
    NetworkError,
    ReleaseError,
)
from runnerdeck.core.models import ReleaseInfo
from runnerdeck.core.release import (
    RUNNER_PLATFORM,
    ReleaseCache,
    extract_sha256_from_body,
    parse_release,
    sha256_file,
)

# Excerpt of the actions/runner v2.338.0 release notes (copied from tests/test_spike_common.py).
RELEASE_BODY = """\
## SHA-256 Checksums
- actions-runner-win-x64-2.338.0.zip <!-- BEGIN SHA win-x64 -->f48e0750a21812bca5f82de5f7f5aeae71abee647fab5a582f1742d07eba455f<!-- END SHA win-x64 -->
- actions-runner-win-arm64-2.338.0.zip <!-- BEGIN SHA win-arm64 -->a57a4d29c0dcf02210a16b01906ed76cb8091b26d728c41232736c0f627a557e<!-- END SHA win-arm64 -->
"""  # noqa: E501
WIN_X64_SHA = "f48e0750a21812bca5f82de5f7f5aeae71abee647fab5a582f1742d07eba455f"
WIN_ARM64_SHA = "a57a4d29c0dcf02210a16b01906ed76cb8091b26d728c41232736c0f627a557e"
ASSET = "actions-runner-win-x64-2.338.0.zip"
URL = f"https://github.com/actions/runner/releases/download/v2.338.0/{ASSET}"

_DEFAULT = object()


def make_release(
    *,
    digest: Any = _DEFAULT,
    body: Any = RELEASE_BODY,
    tag: Any = "v2.338.0",
    extra_assets: list[Any] | None = None,
    **asset_overrides: Any,
) -> dict[str, Any]:
    asset: dict[str, Any] = {
        "name": ASSET,
        "browser_download_url": URL,
        "size": 123_456_789,
        "digest": f"sha256:{WIN_X64_SHA}" if digest is _DEFAULT else digest,
    }
    asset.update(asset_overrides)
    if asset["digest"] is None:
        del asset["digest"]
    data: dict[str, Any] = {
        "tag_name": tag,
        "body": body,
        "assets": [
            {
                "name": "actions-runner-win-arm64-2.338.0.zip",
                "browser_download_url": URL.replace("x64", "arm64"),
                "size": 1,
                "digest": f"sha256:{WIN_ARM64_SHA}",
            },
            *(extra_assets or []),
            asset,
        ],
    }
    if tag is None:
        del data["tag_name"]
    return data


# --- extract_sha256_from_body ---------------------------------------------------------


def test_extract_sha256_picks_requested_platform():
    assert extract_sha256_from_body(RELEASE_BODY) == WIN_X64_SHA
    assert extract_sha256_from_body(RELEASE_BODY, "win-arm64") == WIN_ARM64_SHA


def test_extract_sha256_missing_platform():
    assert extract_sha256_from_body(RELEASE_BODY, "osx-x64") is None
    assert extract_sha256_from_body("") is None


def test_extract_sha256_escapes_platform():
    # "." must not act as a regex wildcard matching the "-" in win-x64.
    assert extract_sha256_from_body(RELEASE_BODY, "win.x64") is None


def test_extract_sha256_lowercases_and_tolerates_whitespace():
    body = f"<!--BEGIN SHA win-x64-->  {WIN_X64_SHA.upper()}\n<!--  END SHA win-x64  -->"
    assert extract_sha256_from_body(body) == WIN_X64_SHA


def test_extract_sha256_rejects_short_hex():
    body = "<!-- BEGIN SHA win-x64 -->abcdef<!-- END SHA win-x64 -->"
    assert extract_sha256_from_body(body) is None


# --- parse_release ----------------------------------------------------------------------------


def test_parse_release_uses_digest_and_strips_v():
    info = parse_release(make_release())
    assert info == ReleaseInfo(
        version="2.338.0",
        asset_name=ASSET,
        download_url=URL,
        size=123_456_789,
        sha256=WIN_X64_SHA,
    )
    assert RUNNER_PLATFORM == "win-x64"


def test_parse_release_digest_only_without_body():
    assert parse_release(make_release(body=None)).sha256 == WIN_X64_SHA


def test_parse_release_falls_back_to_body_without_digest():
    assert parse_release(make_release(digest=None)).sha256 == WIN_X64_SHA


def test_parse_release_uppercase_digest_matches_body():
    info = parse_release(make_release(digest=f"SHA256:{WIN_X64_SHA.upper()}"))
    assert info.sha256 == WIN_X64_SHA


def test_parse_release_digest_and_body_disagree():
    with pytest.raises(ReleaseError, match="不一致") as exc:
        parse_release(make_release(digest=f"sha256:{'0' * 64}"))
    assert not isinstance(exc.value, ChecksumMismatchError)


def test_parse_release_no_checksum_at_all():
    with pytest.raises(ReleaseError, match="SHA-256"):
        parse_release(make_release(digest=None, body="no markers here"))


@pytest.mark.parametrize("digest", ["sha512:" + "a" * 128, "sha256:nothex", "sha256:", "", 42])
def test_parse_release_unusable_digest_falls_back_to_body(digest):
    assert parse_release(make_release(digest=digest)).sha256 == WIN_X64_SHA


def test_parse_release_unusable_digest_and_no_body():
    with pytest.raises(ReleaseError):
        parse_release(make_release(digest="sha256:nothex", body=None))


def test_parse_release_other_platform():
    info = parse_release(make_release(), platform="win-arm64")
    assert info.asset_name == "actions-runner-win-arm64-2.338.0.zip"
    assert info.sha256 == WIN_ARM64_SHA


def test_parse_release_ignores_similar_asset_names():
    noise = [
        "not an asset",
        {"name": f"{ASSET}.sha256", "browser_download_url": URL, "size": 64},
        {"name": "actions-runner-win-x64-2.338.0-noexternals.zip", "size": 1},
        {"name": None},
    ]
    info = parse_release(make_release(extra_assets=noise))
    assert info.asset_name == ASSET
    assert info.size == 123_456_789


def test_parse_release_without_platform_asset():
    data = make_release()
    data["assets"] = [a for a in data["assets"] if a["name"] != ASSET]
    with pytest.raises(ReleaseError, match="win-x64"):
        parse_release(data)


@pytest.mark.parametrize("assets", [None, "oops", []])
def test_parse_release_bad_assets(assets):
    data = make_release()
    data["assets"] = assets
    with pytest.raises(ReleaseError):
        parse_release(data)


@pytest.mark.parametrize("tag", [None, "", "  ", 2338])
def test_parse_release_missing_tag(tag):
    with pytest.raises(ReleaseError, match="tag_name"):
        parse_release(make_release(tag=tag))


def test_parse_release_tag_without_v_prefix():
    assert parse_release(make_release(tag="2.338.0")).version == "2.338.0"


def test_parse_release_asset_version_differs_from_tag():
    with pytest.raises(ReleaseError, match="版本号"):
        parse_release(make_release(tag="v2.339.0"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"browser_download_url": None},
        {"browser_download_url": "http://insecure.example/x.zip"},
        {"size": None},
        {"size": 0},
        {"size": "123"},
        {"size": True},
    ],
)
def test_parse_release_incomplete_asset(overrides):
    with pytest.raises(ReleaseError, match=ASSET):
        parse_release(make_release(**overrides))


# --- sha256_file ---------------------------------------------------------------------------


def test_sha256_file_multi_chunk(tmp_path):
    data = b"runnerdeck" * 300_000  # ~3 MB, several read chunks
    path = tmp_path / "blob"
    path.write_bytes(data)
    assert sha256_file(path) == hashlib.sha256(data).hexdigest()


def test_sha256_file_empty(tmp_path):
    path = tmp_path / "empty"
    path.write_bytes(b"")
    assert sha256_file(path) == hashlib.sha256(b"").hexdigest()


def test_sha256_file_missing(tmp_path):
    with pytest.raises(ReleaseError, match="无法读取"):
        sha256_file(tmp_path / "nope")


# --- ReleaseCache -----------------------------------------------------------------------------

CONTENT = b"PK\x03\x04 fake runner zip " * 1000


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def info_for(content: bytes = CONTENT, *, version: str = "2.338.0") -> ReleaseInfo:
    name = f"actions-runner-win-x64-{version}.zip"
    return ReleaseInfo(
        version=version,
        asset_name=name,
        download_url=f"https://github.com/actions/runner/releases/download/v{version}/{name}",
        size=len(content),
        sha256=sha(content),
    )


class FakeClient:
    """Stands in for GitHubClient.download: writes `content`, returns its sha256."""

    def __init__(
        self,
        content: bytes = CONTENT,
        *,
        digest: Any = _DEFAULT,
        error: BaseException | None = None,
    ) -> None:
        self.content = content
        self.digest = digest
        self.error = error
        self.calls: list[tuple[str, Path]] = []
        self.existed_before: list[bool] = []

    def download(self, url: str, dest: Path, on_progress=None) -> str:
        self.calls.append((url, dest))
        self.existed_before.append(dest.exists())
        half = len(self.content) // 2
        dest.write_bytes(self.content[:half])
        if on_progress is not None:
            on_progress(half, len(self.content))
        if self.error is not None:
            raise self.error  # deliberately leaves the partial file behind
        dest.write_bytes(self.content)
        if on_progress is not None:
            on_progress(len(self.content), len(self.content))
        return sha(self.content) if self.digest is _DEFAULT else self.digest


@pytest.fixture
def cache(tmp_path) -> ReleaseCache:
    return ReleaseCache(tmp_path / "cache")


def leftovers(cache: ReleaseCache) -> list[str]:
    if not cache.cache_dir.exists():
        return []
    return sorted(p.name for p in cache.cache_dir.iterdir())


def test_default_cache_dir_follows_runnerdeck_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNNERDECK_HOME", str(tmp_path))
    assert ReleaseCache().cache_dir == tmp_path / "cache"


def test_cached_path(cache):
    assert cache.cached_path(info_for()) == cache.cache_dir / "actions-runner-win-x64-2.338.0.zip"


@pytest.mark.parametrize("name", ["../evil.zip", "sub/x.zip", "..", ""])
def test_cached_path_rejects_unsafe_names(cache, name):
    info = info_for().model_copy(update={"asset_name": name})
    with pytest.raises(ReleaseError):
        cache.cached_path(info)


def test_is_cached(cache):
    info = info_for()
    assert not cache.is_cached(info)
    cache.cache_dir.mkdir()
    path = cache.cached_path(info)
    path.write_bytes(CONTENT)
    assert cache.is_cached(info)
    assert cache.is_cached(info.model_copy(update={"sha256": info.sha256.upper()}))
    path.write_bytes(CONTENT + b"x")
    assert not cache.is_cached(info)


def test_is_cached_directory_in_place_of_zip(cache):
    info = info_for()
    cache.cached_path(info).mkdir(parents=True)
    assert not cache.is_cached(info)


def test_is_cached_unreadable(cache, monkeypatch):
    info = info_for()
    cache.cache_dir.mkdir()
    cache.cached_path(info).write_bytes(CONTENT)

    def boom(path):
        raise ReleaseError("无法读取")

    monkeypatch.setattr(release, "sha256_file", boom)
    assert not cache.is_cached(info)


def test_ensure_cache_hit_does_not_download(cache):
    info = info_for()
    cache.cache_dir.mkdir()
    cache.cached_path(info).write_bytes(CONTENT)
    client = FakeClient()
    assert cache.ensure(client, info) == cache.cached_path(info)
    assert client.calls == []


def test_ensure_downloads_verifies_and_moves(cache):
    info = info_for()
    client = FakeClient()
    progress: list[tuple[int, int | None]] = []

    path = cache.ensure(
        client, info, on_progress=lambda done, total: progress.append((done, total))
    )

    assert path == cache.cached_path(info)
    assert path.read_bytes() == CONTENT
    assert client.calls == [(info.download_url, path.with_name(path.name + ".part"))]
    assert progress == [(len(CONTENT) // 2, len(CONTENT)), (len(CONTENT), len(CONTENT))]
    assert leftovers(cache) == [info.asset_name]
    # Second call is a cache hit.
    cache.ensure(client, info)
    assert len(client.calls) == 1


def test_ensure_redownloads_corrupt_cache(cache):
    info = info_for()
    cache.cache_dir.mkdir()
    cache.cached_path(info).write_bytes(b"truncated")
    client = FakeClient()

    path = cache.ensure(client, info)

    assert len(client.calls) == 1
    assert path.read_bytes() == CONTENT
    assert leftovers(cache) == [info.asset_name]


def test_ensure_checksum_mismatch_leaves_nothing(cache):
    info = info_for()
    tampered = CONTENT + b"proxy was here"
    client = FakeClient(tampered)

    with pytest.raises(ChecksumMismatchError) as exc:
        cache.ensure(client, info)

    assert info.sha256 in str(exc.value)
    assert sha(tampered) in str(exc.value)
    assert leftovers(cache) == []


def test_ensure_checksum_mismatch_keeps_old_file_untouched(cache):
    # A corrupt cached zip is only ever replaced by a verified one.
    info = info_for()
    cache.cache_dir.mkdir()
    cache.cached_path(info).write_bytes(b"old corrupt")
    with pytest.raises(ChecksumMismatchError):
        cache.ensure(FakeClient(b"also wrong"), info)
    assert cache.cached_path(info).read_bytes() == b"old corrupt"
    assert leftovers(cache) == [info.asset_name]


@pytest.mark.parametrize("digest", [None, ""])
def test_ensure_client_returns_no_digest(cache, digest):
    with pytest.raises(ChecksumMismatchError):
        cache.ensure(FakeClient(digest=digest), info_for())
    assert leftovers(cache) == []


def test_ensure_accepts_uppercase_digest(cache):
    info = info_for()
    path = cache.ensure(FakeClient(digest=sha(CONTENT).upper()), info)
    assert path.read_bytes() == CONTENT


def test_ensure_removes_stale_part_first(cache):
    info = info_for()
    cache.cache_dir.mkdir()
    part = cache.cached_path(info).with_name(info.asset_name + ".part")
    part.write_bytes(b"from a crashed run")
    client = FakeClient()
    cache.ensure(client, info)
    assert client.existed_before == [False]
    assert leftovers(cache) == [info.asset_name]


def test_ensure_network_error_propagates_and_cleans_up(cache):
    client = FakeClient(error=NetworkError("下载失败"))
    with pytest.raises(NetworkError):
        cache.ensure(client, info_for())
    assert leftovers(cache) == []


def test_ensure_keyboard_interrupt_cleans_up(cache):
    with pytest.raises(KeyboardInterrupt):
        cache.ensure(FakeClient(error=KeyboardInterrupt()), info_for())
    assert leftovers(cache) == []


def test_ensure_os_error_becomes_release_error(cache):
    client = FakeClient(error=OSError(28, "No space left on device"))
    with pytest.raises(ReleaseError, match="No space left") as exc:
        cache.ensure(client, info_for())
    assert isinstance(exc.value.__cause__, OSError)
    assert leftovers(cache) == []


def test_ensure_cache_dir_is_a_file(tmp_path):
    blocker = tmp_path / "cache"
    blocker.write_text("not a dir")
    with pytest.raises(ReleaseError, match="缓存目录"):
        ReleaseCache(blocker).ensure(FakeClient(), info_for())


def test_ensure_retries_transient_permission_error(cache, monkeypatch):
    real_replace = release.os.replace
    attempts: list[int] = []

    def flaky_replace(src, dst):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError(13, "in use by another process")
        real_replace(src, dst)

    monkeypatch.setattr(release.os, "replace", flaky_replace)
    monkeypatch.setattr(release, "_REPLACE_DELAY_S", 0)
    info = info_for()
    assert cache.ensure(FakeClient(), info).read_bytes() == CONTENT
    assert len(attempts) == 3
    assert leftovers(cache) == [info.asset_name]


def test_ensure_persistent_permission_error(cache, monkeypatch):
    calls: list[int] = []

    def locked(src, dst):
        calls.append(1)
        raise PermissionError(13, "in use by another process")

    monkeypatch.setattr(release.os, "replace", locked)
    monkeypatch.setattr(release, "_REPLACE_DELAY_S", 0)
    with pytest.raises(ReleaseError, match="无法保存"):
        cache.ensure(FakeClient(), info_for())
    assert len(calls) == release._REPLACE_ATTEMPTS
    assert leftovers(cache) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_ensure_target_held_open_on_windows(cache, monkeypatch):
    # Python's open() does not share delete access, so os.replace onto an open file fails.
    monkeypatch.setattr(release, "_REPLACE_DELAY_S", 0)
    info = info_for()
    cache.cache_dir.mkdir()
    target = cache.cached_path(info)
    target.write_bytes(b"corrupt")
    with target.open("rb"), pytest.raises(ReleaseError, match="无法保存"):
        cache.ensure(FakeClient(), info)
    assert target.read_bytes() == b"corrupt"
    assert leftovers(cache) == [info.asset_name]


# --- prune ------------------------------------------------------------------------------------


def populate(cache: ReleaseCache, names: list[str]) -> None:
    cache.cache_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (cache.cache_dir / name).write_bytes(b"zip")


def zips(*versions: str) -> list[str]:
    return [f"actions-runner-win-x64-{v}.zip" for v in versions]


def test_prune_orders_versions_numerically(cache):
    populate(cache, zips("2.9.0", "2.99.0", "2.338.0", "2.337.0"))
    removed = cache.prune(keep=2)
    assert sorted(p.name for p in removed) == sorted(zips("2.99.0", "2.9.0"))
    assert leftovers(cache) == sorted(zips("2.337.0", "2.338.0"))


def test_prune_default_keep_is_two(cache):
    populate(cache, zips("2.336.0", "2.337.0", "2.338.0"))
    assert [p.name for p in cache.prune()] == zips("2.336.0")


def test_prune_never_touches_unrelated_files(cache):
    others = [
        "notes.txt",
        "actions-runner-win-x64-latest.zip",
        "actions-runner-win-x64-2.338.0.zip.part",
        "actions-runner-win-x64-2.1.zip",
    ]
    populate(cache, [*others, *zips("2.1.0", "2.2.0")])
    (cache.cache_dir / "actions-runner-win-x64-1.0.0.zip").mkdir()
    removed = cache.prune(keep=0)
    assert sorted(p.name for p in removed) == sorted(zips("2.1.0", "2.2.0"))
    assert leftovers(cache) == sorted([*others, "actions-runner-win-x64-1.0.0.zip"])


def test_prune_keep_more_than_present(cache):
    populate(cache, zips("2.338.0"))
    assert cache.prune(keep=5) == []
    assert leftovers(cache) == zips("2.338.0")


def test_prune_missing_cache_dir(cache):
    assert cache.prune() == []


def test_prune_negative_keep(cache):
    with pytest.raises(InvalidInputError):
        cache.prune(keep=-1)


def test_prune_skips_files_it_cannot_delete(cache, monkeypatch):
    populate(cache, zips("2.1.0", "2.2.0", "2.3.0"))
    real_unlink = Path.unlink
    locked = cache.cache_dir / zips("2.1.0")[0]

    def unlink(self, missing_ok=False):
        if self == locked:
            raise PermissionError(13, "in use")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    removed = cache.prune(keep=1)
    assert [p.name for p in removed] == zips("2.2.0")
    assert leftovers(cache) == sorted(zips("2.1.0", "2.3.0"))
