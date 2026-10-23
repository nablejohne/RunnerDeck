"""Account helpers shared by the CLI and the GUI: PAT resolution and token descriptions.

Where the PAT comes from (M1 CLI rule, kept for the GUI): the RUNNERDECK_PAT environment
variable wins over the one stored in the credential store for `config.account`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from runnerdeck.core import secrets
from runnerdeck.core.errors import InvalidInputError, RunnerDeckError
from runnerdeck.core.models import GitHubUser
from runnerdeck.core.redact import register_secret

ENV_PAT = "RUNNERDECK_PAT"
EXPIRY_WARN_DAYS = 7  # warn when the PAT expires sooner than this
SOURCE_ENV = f"环境变量 {ENV_PAT}"
SOURCE_KEYRING = "系统凭据管理器"
TOKEN_KIND_LABELS: Mapping[str, str] = {
    "fine-grained": "fine-grained PAT",
    "classic": "classic PAT",
    "unknown": "未知",
}


# The login dialog pre-checks the format locally; ui/ may import account but not secrets.
validate_pat_format = secrets.validate_pat_format


class NotLoggedInError(RunnerDeckError):
    """No PAT: neither RUNNERDECK_PAT nor a stored PAT for config.account."""


def resolve_pat(
    account: str | None, environ: Mapping[str, str], *, login_hint: str
) -> tuple[str, str]:
    """(PAT, where it came from). RUNNERDECK_PAT wins over the stored one.

    login_hint: what to tell the user when there is no PAT at all (e.g. the CLI's
    "请先运行 runnerdeck-cli login"). Raises NotLoggedInError, or InvalidInputError when the
    environment variable does not look like a PAT.
    """
    env_value = environ.get(ENV_PAT, "").strip()
    if env_value:
        register_secret(env_value)
        try:
            value = secrets.validate_pat_format(env_value)
        except InvalidInputError as e:
            raise InvalidInputError(f"{SOURCE_ENV}：{e}") from None
        return value, SOURCE_ENV
    if not account:
        raise NotLoggedInError(f"还没有登录：{login_hint}")
    stored = secrets.load_pat(account)
    if stored is None:
        raise NotLoggedInError(f"系统凭据管理器里没有 {account} 的 PAT，{login_hint}")
    return stored, SOURCE_KEYRING


def format_expiry(expires_at: datetime | None, now: datetime) -> tuple[str, bool]:
    """(text, needs attention)."""
    if expires_at is None:
        return "GitHub 没有返回过期时间（可能设置为永不过期）", False
    stamp = expires_at.isoformat(sep=" ", timespec="minutes")
    remaining = expires_at - now
    if remaining.total_seconds() <= 0:
        return f"{stamp}（已过期，请在 GitHub 重新生成）", True
    days = remaining.days
    if days < EXPIRY_WARN_DAYS:
        left = f"{days} 天" if days else "不到 1 天"
        return f"{stamp}（即将过期：还剩 {left}，请及时在 GitHub 重新生成）", True
    return f"{stamp}（还剩 {days} 天）", False


def permission_hints(user: GitHubUser) -> list[str]:
    """What the PAT needs for RunnerDeck, phrased for its kind (PLAN “PAT 权限”)."""
    if user.token_kind == "fine-grained":
        return [
            "fine-grained PAT 需要：Repository access 里选中要添加 runner 的仓库；"
            "Repository permissions 里 Administration: Read and write（必需），"
            "Metadata: Read（自动包含）"
        ]
    if user.token_kind == "classic":
        scopes = user.scopes or []
        if "repo" in scopes:
            return [
                "classic PAT 已有 repo 权限，可以管理所有仓库的 runner；"
                "更推荐只授权选定仓库的 fine-grained PAT"
            ]
        shown = ", ".join(scopes) if scopes else "无"
        return [
            f"classic PAT 缺少 repo 权限（当前：{shown}），注册和移除 runner 会失败；"
            "请在 GitHub 上为这个 token 勾选 repo"
        ]
    return [
        "无法判断 token 类型。需要的权限：fine-grained PAT 为 Administration: Read and write"
        " + Metadata: Read；classic PAT 为 repo"
    ]
