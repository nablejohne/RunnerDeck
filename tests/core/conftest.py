"""Shared fixtures for core tests (owned by assignment A; see docs/m1-design.md §12)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class MemoryKeyring(KeyringBackend):
    """In-memory keyring: never touches the real credential store."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.passwords: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.passwords[(service, username)]
        except KeyError:
            raise PasswordDeleteError(f"{username}@{service} not found") from None


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every RunnerDeck app dir (config, cache, logs) to tmp_path."""
    monkeypatch.setenv("RUNNERDECK_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def memory_keyring() -> Iterator[MemoryKeyring]:
    """Install a fresh MemoryKeyring as keyring's backend; restore the previous one after."""
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)
