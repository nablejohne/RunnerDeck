"""Data models shared by every core module.

Persisted: AppConfig (settings + RunnerRecord index), written to config.json.
API views: GitHubUser, Repo, RemoteRunner, RunnerToken, ReleaseInfo (unknown fields ignored).
Runner dir: RunnerFileInfo (parsed `.runner`).
Runtime enums: LocalState, RemoteStatus, DisplayState.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
)

from runnerdeck.core import paths
from runnerdeck.core.errors import InvalidInputError

# --- validation ------------------------------------------------------------------

# Runner names and labels end up on a `cmd.exe /c config.cmd ...` command line and in
# directory names, so stay well inside characters cmd.exe treats literally.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
# Added by the runner itself; passing them again is harmless but noisy.
AUTO_LABELS = frozenset({"self-hosted", "windows", "x64"})


def validate_runner_name(name: str) -> str:
    name = name.strip()
    if not _NAME_RE.fullmatch(name):
        raise InvalidInputError(
            f"runner 名称不合法：{name!r}（只能用字母、数字、. _ -，以字母或数字开头，最长 64）"
        )
    return name


def normalize_labels(labels: list[str] | tuple[str, ...] | str) -> list[str]:
    """Split/trim/dedupe labels (case-insensitive), drop the runner's automatic ones."""
    if isinstance(labels, str):
        labels = labels.split(",")
    seen: set[str] = set()
    out: list[str] = []
    for raw in labels:
        label = raw.strip()
        if not label:
            continue
        if not _LABEL_RE.fullmatch(label):
            raise InvalidInputError(f"标签不合法：{label!r}（只能用字母、数字、. _ -）")
        key = label.lower()
        if key in AUTO_LABELS or key in seen:
            continue
        seen.add(key)
        out.append(label)
    return out


def validate_owner_repo(owner: str, repo: str) -> tuple[str, str]:
    if not _OWNER_RE.fullmatch(owner):
        raise InvalidInputError(f"仓库所有者不合法：{owner!r}")
    if not _REPO_RE.fullmatch(repo) or repo in {".", ".."}:
        raise InvalidInputError(f"仓库名不合法：{repo!r}")
    return owner, repo


def parse_full_name(value: str) -> tuple[str, str]:
    """'owner/repo' -> (owner, repo), validated."""
    owner, sep, repo = value.strip().partition("/")
    if not sep:
        raise InvalidInputError(f"需要 owner/repo 形式：{value!r}")
    return validate_owner_repo(owner, repo)


def default_runner_name(hostname: str, repo: str) -> str:
    """`{hostname}-{repo}` squeezed into the allowed charset and length."""
    raw = f"{hostname}-{repo}"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._") or "runner"
    return cleaned[:64].rstrip("-._")


# --- persisted ----------------------------------------------------------------------


class RunnerMode(StrEnum):
    PROCESS = "process"
    SERVICE = "service"  # M4


class Scope(StrEnum):
    REPO = "repo"  # org / enterprise reserved


class RunnerRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    scope: Scope = Scope.REPO
    owner: str
    repo: str
    name: str
    labels: list[str] = Field(default_factory=list)
    path: str
    mode: RunnerMode = RunnerMode.PROCESS
    github_runner_id: int | None = None
    auto_start: bool = False
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    runners_root: str = Field(default_factory=lambda: str(paths.default_runners_root()))
    proxy: str | None = None
    poll_interval_s: int = Field(default=30, ge=5, le=3600)
    default_labels: list[str] = Field(default_factory=list)
    launch_on_login: bool = False
    start_minimized: bool = False
    close_to_tray: bool = True
    theme: Literal["auto", "light", "dark"] = "auto"


CONFIG_SCHEMA_VERSION = 1


class AppConfig(BaseModel):
    """Root of config.json. Only an index: GitHub and each `.runner` are the sources of truth."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = CONFIG_SCHEMA_VERSION
    account: str | None = None  # GitHub login; also the keyring username for the PAT
    settings: AppSettings = Field(default_factory=AppSettings)
    runners: list[RunnerRecord] = Field(default_factory=list)

    def get(self, record_id: str) -> RunnerRecord | None:
        return next((r for r in self.runners if r.id == record_id), None)

    def upsert(self, record: RunnerRecord) -> None:
        for i, existing in enumerate(self.runners):
            if existing.id == record.id:
                self.runners[i] = record
                return
        self.runners.append(record)

    def remove(self, record_id: str) -> RunnerRecord | None:
        record = self.get(record_id)
        if record is not None:
            self.runners = [r for r in self.runners if r.id != record_id]
        return record


# --- GitHub API views -------------------------------------------------------------


class GitHubUser(BaseModel):
    model_config = ConfigDict(extra="ignore")

    login: str
    id: int
    token_kind: Literal["classic", "fine-grained", "unknown"] = "unknown"
    scopes: list[str] | None = None  # classic tokens only (X-OAuth-Scopes)
    token_expires_at: AwareDatetime | None = None


def _login_of(value: Any) -> Any:
    return value.get("login") if isinstance(value, dict) else value


class Repo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    full_name: str
    owner: str
    private: bool
    html_url: str
    fork: bool = False
    archived: bool = False

    _owner_login = field_validator("owner", mode="before")(_login_of)


class RemoteRunner(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    status: Literal["online", "offline"]
    busy: bool = False
    os: str | None = None
    labels: list[str] = Field(default_factory=list)

    @field_validator("labels", mode="before")
    @classmethod
    def _label_names(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [v.get("name") if isinstance(v, dict) else v for v in value]
        return value


class RunnerToken(BaseModel):
    """Registration or remove token. ~1 h lifetime: fetch right before use, never cache."""

    token: SecretStr
    expires_at: AwareDatetime  # GitHub returns local offsets such as +08:00


class ReleaseInfo(BaseModel):
    version: str  # "2.338.0"
    asset_name: str  # "actions-runner-win-x64-2.338.0.zip"
    download_url: str
    size: int
    sha256: str  # lowercase hex


# --- runner directory ------------------------------------------------------------------


class RunnerFileInfo(BaseModel):
    """Parsed `.runner` (UTF-8 with BOM). Unknown keys kept in `raw` for diagnostics."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    agent_id: int = Field(alias="agentId")
    agent_name: str = Field(alias="agentName")
    pool_id: int | None = Field(default=None, alias="poolId")
    server_url: str | None = Field(default=None, alias="serverUrl")
    github_url: str = Field(alias="gitHubUrl")
    work_folder: str = Field(default="_work", alias="workFolder")
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)


# --- runtime state (never persisted) ---------------------------------------------------


class LocalState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"  # spawned, not yet "Listening for Jobs"
    RECONNECTING = "reconnecting"  # "A session for this runner already exists" (spike §5)
    IDLE = "idle"
    BUSY = "busy"
    STOPPING = "stopping"
    ERROR = "error"


class RemoteStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    MISSING = "missing"  # not in the repo's runner list (deleted on GitHub)
    UNKNOWN = "unknown"  # not fetched yet / fetch failed


class DisplayState(StrEnum):
    """What the dashboard shows after merging local and remote state (status.py)."""

    STOPPED = "stopped"
    STARTING = "starting"
    RECONNECTING = "reconnecting"
    IDLE = "idle"
    BUSY = "busy"
    STOPPING = "stopping"
    CONNECTING = "connecting"  # running locally, GitHub still offline (< threshold)
    STALLED = "stalled"  # running locally, GitHub offline beyond threshold
    SESSION_LINGERING = "session_lingering"  # recently hard-stopped, GitHub still online
    EXTERNAL = "external"  # not ours, but GitHub online (service / leftover process)
    MISSING = "missing"  # deleted on GitHub
    ERROR = "error"
    UNKNOWN = "unknown"
