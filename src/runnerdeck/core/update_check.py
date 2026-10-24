"""Is there a newer RunnerDeck release? (docs/m4-design.md §8; only tells, never installs.)"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from runnerdeck.core.models import UpdateInfo

APP_REPO = ("nablejohne", "RunnerDeck")
RELEASES_URL = "https://github.com/nablejohne/RunnerDeck/releases"
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(value: str) -> tuple[int, int, int] | None:
    """x.y.z (an optional leading v); None for anything else (pre-releases included)."""
    match = _VERSION_RE.fullmatch(value.strip())
    return None if match is None else (int(match[1]), int(match[2]), int(match[3]))


def compare(current: str, release: Mapping[str, Any] | None) -> UpdateInfo:
    """`release`: GitHub's latest-release JSON (None: the repo has no release yet)."""
    if release is None:
        return UpdateInfo(current=current)
    tag = str(release.get("tag_name") or "")
    url = str(release.get("html_url") or "")
    if not url.startswith(RELEASES_URL + "/"):  # only ever open our own release page
        url = RELEASES_URL
    latest, mine = parse_version(tag), parse_version(current)
    if latest is None:
        return UpdateInfo(current=current, error=f"无法识别最新版本号：{tag!r}")
    newer = mine is not None and latest > mine
    return UpdateInfo(current=current, latest=".".join(map(str, latest)), newer=newer, url=url)
