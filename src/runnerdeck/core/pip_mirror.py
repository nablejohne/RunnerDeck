"""pip mirrors for CI jobs: presets, settings.pip_index_url parsing and the speed test.

settings.pip_index_url is written into every runner's `.env` as PIP_INDEX_URL (manager), which
pip in a job prefers over the machine's pip.ini. The speed test downloads the same wheel from
each mirror; the network part is a `measure` callable (GitHubClient.measure_download), so this
module stays pure and the UI may import it.

Measured on XM (2026-10-26, scipy wheel): Aliyun 0.41 MB/s, Tencent 29, Huawei 23, Tsinghua 18,
USTC 10, files.pythonhosted.org 0.42 direct / 7.8 through the proxy. Mirrors throttle per
network, so the test is worth repeating on each machine.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from runnerdeck.core.errors import InvalidInputError, RunnerDeckError
from runnerdeck.core.models import DownloadSample, MirrorSpeed


@dataclass(frozen=True)
class PipMirror:
    key: str  # accepted by `runnerdeck-cli settings set pip_index_url <key>`
    label: str
    index_url: str


PIP_MIRRORS: tuple[PipMirror, ...] = (
    PipMirror("tencent", "腾讯云", "https://mirrors.cloud.tencent.com/pypi/simple/"),
    PipMirror("huawei", "华为云", "https://repo.huaweicloud.com/repository/pypi/simple/"),
    PipMirror("tsinghua", "清华大学", "https://pypi.tuna.tsinghua.edu.cn/simple/"),
    PipMirror("ustc", "中国科学技术大学", "https://mirrors.ustc.edu.cn/pypi/simple/"),
    PipMirror("aliyun", "阿里云", "https://mirrors.aliyun.com/pypi/simple/"),
    PipMirror("pypi", "PyPI 官方", "https://pypi.org/simple/"),
)

NOT_SET_LABEL = "不设置（用本机的 pip 配置）"
_OFF_WORDS = frozenset({"none", "off", "-"})

# A 39.3 MB wheel every mirror carries under the same path (PyPI file paths never change).
PROBE_PATH = (
    "d1/84/55bc4881973d3f79b479a5a2e2df61c8c9a04fcb986a213ac9c02cfb659b/"
    "scipy-1.15.3-cp310-cp310-win_amd64.whl"
)
PROBE_MAX_BYTES = 16 * 1024 * 1024
PROBE_MAX_SECONDS = 5.0
PROBE_TIMEOUT_S = 10.0  # per request: an unreachable mirror must not hold up the others


def _same(a: str, b: str) -> bool:
    return a.rstrip("/").casefold() == b.rstrip("/").casefold()


def preset_for(index_url: str | None) -> PipMirror | None:
    if not index_url:
        return None
    return next((m for m in PIP_MIRRORS if _same(m.index_url, index_url)), None)


def label_for(index_url: str | None) -> str:
    if not index_url:
        return NOT_SET_LABEL
    preset = preset_for(index_url)
    if preset is not None:
        return preset.label
    return f"自定义（{urlsplit(index_url).hostname or index_url}）"


def normalize_index_url(text: str | None) -> str | None:
    """settings.pip_index_url from user input: None for empty / none / off, a preset key
    (tencent, ...) for its URL, otherwise an http(s) URL without credentials (it goes into
    `.env` as plain text). Raises InvalidInputError."""
    value = (text or "").strip()
    if not value or value.casefold() in _OFF_WORDS:
        return None
    preset = next((m for m in PIP_MIRRORS if m.key == value.casefold()), None)
    if preset is not None:
        return preset.index_url
    try:
        parts = urlsplit(value)
        _ = parts.port  # ValueError for a port that is not a number in range
    except ValueError:
        raise InvalidInputError(f"pip 镜像源地址不合法：{value}") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InvalidInputError(f"pip 镜像源必须是 http(s) 地址，例如 {PIP_MIRRORS[0].index_url}")
    if parts.username or parts.password:
        raise InvalidInputError("pip 镜像源地址不能带用户名或密码（会明文写进 runner 的 .env）")
    return value


def options(current: str | None) -> list[tuple[str | None, str]]:
    """(value, text) pairs for the settings ComboBox; a custom current value is kept."""
    items: list[tuple[str | None, str]] = [(None, NOT_SET_LABEL)]
    items += [(m.index_url, m.label) for m in PIP_MIRRORS]
    if current and preset_for(current) is None:
        items.append((current, label_for(current)))
    return items


def option_value(current: str | None) -> str | None:
    """The ComboBox value standing for `current` (a preset is matched loosely)."""
    preset = preset_for(current)
    return preset.index_url if preset is not None else current


def candidates(current: str | None) -> list[str]:
    """Index URLs the speed test measures: every preset, plus a custom current value."""
    urls = [m.index_url for m in PIP_MIRRORS]
    if current and preset_for(current) is None:
        urls.append(current)
    return urls


def probe_url(index_url: str) -> str:
    """The wheel to download from `index_url`: mirrors serve files next to /simple/ under
    /packages/ (pypi.org serves them from files.pythonhosted.org)."""
    parts = urlsplit(index_url)
    if parts.hostname == "pypi.org":
        return f"https://files.pythonhosted.org/packages/{PROBE_PATH}"
    segments = parts.path.rstrip("/").split("/")
    if segments[-1] != "simple":
        raise InvalidInputError(f"无法测速：{index_url} 的路径不是以 /simple/ 结尾")
    path = "/".join([*segments[:-1], "packages", PROBE_PATH])
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def measure_mirrors(
    index_urls: Iterable[str], measure: Callable[[str], DownloadSample]
) -> list[MirrorSpeed]:
    """Measure each mirror in turn (in parallel they would share the bandwidth). A mirror that
    fails is reported with its error; the others still run."""
    results: list[MirrorSpeed] = []
    for url in index_urls:
        label = label_for(url)
        try:
            sample = measure(probe_url(url))
        except RunnerDeckError as e:
            results.append(MirrorSpeed(url, label, error=str(e)))
            continue
        if sample.received <= 0 or sample.seconds <= 0:
            results.append(MirrorSpeed(url, label, error="没有收到数据"))
        else:
            results.append(MirrorSpeed(url, label, sample.received, sample.seconds))
    return results


def ranked(results: Sequence[MirrorSpeed]) -> list[MirrorSpeed]:
    """Fastest first; failed mirrors last, in their original order."""
    ok = sorted((r for r in results if r.mb_per_s is not None), key=lambda r: -(r.mb_per_s or 0))
    return ok + [r for r in results if r.mb_per_s is None]
