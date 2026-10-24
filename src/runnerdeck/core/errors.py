"""Exception hierarchy for the core layer.

Callers (CLI now, Qt controller later) catch RunnerDeckError and show str(e) to the user,
so every message must be presentable and must never contain a secret.
"""

from __future__ import annotations

from datetime import datetime


class RunnerDeckError(Exception):
    """Base class for all expected failures."""


class InvalidInputError(RunnerDeckError):
    """User-supplied value (PAT format, runner name, label, repo) is not acceptable."""


# --- GitHub -----------------------------------------------------------------


class GitHubError(RunnerDeckError):
    """A GitHub REST call failed. `status` is None for transport errors."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        method: str | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.method = method
        self.path = path


class AuthError(GitHubError):
    """401: PAT missing, wrong, revoked or expired."""


class PermissionDeniedError(GitHubError):
    """403 that is not rate limiting: the PAT lacks a permission (e.g. Administration)."""


class NotFoundError(GitHubError):
    """404. GitHub also answers 404 for repos the PAT is not allowed to see."""


class RateLimitError(GitHubError):
    def __init__(self, message: str, *, reset_at: datetime | None = None, **kw: object) -> None:
        super().__init__(message, **kw)  # type: ignore[arg-type]
        self.reset_at = reset_at


class NetworkError(GitHubError):
    """Connection, TLS, proxy or timeout failure before an HTTP status was received."""


# --- runner package / directory ------------------------------------------------


class ReleaseError(RunnerDeckError):
    """Runner release metadata unusable (no win-x64 asset, no checksum)."""


class ChecksumMismatchError(ReleaseError):
    pass


class RunnerFileError(RunnerDeckError):
    """`.runner` exists but cannot be parsed."""


class InstallError(RunnerDeckError):
    """config.cmd (register / remove) failed, timed out, or a runner dir is unsafe to touch."""

    def __init__(self, message: str, *, output: str = "", returncode: int | None = None) -> None:
        super().__init__(message)
        self.output = output  # already redacted
        self.returncode = returncode


class ProcessError(RunnerDeckError):
    """Starting or stopping run.cmd failed, or the runner is already running elsewhere."""


class ServiceError(RunnerDeckError):
    """A service-mode runner's Windows service could not be queried, started or stopped."""


class ElevationRequiredError(ServiceError):
    """The service manager refused access: the operation needs administrator rights."""


class ElevationCancelledError(RunnerDeckError):
    """The user answered "No" in the UAC prompt (nothing was changed)."""


# --- local state --------------------------------------------------------------


class StoreError(RunnerDeckError):
    """config.json unreadable, invalid, or could not be written."""


class SecretStoreError(RunnerDeckError):
    """Windows Credential Manager (keyring) unavailable or refused the operation."""


class RunnerNotFoundError(RunnerDeckError):
    pass


class AmbiguousRunnerError(RunnerDeckError):
    pass
