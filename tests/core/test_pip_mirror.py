from __future__ import annotations

import pytest

from runnerdeck.core import pip_mirror
from runnerdeck.core.errors import InvalidInputError, NetworkError
from runnerdeck.core.models import DownloadSample, MirrorSpeed

TENCENT = "https://mirrors.cloud.tencent.com/pypi/simple/"
CUSTOM = "https://pypi.example.com/simple/"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", None),
        ("  ", None),
        (None, None),
        ("none", None),
        ("OFF", None),
        ("tencent", TENCENT),
        ("Tencent", TENCENT),
        ("  https://pypi.example.com/simple/ ", CUSTOM),
        ("http://10.0.0.5:8080/simple", "http://10.0.0.5:8080/simple"),
    ],
)
def test_normalize_index_url(text: str | None, expected: str | None) -> None:
    assert pip_mirror.normalize_index_url(text) == expected


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("ftp://pypi.example.com/simple/", "http(s)"),
        ("pypi.example.com/simple/", "http(s)"),
        ("https://", "http(s)"),
        ("https://host:99999/simple/", "不合法"),
        ("https://user:secret@pypi.example.com/simple/", "用户名或密码"),
    ],
)
def test_normalize_index_url_rejects(text: str, needle: str) -> None:
    with pytest.raises(InvalidInputError, match=needle):
        pip_mirror.normalize_index_url(text)


def test_presets_are_distinct_and_https() -> None:
    keys = [m.key for m in pip_mirror.PIP_MIRRORS]
    assert len(set(keys)) == len(keys)
    assert all(m.index_url.startswith("https://") for m in pip_mirror.PIP_MIRRORS)
    assert all(m.index_url.endswith("/simple/") for m in pip_mirror.PIP_MIRRORS)


def test_labels_and_preset_matching() -> None:
    assert pip_mirror.label_for(None) == pip_mirror.NOT_SET_LABEL
    assert pip_mirror.label_for(TENCENT) == "腾讯云"
    # trailing slash and case do not matter for a preset
    loose = "HTTPS://mirrors.cloud.tencent.com/pypi/simple"
    assert pip_mirror.label_for(loose) == "腾讯云"
    assert pip_mirror.option_value(loose) == TENCENT
    assert pip_mirror.label_for(CUSTOM) == "自定义（pypi.example.com）"
    assert pip_mirror.option_value(CUSTOM) == CUSTOM
    assert pip_mirror.option_value(None) is None


def test_options_keep_a_custom_value() -> None:
    values = [v for v, _ in pip_mirror.options(None)]
    assert values[0] is None
    assert values[1:] == [m.index_url for m in pip_mirror.PIP_MIRRORS]
    assert pip_mirror.options(TENCENT) == pip_mirror.options(None)
    assert pip_mirror.options(CUSTOM)[-1] == (CUSTOM, "自定义（pypi.example.com）")


def test_candidates() -> None:
    presets = [m.index_url for m in pip_mirror.PIP_MIRRORS]
    assert pip_mirror.candidates(None) == presets
    assert pip_mirror.candidates(TENCENT) == presets
    assert pip_mirror.candidates(CUSTOM) == [*presets, CUSTOM]


@pytest.mark.parametrize(
    ("index_url", "base"),
    [
        (TENCENT, "https://mirrors.cloud.tencent.com/pypi/packages/"),
        (
            "https://repo.huaweicloud.com/repository/pypi/simple/",
            "https://repo.huaweicloud.com/repository/pypi/packages/",
        ),
        (
            "https://pypi.tuna.tsinghua.edu.cn/simple/",
            "https://pypi.tuna.tsinghua.edu.cn/packages/",
        ),
        ("https://mirrors.ustc.edu.cn/pypi/simple/", "https://mirrors.ustc.edu.cn/pypi/packages/"),
        ("https://mirrors.aliyun.com/pypi/simple", "https://mirrors.aliyun.com/pypi/packages/"),
        ("https://pypi.org/simple/", "https://files.pythonhosted.org/packages/"),
        ("http://10.0.0.5:8080/simple", "http://10.0.0.5:8080/packages/"),
    ],
)
def test_probe_url(index_url: str, base: str) -> None:
    assert pip_mirror.probe_url(index_url) == base + pip_mirror.PROBE_PATH


def test_probe_url_needs_a_simple_path() -> None:
    with pytest.raises(InvalidInputError, match="/simple/"):
        pip_mirror.probe_url("https://pypi.example.com/repo/")


def test_measure_mirrors_runs_each_in_turn_and_keeps_failures() -> None:
    asked: list[str] = []

    def measure(url: str) -> DownloadSample:
        asked.append(url)
        if "ustc" in url:
            raise NetworkError("无法连接到镜像源")
        if "aliyun" in url:
            return DownloadSample(0, 0.0)
        return DownloadSample(8 * 1024 * 1024, 2.0)

    urls = [*pip_mirror.candidates(None)[:5], "https://pypi.example.com/repo/"]
    results = pip_mirror.measure_mirrors(urls, measure)

    assert [r.index_url for r in results] == urls
    assert asked == [pip_mirror.probe_url(u) for u in urls[:5]]  # a bad custom path is not fetched
    by_label = {r.label: r for r in results}
    assert by_label["腾讯云"].mb_per_s == pytest.approx(4.0)
    assert by_label["中国科学技术大学"].error == "无法连接到镜像源"
    assert by_label["阿里云"].error == "没有收到数据"
    assert "/simple/" in (by_label["自定义（pypi.example.com）"].error or "")


def test_ranked_puts_the_fastest_first_and_failures_last() -> None:
    slow = MirrorSpeed("a", "A", 1024 * 1024, 2.0)
    fast = MirrorSpeed("b", "B", 10 * 1024 * 1024, 1.0)
    broken = MirrorSpeed("c", "C", error="x")
    empty = MirrorSpeed("d", "D")
    assert pip_mirror.ranked([broken, slow, empty, fast]) == [fast, slow, broken, empty]
    assert slow.mb_per_s == pytest.approx(0.5)
    assert broken.mb_per_s is None and empty.mb_per_s is None
