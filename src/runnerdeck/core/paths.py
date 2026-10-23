"""Filesystem locations. Functions (not constants) so tests can redirect via RUNNERDECK_HOME."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "RunnerDeck"
ENV_HOME = "RUNNERDECK_HOME"  # dev/test override: everything lives under this one dir
CONFIG_FILE_NAME = "config.json"
LOG_FILE_NAME = "runnerdeck.log"


def _override() -> Path | None:
    value = os.environ.get(ENV_HOME, "").strip()
    return Path(value) if value else None


def _base(env_var: str) -> Path:
    override = _override()
    if override is not None:
        return override
    value = os.environ.get(env_var, "").strip()
    if value:
        return Path(value) / APP_NAME
    return Path.home() / ".runnerdeck"  # non-Windows dev machines


def config_dir() -> Path:
    """%APPDATA%\\RunnerDeck (roaming)."""
    return _base("APPDATA")


def config_file() -> Path:
    return config_dir() / CONFIG_FILE_NAME


def local_dir() -> Path:
    """%LOCALAPPDATA%\\RunnerDeck (machine-local)."""
    return _base("LOCALAPPDATA")


def cache_dir() -> Path:
    return local_dir() / "cache"


def log_dir() -> Path:
    return local_dir() / "logs"


def default_runners_root() -> Path:
    """Short root to stay clear of the 260-char path limit: D:\\runners, else C:\\runners.

    Under RUNNERDECK_HOME (dev/test) it is `<home>/runners`, so nothing leaks outside.
    """
    override = _override()
    if override is not None:
        return override / "runners"
    if sys.platform == "win32":
        return Path("D:\\runners") if Path("D:\\").exists() else Path("C:\\runners")
    return Path.home() / "runners"


def runner_dir_for(root: Path, repo: str) -> Path:
    """`{root}\\{repo}`, or `{repo}-2`, `{repo}-3`, ... if taken by a non-empty directory."""
    candidate = root / repo
    n = 1
    while candidate.exists() and (not candidate.is_dir() or any(candidate.iterdir())):
        n += 1
        candidate = root / f"{repo}-{n}"
    return candidate
