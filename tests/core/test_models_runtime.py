"""Tests for the M3 runtime dataclasses and settings in runnerdeck.core.models."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from runnerdeck.core.models import (
    AppSettings,
    AutostartState,
    DiagLog,
    Discovery,
    StrayRunner,
)


def test_notify_jobs_defaults_to_failed_and_is_validated() -> None:
    assert AppSettings().notify_jobs == "failed"
    assert AppSettings(notify_jobs="all").notify_jobs == "all"
    with pytest.raises(ValidationError):
        AppSettings(notify_jobs="sometimes")  # type: ignore[arg-type]


def test_old_config_without_notify_jobs_loads() -> None:
    settings = AppSettings.model_validate({"poll_interval_s": 30})
    assert settings.notify_jobs == "failed"


CMD = '"C:\\RD\\RunnerDeck.exe" --autostart'


@pytest.mark.parametrize(
    ("registered", "command", "expected", "matches"),
    [
        (True, CMD, CMD, True),
        (True, CMD.lower(), CMD, True),  # Windows paths are case-insensitive
        (True, '"D:\\old\\RunnerDeck.exe" --autostart', CMD, False),  # folder moved
        (False, None, CMD, False),
        (True, "x", None, False),
    ],
)
def test_autostart_state_matches(
    registered: bool, command: str | None, expected: str | None, matches: bool
) -> None:
    state = AutostartState(
        supported=True, registered=registered, command=command, expected=expected
    )
    assert state.matches is matches


def test_unsupported_autostart_state_defaults() -> None:
    state = AutostartState(supported=False)
    assert not state.registered and not state.matches and not state.disabled_by_user


def test_discovery_empty() -> None:
    root = Path("D:/runners")
    assert Discovery(root).empty
    assert not Discovery(root, unregistered=(root / "a",)).empty
    stray = StrayRunner(root / "b", (12,), configured=True, service=False)
    assert not Discovery(root, strays=(stray,)).empty


def test_diag_log_name_and_frozen() -> None:
    log = DiagLog(
        Path("D:/runners/x/_diag/Runner_20261024-010203-utc.log"),
        "runner",
        datetime(2026, 10, 24, tzinfo=UTC),
        123,
    )
    assert log.name == "Runner_20261024-010203-utc.log"
    with pytest.raises(dataclasses.FrozenInstanceError):
        log.size = 1  # type: ignore[misc]
