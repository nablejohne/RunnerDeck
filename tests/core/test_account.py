"""Tests for runnerdeck.core.account (shared by the CLI and the GUI controller)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from runnerdeck.core import account, secrets
from runnerdeck.core.account import NotLoggedInError, format_expiry, resolve_pat
from runnerdeck.core.errors import InvalidInputError
from runnerdeck.core.redact import redact

FAKE_PAT = "github_pat_" + "X" * 40
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def test_env_pat_wins_over_keyring(memory_keyring: object) -> None:
    secrets.save_pat("octo", "ghp_" + "K" * 36)
    pat, source = resolve_pat("octo", {account.ENV_PAT: f" {FAKE_PAT} "}, login_hint="h")
    assert pat == FAKE_PAT
    assert source == account.SOURCE_ENV
    assert FAKE_PAT not in redact(f"value {FAKE_PAT}")


def test_env_pat_must_look_like_a_token() -> None:
    with pytest.raises(InvalidInputError) as info:
        resolve_pat("octo", {account.ENV_PAT: "token-name-not-value"}, login_hint="h")
    assert account.ENV_PAT in str(info.value)
    assert "token-name-not-value" not in str(info.value)


def test_keyring_pat(memory_keyring: object) -> None:
    secrets.save_pat("octo", FAKE_PAT)
    assert resolve_pat("octo", {}, login_hint="h") == (FAKE_PAT, account.SOURCE_KEYRING)


def test_not_logged_in_messages(memory_keyring: object) -> None:
    with pytest.raises(NotLoggedInError, match="还没有登录：请先登录"):
        resolve_pat(None, {account.ENV_PAT: "  "}, login_hint="请先登录")
    with pytest.raises(NotLoggedInError, match="凭据管理器里没有 octo 的 PAT，请先登录"):
        resolve_pat("octo", {}, login_hint="请先登录")


def test_format_expiry() -> None:
    assert format_expiry(None, NOW)[1] is False
    assert format_expiry(NOW - timedelta(minutes=1), NOW) == (
        "2026-10-23 11:59+00:00（已过期，请在 GitHub 重新生成）",
        True,
    )
    text, attention = format_expiry(NOW + timedelta(hours=5), NOW)
    assert attention and "不到 1 天" in text
    text, attention = format_expiry(NOW + timedelta(days=30), NOW)
    assert not attention and "还剩 30 天" in text
