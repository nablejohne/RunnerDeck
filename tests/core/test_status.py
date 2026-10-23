"""Tests for runnerdeck.core.status: the §9 merge rules, their order and their boundaries."""

from __future__ import annotations

import dataclasses
import itertools
from datetime import UTC, datetime, timedelta

import pytest

from runnerdeck.core import status as st
from runnerdeck.core.models import (
    DisplayState,
    LocalState,
    RemoteRunner,
    RemoteStatus,
    RunnerRecord,
)
from runnerdeck.core.status import (
    LABELS,
    SESSION_LINGER_S,
    STALL_AFTER_S,
    DisplayStatus,
    LocalSnapshot,
    RemoteSnapshot,
    display_label,
    match_remote,
    merge_status,
)

NOW = datetime(2026, 10, 22, 8, 10, 0, tzinfo=UTC)
D = DisplayState
L = LocalState


def local(
    state: LocalState,
    *,
    running: bool,
    external: bool = False,
    since_s: float = 0,
    forced: bool = False,
    stopped_ago_s: float | None = None,
    job: str | None = None,
    error: str | None = None,
) -> LocalSnapshot:
    return LocalSnapshot(
        state=state,
        running=running,
        external=external,
        state_since=NOW - timedelta(seconds=since_s),
        last_stop_forced=forced,
        stopped_at=None if stopped_ago_s is None else NOW - timedelta(seconds=stopped_ago_s),
        current_job=job,
        last_error=error,
    )


def remote(status: RemoteStatus, busy: bool = False) -> RemoteSnapshot:
    return RemoteSnapshot(status=status, busy=busy, fetched_at=NOW)


ONLINE = remote(RemoteStatus.ONLINE)
ONLINE_BUSY = remote(RemoteStatus.ONLINE, busy=True)
OFFLINE = remote(RemoteStatus.OFFLINE)
UNKNOWN = remote(RemoteStatus.UNKNOWN)
MISSING = remote(RemoteStatus.MISSING)
# Every remote input except MISSING; None = never fetched.
NON_MISSING = [ONLINE, ONLINE_BUSY, OFFLINE, UNKNOWN, None]
NON_MISSING_IDS = ["online", "online-busy", "offline", "unknown", "none"]
RUNNING_STATES = [L.STARTING, L.IDLE, L.BUSY]


def merge(loc: LocalSnapshot, rem: RemoteSnapshot | None) -> DisplayStatus:
    return merge_status(loc, rem, now=NOW)


# --- labels -----------------------------------------------------------------------------


def test_labels_cover_every_display_state_with_the_documented_set() -> None:
    assert set(LABELS) == set(DisplayState)
    assert dict(LABELS) == {
        D.STOPPED: "已停止",
        D.STARTING: "启动中",
        D.RECONNECTING: "等待旧会话过期",
        D.IDLE: "空闲",
        D.BUSY: "运行中",
        D.STOPPING: "停止中",
        D.CONNECTING: "连接中",
        D.STALLED: "连接异常",
        D.SESSION_LINGERING: "GitHub 会话未过期",
        D.EXTERNAL: "被其他进程运行",
        D.MISSING: "已在 GitHub 端删除",
        D.ERROR: "异常",
        D.UNKNOWN: "未知",
    }
    assert len(set(LABELS.values())) == len(LABELS), "labels must be distinguishable"


def test_labels_mapping_is_read_only() -> None:
    with pytest.raises(TypeError):
        LABELS[D.IDLE] = "x"  # type: ignore[index]


@pytest.mark.parametrize(
    ("state", "job", "expected"),
    [
        (D.BUSY, "build", "运行中：build"),
        (D.BUSY, None, "运行中"),
        (D.BUSY, "", "运行中"),
        (D.IDLE, "build", "空闲"),  # job only decorates BUSY
        (D.STOPPED, None, "已停止"),
    ],
)
def test_display_label(state: DisplayState, job: str | None, expected: str) -> None:
    assert display_label(state, job) == expected


# --- rule 1: MISSING overrides everything ------------------------------------------------


@pytest.mark.parametrize(
    ("state", "running", "external", "forced"),
    list(itertools.product(LocalState, [True, False], [True, False], [True, False])),
)
def test_missing_overrides_every_local_state(
    state: LocalState, running: bool, external: bool, forced: bool
) -> None:
    loc = local(
        state,
        running=running,
        external=external,
        since_s=3600,
        forced=forced,
        stopped_ago_s=5,
        job="build",
        error="boom",
    )
    result = merge(loc, MISSING)
    assert result.state is D.MISSING
    assert result.label == "已在 GitHub 端删除"
    assert result.detail == st.DETAIL_MISSING


# --- rule 2: STOPPING / ERROR -----------------------------------------------------------


@pytest.mark.parametrize("rem", NON_MISSING, ids=NON_MISSING_IDS)
@pytest.mark.parametrize("running", [True, False])
def test_stopping_wins_over_remote(rem: RemoteSnapshot | None, running: bool) -> None:
    result = merge(local(L.STOPPING, running=running, since_s=3600, job="build"), rem)
    assert result == DisplayStatus(D.STOPPING, "停止中", None)


@pytest.mark.parametrize("rem", NON_MISSING, ids=NON_MISSING_IDS)
@pytest.mark.parametrize("running", [True, False])
def test_error_wins_over_remote_and_carries_last_error(
    rem: RemoteSnapshot | None, running: bool
) -> None:
    loc = local(L.ERROR, running=running, external=True, error="run.cmd 退出码 1")
    assert merge(loc, rem) == DisplayStatus(D.ERROR, "异常", "run.cmd 退出码 1")


def test_error_without_message_has_no_detail() -> None:
    assert merge(local(L.ERROR, running=False), OFFLINE) == DisplayStatus(D.ERROR, "异常", None)


# --- rule 3: our process is running -----------------------------------------------------


@pytest.mark.parametrize("rem", NON_MISSING, ids=NON_MISSING_IDS)
@pytest.mark.parametrize("since_s", [0, STALL_AFTER_S + 1, 3600])
def test_reconnecting_wins_over_remote_and_stall_timer(
    rem: RemoteSnapshot | None, since_s: float
) -> None:
    result = merge(local(L.RECONNECTING, running=True, since_s=since_s), rem)
    assert result.state is D.RECONNECTING
    assert result.label == "等待旧会话过期"
    assert result.detail == st.DETAIL_RECONNECTING


@pytest.mark.parametrize(
    ("state", "job", "rem", "expected", "label"),
    [
        # remote online, not busy
        (L.STARTING, None, ONLINE, D.IDLE, "空闲"),
        (L.IDLE, None, ONLINE, D.IDLE, "空闲"),
        (L.BUSY, "build", ONLINE, D.BUSY, "运行中：build"),  # GitHub busy flag lags stdout
        (L.BUSY, None, ONLINE, D.BUSY, "运行中"),
        # remote online and busy: local job name is used (GitHub has none)
        (L.BUSY, "build", ONLINE_BUSY, D.BUSY, "运行中：build"),
        (L.IDLE, None, ONLINE_BUSY, D.BUSY, "运行中"),  # busy remote, idle local
        (L.STARTING, None, ONLINE_BUSY, D.BUSY, "运行中"),
    ],
)
def test_running_with_remote_online(
    state: LocalState, job: str | None, rem: RemoteSnapshot, expected: DisplayState, label: str
) -> None:
    result = merge(local(state, running=True, since_s=3600, job=job), rem)
    assert result == DisplayStatus(expected, label, None)


@pytest.mark.parametrize("state", RUNNING_STATES)
@pytest.mark.parametrize(
    ("since_s", "expected"),
    [
        (0, D.CONNECTING),
        (STALL_AFTER_S - 1, D.CONNECTING),  # 59 s
        (STALL_AFTER_S - 0.001, D.CONNECTING),
        (STALL_AFTER_S, D.STALLED),  # "不到 60 秒" is strict
        (STALL_AFTER_S + 1, D.STALLED),  # 61 s
        (3600, D.STALLED),
    ],
)
def test_running_with_remote_offline_stall_boundary(
    state: LocalState, since_s: float, expected: DisplayState
) -> None:
    result = merge(local(state, running=True, since_s=since_s, job="build"), OFFLINE)
    assert result.state is expected
    if expected is D.CONNECTING:
        assert result == DisplayStatus(D.CONNECTING, "连接中", st.DETAIL_CONNECTING)
    else:
        assert result == DisplayStatus(D.STALLED, "连接异常", st.DETAIL_STALLED)
        assert str(STALL_AFTER_S) in result.detail  # type: ignore[operator]


def test_offline_busy_flag_is_ignored() -> None:
    # A stale busy=True on an offline runner must not hide the stall.
    rem = remote(RemoteStatus.OFFLINE, busy=True)
    assert merge(local(L.BUSY, running=True, since_s=120, job="x"), rem).state is D.STALLED


@pytest.mark.parametrize("rem", [UNKNOWN, None], ids=["unknown", "none"])
@pytest.mark.parametrize(
    ("state", "job", "expected", "label"),
    [
        (L.STARTING, None, D.STARTING, "启动中"),
        (L.IDLE, None, D.IDLE, "空闲"),
        (L.BUSY, "build", D.BUSY, "运行中：build"),
        (L.BUSY, None, D.BUSY, "运行中"),
    ],
)
def test_running_with_remote_unknown_follows_local_state(
    rem: RemoteSnapshot | None,
    state: LocalState,
    job: str | None,
    expected: DisplayState,
    label: str,
) -> None:
    # Stall timer does not apply without a remote signal.
    result = merge(local(state, running=True, since_s=3600, job=job), rem)
    assert result == DisplayStatus(expected, label, st.DETAIL_REMOTE_UNKNOWN)


@pytest.mark.parametrize(
    ("rem", "since_s", "expected"),
    [
        (None, 0, D.STARTING),
        (UNKNOWN, 0, D.STARTING),
        (ONLINE, 0, D.IDLE),
        (ONLINE_BUSY, 0, D.BUSY),
        (OFFLINE, 0, D.CONNECTING),
        (OFFLINE, STALL_AFTER_S + 1, D.STALLED),
    ],
)
def test_running_but_state_not_reported_yet(
    rem: RemoteSnapshot | None, since_s: float, expected: DisplayState
) -> None:
    # Process alive while its state still reads STOPPED (race right after Popen).
    assert merge(local(L.STOPPED, running=True, since_s=since_s), rem).state is expected


@pytest.mark.parametrize("rem", NON_MISSING, ids=NON_MISSING_IDS)
def test_running_takes_precedence_over_external(rem: RemoteSnapshot | None) -> None:
    result = merge(local(L.IDLE, running=True, external=True), rem)
    assert result.state is not D.EXTERNAL


# --- rule 4: our process is not running -------------------------------------------------


@pytest.mark.parametrize("rem", NON_MISSING, ids=NON_MISSING_IDS)
@pytest.mark.parametrize("forced", [True, False])
def test_external_process_in_directory(rem: RemoteSnapshot | None, forced: bool) -> None:
    loc = local(L.STOPPED, running=False, external=True, forced=forced, stopped_ago_s=5)
    assert merge(loc, rem) == DisplayStatus(D.EXTERNAL, "被其他进程运行", st.DETAIL_EXTERNAL_LOCAL)


@pytest.mark.parametrize("rem", [ONLINE, ONLINE_BUSY], ids=["online", "online-busy"])
@pytest.mark.parametrize(
    ("forced", "stopped_ago_s", "expected"),
    [
        (True, 0, D.SESSION_LINGERING),
        (True, 59, D.SESSION_LINGERING),
        (True, 69, D.SESSION_LINGERING),
        (True, SESSION_LINGER_S - 1, D.SESSION_LINGERING),  # 239 s
        (True, SESSION_LINGER_S - 0.5, D.SESSION_LINGERING),
        (True, SESSION_LINGER_S, D.EXTERNAL),  # "< 240" is strict
        (True, SESSION_LINGER_S + 1, D.EXTERNAL),  # 241 s
        (True, 3600, D.EXTERNAL),
        (True, None, D.EXTERNAL),  # forced but stop time unknown
        (False, 0, D.EXTERNAL),  # graceful stop: GitHub drops the session itself
        (False, 100, D.EXTERNAL),
        (False, None, D.EXTERNAL),  # never stopped by us: service / other machine
    ],
)
def test_not_running_remote_online_session_linger_boundary(
    rem: RemoteSnapshot,
    forced: bool,
    stopped_ago_s: float | None,
    expected: DisplayState,
) -> None:
    loc = local(L.STOPPED, running=False, forced=forced, stopped_ago_s=stopped_ago_s)
    result = merge(loc, rem)
    assert result.state is expected
    assert result.label == LABELS[expected]
    if expected is D.EXTERNAL:
        assert result.detail == st.DETAIL_EXTERNAL_REMOTE
    else:
        assert "等待旧会话过期" in result.detail  # type: ignore[operator]


@pytest.mark.parametrize(
    ("stopped_ago_s", "remaining"),
    [
        (0, SESSION_LINGER_S),
        (100, SESSION_LINGER_S - 100),
        (SESSION_LINGER_S - 0.5, 1),  # rounds up, never "0 秒"
        (-30, SESSION_LINGER_S),  # stop time in the future (clock skew) is clamped
    ],
)
def test_session_lingering_detail_shows_remaining_seconds(
    stopped_ago_s: float, remaining: int
) -> None:
    loc = local(L.STOPPED, running=False, forced=True, stopped_ago_s=stopped_ago_s)
    result = merge(loc, ONLINE)
    assert result.state is D.SESSION_LINGERING
    assert f"约 {remaining} 秒" in result.detail  # type: ignore[operator]


@pytest.mark.parametrize("forced", [True, False])
@pytest.mark.parametrize("stale_state", [L.STOPPED, L.IDLE, L.BUSY, L.STARTING, L.RECONNECTING])
def test_not_running_remote_offline_is_stopped(forced: bool, stale_state: LocalState) -> None:
    # Local state left over from a finished process does not matter once it is not running.
    loc = local(stale_state, running=False, forced=forced, stopped_ago_s=5, job="build")
    assert merge(loc, OFFLINE) == DisplayStatus(D.STOPPED, "已停止", None)


@pytest.mark.parametrize("rem", [UNKNOWN, None], ids=["unknown", "none"])
@pytest.mark.parametrize("forced", [True, False])
def test_not_running_remote_unknown_is_stopped_with_note(
    rem: RemoteSnapshot | None, forced: bool
) -> None:
    loc = local(L.STOPPED, running=False, forced=forced, stopped_ago_s=5)
    assert merge(loc, rem) == DisplayStatus(D.STOPPED, "已停止", st.DETAIL_REMOTE_UNKNOWN)


def test_busy_remote_with_stopped_local_is_external() -> None:
    result = merge(local(L.STOPPED, running=False), ONLINE_BUSY)
    assert result.state is D.EXTERNAL


# --- whole input space -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "running", "external", "forced", "stopped_ago_s", "since_s"),
    list(
        itertools.product(
            LocalState, [True, False], [True, False], [True, False], [None, 10, 600], [0, 600]
        )
    ),
)
def test_every_combination_yields_consistent_status(
    state: LocalState,
    running: bool,
    external: bool,
    forced: bool,
    stopped_ago_s: float | None,
    since_s: float,
) -> None:
    loc = local(
        state,
        running=running,
        external=external,
        since_s=since_s,
        forced=forced,
        stopped_ago_s=stopped_ago_s,
        job="build",
    )
    for rem in [*NON_MISSING, MISSING]:
        result = merge(loc, rem)
        assert result.label == display_label(result.state, "build")
        assert result.state is not D.UNKNOWN  # merge always decides
        if not running and result.state in {D.IDLE, D.BUSY, D.STARTING, D.CONNECTING}:
            pytest.fail(f"stopped runner shown as {result.state}: {loc} / {rem}")


def test_naive_and_aware_datetimes_are_rejected() -> None:
    loc = local(L.IDLE, running=True)
    with pytest.raises(TypeError):
        merge_status(loc, OFFLINE, now=NOW.replace(tzinfo=None))


def test_snapshots_are_frozen() -> None:
    loc = local(L.IDLE, running=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        loc.state = L.BUSY  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ONLINE.busy = True  # type: ignore[misc]


# --- RemoteSnapshot.from_remote --------------------------------------------------------


def _remote_runner(
    runner_id: int, name: str, status: str = "online", busy: bool = False
) -> RemoteRunner:
    return RemoteRunner(id=runner_id, name=name, status=status, busy=busy)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("runner", "expected_status", "expected_busy"),
    [
        (None, RemoteStatus.MISSING, False),
        (_remote_runner(1, "a", "online", True), RemoteStatus.ONLINE, True),
        (_remote_runner(1, "a", "online", False), RemoteStatus.ONLINE, False),
        (_remote_runner(1, "a", "offline", False), RemoteStatus.OFFLINE, False),
    ],
)
def test_remote_snapshot_from_remote(
    runner: RemoteRunner | None, expected_status: RemoteStatus, expected_busy: bool
) -> None:
    snap = RemoteSnapshot.from_remote(runner, fetched_at=NOW)
    assert snap == RemoteSnapshot(expected_status, busy=expected_busy, fetched_at=NOW)
    assert RemoteSnapshot.from_remote(runner).fetched_at is None


def test_from_remote_none_feeds_missing_rule() -> None:
    snap = RemoteSnapshot.from_remote(None)
    assert merge(local(L.IDLE, running=True), snap).state is D.MISSING


# --- match_remote ----------------------------------------------------------------------


def _record(name: str = "PC-sandbox", runner_id: int | None = 42) -> RunnerRecord:
    return RunnerRecord(
        owner="octo", repo="sandbox", name=name, path="D:/runners/x", github_runner_id=runner_id
    )


def test_match_by_id_first_even_when_another_runner_has_the_name() -> None:
    by_name = _remote_runner(7, "PC-sandbox")
    by_id = _remote_runner(42, "renamed-on-github")
    assert match_remote(_record(), [by_name, by_id]) is by_id


def test_match_falls_back_to_name_case_insensitively_when_id_unknown() -> None:
    other = _remote_runner(1, "other")
    target = _remote_runner(99, "pc-SANDBOX")  # re-registered: new id, same name
    assert match_remote(_record(), [other, target]) is target


def test_match_by_name_when_record_has_no_id() -> None:
    target = _remote_runner(5, "PC-SANDBOX")
    assert match_remote(_record(runner_id=None), [target]) is target


@pytest.mark.parametrize(
    "remotes",
    [
        [],
        [_remote_runner(1, "other"), _remote_runner(2, "PC-sandbox-2")],
        [_remote_runner(3, "PC-sandbo")],
    ],
)
def test_match_returns_none_when_neither_id_nor_name_match(remotes: list[RemoteRunner]) -> None:
    assert match_remote(_record(), remotes) is None
    assert match_remote(_record(runner_id=None), remotes) is None


def test_match_accepts_any_sequence() -> None:
    target = _remote_runner(42, "x")
    assert match_remote(_record(), (target,)) is target
