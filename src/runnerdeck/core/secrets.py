"""PAT storage in the OS credential store (Windows Credential Manager via keyring).

The PAT never goes to config.json; only the login (keyring username) does.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import keyring
from keyring.errors import PasswordDeleteError

from runnerdeck.core.errors import InvalidInputError, SecretStoreError
from runnerdeck.core.redact import redact, register_secret

if TYPE_CHECKING:
    from keyring.backend import KeyringBackend

log = logging.getLogger(__name__)

KEYRING_SERVICE = "RunnerDeck"
PAT_PREFIXES = ("github_pat_", "ghp_", "gho_", "ghu_")
MIN_PAT_LENGTH = 36
_PAT_CHARS_RE = re.compile(r"[A-Za-z0-9_]+")

_backend: KeyringBackend | None = None


def set_backend(backend: KeyringBackend | None) -> None:
    """Use `backend` instead of keyring's global one (tests, diagnostics). None restores it."""
    global _backend
    _backend = backend


def _keyring() -> KeyringBackend:
    return _backend if _backend is not None else keyring.get_keyring()


def validate_pat_format(pat: str) -> str:
    """Return the stripped PAT, or raise InvalidInputError without ever echoing the input.

    spike §2: users paste the token's *name* instead of its value and get a wall of 401s.
    """
    value = pat.strip()
    if (
        len(value) < MIN_PAT_LENGTH
        or not value.startswith(PAT_PREFIXES)
        or not _PAT_CHARS_RE.fullmatch(value)
    ):
        raise InvalidInputError(
            "PAT 格式不对：应以 github_pat_、ghp_、gho_ 或 ghu_ 开头，至少 36 个字符，"
            "且只含字母、数字和下划线。请粘贴 token 的值，而不是名字"
            "（值只在 GitHub 创建 token 后显示一次）"
        )
    register_secret(value)
    return value


def save_pat(login: str, pat: str) -> None:
    register_secret(pat)
    login = _check_login(login)
    if not pat:
        raise InvalidInputError("PAT 不能为空")
    try:
        _keyring().set_password(KEYRING_SERVICE, login, pat)
    except Exception as e:
        raise _store_error("保存", e) from e
    log.info("PAT for %s saved to the credential store", login)


def load_pat(login: str) -> str | None:
    """The stored PAT for `login`, or None. Registered for redaction before it is returned."""
    login = _check_login(login)
    try:
        value = _keyring().get_password(KEYRING_SERVICE, login)
    except Exception as e:
        raise _store_error("读取", e) from e
    if not value:
        return None
    register_secret(value)
    return value


def delete_pat(login: str) -> None:
    """Remove the stored PAT; silently does nothing if there is none."""
    login = _check_login(login)
    try:
        _keyring().delete_password(KEYRING_SERVICE, login)
    except PasswordDeleteError:
        log.debug("no PAT stored for %s", login)
        return
    except Exception as e:
        raise _store_error("删除", e) from e
    log.info("PAT for %s deleted from the credential store", login)


def _check_login(login: str) -> str:
    login = login.strip()
    if not login:
        raise InvalidInputError("GitHub 登录名不能为空")
    return login


def _store_error(action: str, e: Exception) -> SecretStoreError:
    detail = redact(str(e)).strip()
    reason = f"{type(e).__name__}: {detail}" if detail else type(e).__name__
    return SecretStoreError(f"无法{action}凭据管理器中的 PAT：{reason}")
