"""Merge local process state with the GitHub runner list into one dashboard status.

Pure functions, no IO: used by the CLI `status` command and the GUI controller (live re-merge,
dashboard counters, tray colour).
Rules follow docs/m1-design.md §9 (PLAN status table + spike §5 corrections), revised in
docs/m3-design.md §2:
- A hard kill leaves the GitHub session alive for ~3-3.5 min, so "online but not ours" right
  after a forced stop is SESSION_LINGERING, not EXTERNAL.
- GitHub's `status` turns offline long before that session expires, so offline never means
  "safe to restart".
- After a graceful stop GitHub still reports online for 11-70 s (spike §10), so a fresh graceful
  stop with GitHub online is STOPPED, not EXTERNAL.
- A remote snapshot fetched before the current local state began says nothing about that state,
  so a running process then shows its local state as-is.

All datetimes must be timezone-aware (UTC recommended); mixing naive and aware raises TypeError.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from runnerdeck.core.models import (
    DisplayState,
    LocalState,
    RemoteRunner,
    RemoteStatus,
    RunnerRecord,
)

STALL_AFTER_S = 60  # running locally but GitHub still offline beyond this -> STALLED
SESSION_LINGER_S = 240  # after a forced stop GitHub may stay online this long (spike §5)
OFFLINE_LAG_S = 90  # after a graceful stop GitHub may still say online this long (spike §10)

# The complete, final set of dashboard labels. BUSY gets "：<job>" appended when the job is known.
LABELS: Mapping[DisplayState, str] = MappingProxyType(
    {
        DisplayState.STOPPED: "已停止",
        DisplayState.STARTING: "启动中",
        DisplayState.RECONNECTING: "等待旧会话过期",
        DisplayState.IDLE: "空闲",
        DisplayState.BUSY: "运行中",
        DisplayState.STOPPING: "停止中",
        DisplayState.CONNECTING: "连接中",
        DisplayState.STALLED: "连接异常",
        DisplayState.SESSION_LINGERING: "GitHub 会话未过期",
        DisplayState.EXTERNAL: "被其他进程运行",
        DisplayState.MISSING: "已在 GitHub 端删除",
        DisplayState.ERROR: "异常",
        DisplayState.UNKNOWN: "未知",
    }
)

DETAIL_REMOTE_UNKNOWN = "GitHub 端状态未知"
DETAIL_MISSING = "GitHub 上已没有这个 runner，请重新注册或移除本地记录"
DETAIL_RECONNECTING = "上次强制停止后 GitHub 端旧会话尚未过期（约 3 分钟），runner 会自动重试"
DETAIL_CONNECTING = "本地已启动，等待 GitHub 显示在线"
DETAIL_STALLED = f"本地运行中，但 GitHub 超过 {STALL_AFTER_S} 秒仍显示离线，请检查网络或代理"
DETAIL_EXTERNAL_LOCAL = (
    "目录下有不归本进程管理的 runner 进程（另一个终端里的 start、服务或残留进程）"
)
DETAIL_EXTERNAL_REMOTE = (
    "GitHub 显示在线，但本应用没有运行它（可能是服务或其他电脑上的同名 runner）"
)
DETAIL_OFFLINE_LAG = "已优雅停止，GitHub 端还显示在线（通常 1 分钟内更新）"

# Local states that can be shown as-is while the process runs and GitHub gives no signal.
_RUNNING_PASSTHROUGH: Mapping[LocalState, DisplayState] = MappingProxyType(
    {
        LocalState.STARTING: DisplayState.STARTING,
        LocalState.IDLE: DisplayState.IDLE,
        LocalState.BUSY: DisplayState.BUSY,
    }
)


@dataclass(frozen=True)
class LocalSnapshot:
    """Local view of one runner at one instant.

    running: our RunnerProcess is alive. external: a runner process lives in the directory
    but is not managed by this app. state_since: when `state` was entered.
    last_stop_forced / stopped_at: describe the most recent stop (forced = no Ctrl+Break).
    """

    state: LocalState
    running: bool
    external: bool
    state_since: datetime
    last_stop_forced: bool = False
    stopped_at: datetime | None = None
    current_job: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class RemoteSnapshot:
    """GitHub's view. UNKNOWN when the list request failed, MISSING when not in the list."""

    status: RemoteStatus
    busy: bool = False
    fetched_at: datetime | None = None

    @classmethod
    def from_remote(
        cls, remote: RemoteRunner | None, *, fetched_at: datetime | None = None
    ) -> RemoteSnapshot:
        """Snapshot from a successful list fetch; `None` (no match) means MISSING."""
        if remote is None:
            return cls(RemoteStatus.MISSING, fetched_at=fetched_at)
        return cls(RemoteStatus(remote.status), busy=remote.busy, fetched_at=fetched_at)


@dataclass(frozen=True)
class DisplayStatus:
    state: DisplayState
    label: str  # Chinese, e.g. "空闲" / "运行中：build"
    detail: str | None = None


def display_label(state: DisplayState, job: str | None = None) -> str:
    """Label for `state`; BUSY becomes "运行中：<job>" when the job name is known."""
    label = LABELS[state]
    if state is DisplayState.BUSY and job:
        return f"{label}：{job}"
    return label


def _status(
    state: DisplayState, detail: str | None = None, *, job: str | None = None
) -> DisplayStatus:
    return DisplayStatus(state=state, label=display_label(state, job), detail=detail)


def _elapsed_s(since: datetime, now: datetime) -> float:
    return (now - since).total_seconds()


def merge_status(
    local: LocalSnapshot, remote: RemoteSnapshot | None, *, now: datetime
) -> DisplayStatus:
    """Combine local and remote state; rules are checked strictly in the order of §9."""
    remote_status = remote.status if remote is not None else RemoteStatus.UNKNOWN
    remote_busy = remote.busy if remote is not None else False

    # 1. Deleted on GitHub wins over everything local.
    if remote_status is RemoteStatus.MISSING:
        return _status(DisplayState.MISSING, DETAIL_MISSING)

    # 2. Local transitions / failures that GitHub cannot contradict.
    if local.state is LocalState.STOPPING:
        return _status(DisplayState.STOPPING)
    if local.state is LocalState.ERROR and not local.external:
        # Our run failed. Once someone else runs the directory, rule 4 shows EXTERNAL instead,
        # so take-over / kill are offered rather than a start that cannot succeed.
        return _status(DisplayState.ERROR, local.last_error)

    # 3. Our process is running.
    if local.running:
        if local.state is LocalState.RECONNECTING:
            return _status(DisplayState.RECONNECTING, DETAIL_RECONNECTING)
        if (
            remote is not None
            and remote_status in (RemoteStatus.ONLINE, RemoteStatus.OFFLINE)
            and remote.fetched_at is not None
            and remote.fetched_at < local.state_since
        ):
            # Fetched before this local state began: it neither confirms nor contradicts it.
            shown = _RUNNING_PASSTHROUGH.get(local.state, DisplayState.STARTING)
            return _status(shown, job=local.current_job)
        if remote_status is RemoteStatus.ONLINE:
            # GitHub's busy flag lags the stdout; trust local BUSY until the next poll agrees.
            if remote_busy or local.state is LocalState.BUSY:
                return _status(DisplayState.BUSY, job=local.current_job)
            return _status(DisplayState.IDLE)
        if remote_status is RemoteStatus.OFFLINE:
            if _elapsed_s(local.state_since, now) < STALL_AFTER_S:
                return _status(DisplayState.CONNECTING, DETAIL_CONNECTING)
            return _status(DisplayState.STALLED, DETAIL_STALLED)
        # Remote UNKNOWN / not fetched: show the local state. A running process whose state
        # still reads STOPPED has not reported anything yet, which is what STARTING means.
        shown = _RUNNING_PASSTHROUGH.get(local.state, DisplayState.STARTING)
        return _status(shown, DETAIL_REMOTE_UNKNOWN, job=local.current_job)

    # 4. Our process is not running.
    if local.external:
        return _status(DisplayState.EXTERNAL, DETAIL_EXTERNAL_LOCAL)
    if remote_status is RemoteStatus.ONLINE and local.stopped_at is not None:
        # Judge by when GitHub said "online", not by now: a snapshot taken shortly after the
        # stop stays the latest one until the next poll (up to poll_interval_s, i.e. minutes).
        seen = remote.fetched_at if remote is not None and remote.fetched_at else now
        seen_after_stop_s = _elapsed_s(local.stopped_at, seen)
        if not local.last_stop_forced and seen_after_stop_s < OFFLINE_LAG_S:
            # The session is already released; GitHub's flag just lags behind.
            return _status(DisplayState.STOPPED, DETAIL_OFFLINE_LAG)
        if local.last_stop_forced and seen_after_stop_s < SESSION_LINGER_S:
            # ceil so 239.5 s elapsed never reads "0 秒"; clamp against clock skew and against
            # an old snapshot outliving the session.
            left = math.ceil(SESSION_LINGER_S - _elapsed_s(local.stopped_at, now))
            remaining = max(1, min(SESSION_LINGER_S, left))
            return _status(
                DisplayState.SESSION_LINGERING,
                f"强制停止后 GitHub 端旧会话尚未过期（最多约 {remaining} 秒）；"
                "现在重新启动会先显示“等待旧会话过期”",
            )
    if remote_status is RemoteStatus.ONLINE:
        return _status(DisplayState.EXTERNAL, DETAIL_EXTERNAL_REMOTE)
    if remote_status is RemoteStatus.OFFLINE:
        return _status(DisplayState.STOPPED)
    return _status(DisplayState.STOPPED, DETAIL_REMOTE_UNKNOWN)


# --- grouping (dashboard counters, tray colour) ------------------------------------------


class Bucket(StrEnum):
    """Coarse group of a DisplayState, shared by the dashboard counters and the tray icon."""

    RUNNING = "running"
    IDLE = "idle"
    TROUBLE = "trouble"
    STOPPED = "stopped"


_BUCKETS: Mapping[DisplayState, Bucket] = MappingProxyType(
    {
        DisplayState.IDLE: Bucket.IDLE,
        DisplayState.BUSY: Bucket.RUNNING,
        DisplayState.STARTING: Bucket.RUNNING,
        DisplayState.CONNECTING: Bucket.RUNNING,
        DisplayState.RECONNECTING: Bucket.RUNNING,
        DisplayState.STOPPING: Bucket.RUNNING,
        DisplayState.STALLED: Bucket.TROUBLE,
        DisplayState.ERROR: Bucket.TROUBLE,
        DisplayState.MISSING: Bucket.TROUBLE,
        DisplayState.EXTERNAL: Bucket.TROUBLE,
        DisplayState.STOPPED: Bucket.STOPPED,
        DisplayState.UNKNOWN: Bucket.STOPPED,
        DisplayState.SESSION_LINGERING: Bucket.STOPPED,
    }
)


def bucket(state: DisplayState) -> Bucket:
    return _BUCKETS[state]


class TrayLevel(StrEnum):
    """Tray icon colour: grey / green / blue / red."""

    STOPPED = "stopped"
    ONLINE = "online"
    BUSY = "busy"
    ERROR = "error"


def tray_level(states: Iterable[DisplayState]) -> TrayLevel:
    """Any trouble -> ERROR, else any BUSY -> BUSY, else anything running -> ONLINE."""
    shown = list(states)
    groups = {bucket(s) for s in shown}
    if Bucket.TROUBLE in groups:
        return TrayLevel.ERROR
    if DisplayState.BUSY in shown:
        return TrayLevel.BUSY
    if groups & {Bucket.RUNNING, Bucket.IDLE}:
        return TrayLevel.ONLINE
    return TrayLevel.STOPPED


# Runner TaskResult values worth a "failed" notification; Canceled / Succeeded* / Skipped are not.
JOB_FAILURE_RESULTS = frozenset({"failed", "abandoned"})


def is_job_failure(result: str) -> bool:
    return result.strip().casefold() in JOB_FAILURE_RESULTS


def match_remote(record: RunnerRecord, remotes: Sequence[RemoteRunner]) -> RemoteRunner | None:
    """Find `record` in one repo's runner list: by github_runner_id, then by name (no case)."""
    if record.github_runner_id is not None:
        for remote in remotes:
            if remote.id == record.github_runner_id:
                return remote
    name = record.name.casefold()
    for remote in remotes:
        if remote.name.casefold() == name:
            return remote
    return None
