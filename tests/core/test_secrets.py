from __future__ import annotations

import uuid
from collections.abc import Iterator

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail
from keyring.errors import KeyringError, PasswordDeleteError

from runnerdeck.core import secrets
from runnerdeck.core.errors import InvalidInputError, SecretStoreError
from runnerdeck.core.redact import redact
from runnerdeck.core.secrets import (
    KEYRING_SERVICE,
    delete_pat,
    load_pat,
    save_pat,
    set_backend,
    validate_pat_format,
)

# Obviously fake values in the right shape.
FINE_GRAINED = "github_pat_" + "FAKE0fake1" * 8 + "XY"  # 93 chars like the real thing
CLASSIC = "ghp_" + "FakeClassicTokenForTests0123456789AB"  # 40 chars


def _unique(prefix: str = "opaque-secret") -> str:
    """A registered-only secret: not token-shaped, so only register_secret can mask it."""
    return f"{prefix}-{uuid.uuid4().hex}"


class BrokenKeyring(KeyringBackend):
    """Every call fails, and the message echoes the password to prove redaction."""

    priority = 0.1  # type: ignore[assignment]

    def __init__(self, exc_type: type[Exception] = KeyringError) -> None:
        super().__init__()
        self.exc_type = exc_type

    def get_password(self, service: str, username: str) -> str | None:
        raise self.exc_type("vault locked")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise self.exc_type(f"refused to store {password}")

    def delete_password(self, service: str, username: str) -> None:
        raise self.exc_type("access denied")


@pytest.fixture
def module_backend() -> Iterator[None]:
    yield
    set_backend(None)


# --- validate_pat_format ---------------------------------------------------------------


def test_service_name() -> None:
    assert KEYRING_SERVICE == "RunnerDeck"


@pytest.mark.parametrize(
    "pat",
    [
        FINE_GRAINED,
        CLASSIC,
        "gho_" + "a" * 36,
        "ghu_" + "B" * 32,  # exactly 36
        "github_pat_" + "1" * 25,  # exactly 36
    ],
)
def test_valid_pat_formats(pat: str) -> None:
    assert validate_pat_format(pat) == pat


def test_valid_pat_is_stripped_and_registered() -> None:
    # Underscores right after the prefix dodge the token regexes, so only registration masks it.
    pat = "ghp_____" + uuid.uuid4().hex
    assert redact(f"x {pat} y") == f"x {pat} y"
    assert validate_pat_format(f"  {pat}\r\n") == pat
    assert redact(f"x {pat} y") == "x *** y"


@pytest.mark.parametrize(
    "pat",
    [
        "",
        "   ",
        "runnerdeck-sandbox",  # the token's *name*, spike §2
        "RunnerDeck PAT for sandbox runner management",
        "ghp_short",
        "ghp_" + "a" * 31,  # 35 chars
        "ghs_" + "a" * 36,  # server-to-server token, not a user PAT
        "github_pat" + "a" * 36,  # missing underscore
        "GHP_" + "a" * 36,  # prefix is case-sensitive
        "ghp_" + "a" * 18 + " " + "a" * 18,  # whitespace inside
        "ghp_" + "a" * 36 + "\nX-Evil: 1",  # header injection attempt
        "Bearer " + CLASSIC,
    ],
)
def test_invalid_pat_formats_never_echo_input(pat: str) -> None:
    with pytest.raises(InvalidInputError) as exc:
        validate_pat_format(pat)
    message = str(exc.value)
    assert "请粘贴 token 的值，而不是名字" in message
    if pat.strip():
        assert pat.strip() not in message


# --- save / load / delete ------------------------------------------------------------


def test_save_then_load(memory_keyring) -> None:
    save_pat("octocat", CLASSIC)
    assert memory_keyring.passwords == {(KEYRING_SERVICE, "octocat"): CLASSIC}
    assert load_pat("octocat") == CLASSIC


def test_logins_are_independent_and_trimmed(memory_keyring) -> None:
    save_pat(" alice ", CLASSIC)
    save_pat("bob", FINE_GRAINED)
    assert load_pat("alice") == CLASSIC
    assert load_pat("bob") == FINE_GRAINED
    delete_pat("alice")
    assert load_pat("alice") is None
    assert load_pat("bob") == FINE_GRAINED


def test_save_overwrites(memory_keyring) -> None:
    save_pat("octocat", CLASSIC)
    save_pat("octocat", FINE_GRAINED)
    assert load_pat("octocat") == FINE_GRAINED


def test_load_missing_returns_none(memory_keyring) -> None:
    assert load_pat("nobody") is None


def test_load_empty_value_returns_none(memory_keyring) -> None:
    memory_keyring.passwords[(KEYRING_SERVICE, "octocat")] = ""
    assert load_pat("octocat") is None


def test_load_registers_secret(memory_keyring) -> None:
    value = _unique("loaded")
    memory_keyring.passwords[(KEYRING_SERVICE, "octocat")] = value
    assert redact(value) == value
    assert load_pat("octocat") == value
    assert redact(f"Authorization {value}!") == "Authorization ***!"


def test_save_registers_secret(memory_keyring) -> None:
    value = _unique("saved")
    assert redact(value) == value
    save_pat("octocat", value)
    assert redact(f"pat={value}") == "pat=***"
    assert redact(f"x {value} y") == "x *** y"


def test_delete_existing(memory_keyring) -> None:
    save_pat("octocat", CLASSIC)
    delete_pat("octocat")
    assert memory_keyring.passwords == {}


def test_delete_missing_is_silent(memory_keyring) -> None:
    delete_pat("octocat")
    delete_pat("octocat")


@pytest.mark.parametrize("login", ["", "  "])
def test_empty_login_rejected(memory_keyring, login: str) -> None:
    with pytest.raises(InvalidInputError):
        save_pat(login, CLASSIC)
    with pytest.raises(InvalidInputError):
        load_pat(login)
    with pytest.raises(InvalidInputError):
        delete_pat(login)
    assert memory_keyring.passwords == {}


def test_empty_pat_rejected(memory_keyring) -> None:
    with pytest.raises(InvalidInputError):
        save_pat("octocat", "")
    assert memory_keyring.passwords == {}


# --- backend failures -------------------------------------------------------------------


@pytest.mark.parametrize("exc_type", [KeyringError, RuntimeError, OSError])
def test_backend_errors_become_secret_store_error(module_backend, exc_type) -> None:
    set_backend(BrokenKeyring(exc_type))
    with pytest.raises(SecretStoreError, match="读取") as exc:
        load_pat("octocat")
    assert isinstance(exc.value.__cause__, exc_type)
    assert exc_type.__name__ in str(exc.value)
    with pytest.raises(SecretStoreError, match="删除"):
        delete_pat("octocat")
    with pytest.raises(SecretStoreError, match="保存"):
        save_pat("octocat", CLASSIC)


def test_backend_error_message_is_redacted(module_backend) -> None:
    set_backend(BrokenKeyring())
    value = _unique("echoed")
    with pytest.raises(SecretStoreError) as exc:
        save_pat("octocat", value)
    assert value not in str(exc.value)
    assert "refused to store ***" in str(exc.value)


def test_backend_error_without_message_uses_type_name(module_backend) -> None:
    class Silent(BrokenKeyring):
        def get_password(self, service: str, username: str) -> str | None:
            raise KeyringError()

    set_backend(Silent())
    with pytest.raises(SecretStoreError, match=r"KeyringError$"):
        load_pat("octocat")


def test_no_keyring_available(module_backend) -> None:
    set_backend(fail.Keyring())
    with pytest.raises(SecretStoreError, match="NoKeyringError"):
        load_pat("octocat")


def test_delete_not_found_from_backend_is_silent(module_backend) -> None:
    set_backend(BrokenKeyring(PasswordDeleteError))
    delete_pat("octocat")  # PasswordDeleteError == "not found" in every keyring backend


# --- backend selection -------------------------------------------------------------------


def test_set_backend_overrides_global_keyring(memory_keyring, module_backend) -> None:
    private = type(memory_keyring)()
    set_backend(private)
    save_pat("octocat", CLASSIC)
    assert private.passwords == {(KEYRING_SERVICE, "octocat"): CLASSIC}
    assert memory_keyring.passwords == {}

    set_backend(None)
    assert load_pat("octocat") is None  # back to the global (memory_keyring) backend
    save_pat("octocat", FINE_GRAINED)
    assert memory_keyring.passwords == {(KEYRING_SERVICE, "octocat"): FINE_GRAINED}


def test_memory_keyring_fixture_is_installed_globally(memory_keyring) -> None:
    assert keyring.get_keyring() is memory_keyring
    assert secrets._keyring() is memory_keyring


def test_no_logon_session_error_suggests_env_var(memory_keyring) -> None:
    # Real 2026-10-23: keyring over SSH -> pywintypes.error (1312, 'CredRead', ...)
    class NoSession(Exception):
        winerror = 1312

    def boom(*args, **kwargs):
        raise NoSession(1312, "CredRead", "A specified logon session does not exist.")

    memory_keyring.get_password = boom  # type: ignore[method-assign]
    with pytest.raises(SecretStoreError, match="RUNNERDECK_PAT"):
        secrets.load_pat("someone")
