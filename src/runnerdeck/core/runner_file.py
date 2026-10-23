"""Reading a runner directory: `.runner`, configured/runner-dir checks, scanning a root.

Never opens `.credentials*` (CLAUDE.md 安全): existence is checked with stat() only.
`config.cmd remove` deletes `.runner` (spike §3), so read it before unconfiguring.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from runnerdeck.core.errors import InvalidInputError, RunnerDeckError, RunnerFileError
from runnerdeck.core.models import RunnerFileInfo, validate_owner_repo

log = logging.getLogger(__name__)

RUNNER_FILE = ".runner"
CREDENTIALS_FILE = ".credentials"
GITHUB_HOST = "github.com"
# Two-segment URLs that are not repos: enterprise runners are configured with
# https://github.com/enterprises/{slug}; /orgs/{org} is the org page.
_NON_REPO_PREFIXES = frozenset({"enterprises", "orgs"})


def read_runner_file(runner_dir: Path) -> RunnerFileInfo | None:
    """Parse `runner_dir/.runner` (UTF-8, usually with BOM). Missing -> None.

    Unreadable, invalid JSON, not an object, or required fields missing -> RunnerFileError.
    `raw` holds the whole decoded object.
    """
    path = runner_dir / RUNNER_FILE
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except UnicodeDecodeError as e:
        raise RunnerFileError(f"{path} 不是 UTF-8 文本，无法解析") from e
    except OSError as e:
        raise RunnerFileError(f"无法读取 {path}：{e.strerror or e}") from e

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RunnerFileError(f"{path} 不是有效的 JSON（第 {e.lineno} 行）") from e
    if not isinstance(data, dict):
        raise RunnerFileError(f"{path} 的内容不是 JSON 对象")

    try:
        return RunnerFileInfo.model_validate({**data, "raw": data})
    except ValidationError as e:
        fields = sorted({str(err["loc"][0]) for err in e.errors() if err["loc"]})
        raise RunnerFileError(f"{path} 缺少或包含无效字段：{', '.join(fields) or '未知'}") from e


def parse_github_url(url: str) -> tuple[str, str]:
    """`https://github.com/{owner}/{repo}` -> (owner, repo).

    Accepted: surrounding whitespace, any case in scheme/host, trailing "/", one trailing
    ".git" (GitHub never allows a repo name ending in .git), query/fragment (ignored).
    Rejected with InvalidInputError: other schemes or hosts, user:password@, ports,
    user/org/enterprise URLs (only repo-level runners are supported), deeper paths such as
    /tree/main, empty segments.
    """
    value = url.strip()
    try:
        parts = urlsplit(value)
        has_userinfo = parts.username is not None or parts.password is not None
        port = parts.port
    except ValueError as e:  # e.g. non-numeric port
        raise InvalidInputError(f"不是有效的 GitHub 仓库地址：{value!r}") from e
    if has_userinfo:
        # Never echo the URL: the userinfo may be a credential.
        raise InvalidInputError("GitHub 仓库地址不能包含用户名或密码")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host != GITHUB_HOST or port is not None:
        raise InvalidInputError(f"只支持 https://github.com/owner/repo 形式的仓库地址：{value!r}")

    # "/owner/repo" or "/owner/repo/" -> ["owner", "repo"]; empty segments ("//") stay and fail.
    segments = parts.path.removesuffix("/").split("/")[1:]
    if len(segments) == 1 and segments[0]:
        raise InvalidInputError(
            f"这是用户或组织地址，不是仓库地址（只支持仓库级 runner）：{value!r}"
        )
    if len(segments) != 2:
        raise InvalidInputError(f"不是仓库级地址（需要 https://github.com/owner/repo）：{value!r}")
    owner, repo = segments
    if owner.lower() in _NON_REPO_PREFIXES:
        raise InvalidInputError(
            f"这是企业或组织地址，不是仓库地址（只支持仓库级 runner）：{value!r}"
        )
    return validate_owner_repo(owner, repo.removesuffix(".git"))


def _is_file(path: Path) -> bool:
    """stat()-only check; permission errors count as absent (logged)."""
    try:
        return path.is_file()
    except OSError as e:
        log.warning("cannot stat %s: %s", path, e)
        return False


def is_configured(runner_dir: Path) -> bool:
    """`.runner` and `.credentials` both exist. Never opens either file."""
    return _is_file(runner_dir / RUNNER_FILE) and _is_file(runner_dir / CREDENTIALS_FILE)


def looks_like_runner_dir(path: Path) -> bool:
    """An extracted runner package: both config.cmd and run.cmd are present."""
    return _is_file(path / "config.cmd") and _is_file(path / "run.cmd")


def scan_runner_dirs(root: Path) -> list[Path]:
    """Direct subdirectories of `root` that are configured runners, sorted by name.

    Missing root (or not a directory) -> []. An unreadable root -> RunnerDeckError.
    """
    try:
        if not root.is_dir():
            return []
        children = list(root.iterdir())
    except OSError as e:
        raise RunnerDeckError(f"无法读取目录 {root}：{e.strerror or e}") from e

    found: list[Path] = []
    for child in children:
        try:
            if not child.is_dir():
                continue
        except OSError as e:
            log.warning("cannot stat %s: %s", child, e)
            continue
        if is_configured(child):
            found.append(child)
    found.sort(key=lambda p: (p.name.casefold(), p.name))
    return found
