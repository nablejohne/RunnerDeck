"""A workflow job's log, read for the detail page's Job tab (pure; the UI may import it).

GitHub serves a job's log only once the job completed (spike-notes §13). Every line starts
with an ISO timestamp ("2026-10-26T03:29:45.7028124Z ") and may carry ANSI colours and
workflow markers (##[group], ##[endgroup], ##[error], ##[warning]). Step timestamps from the
API have whole seconds and neighbouring steps often share one, so the log is not split per
step: the failure excerpt is simply what led up to the first ##[error].
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta, tzinfo

from runnerdeck.core.models import JobSnapshot, JobStep, WorkflowJob

EXCERPT_LINES = 30
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z ?")
_ERROR = "##[error]"
_WARNING = "##[warning]"
_GROUP = "##[group]"
_END_GROUP = "##[endgroup]"


def _parse(text: str, tz: tzinfo | None) -> list[tuple[str, str]]:
    """[(kind, display line)], kind in plain / group / error / warning; ##[endgroup] dropped."""
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = _ANSI_RE.sub("", raw).rstrip("\r")
        stamp = ""
        match = _STAMP_RE.match(line)
        if match:
            when = datetime.fromisoformat(match.group(1)).replace(tzinfo=UTC).astimezone(tz)
            stamp = when.strftime("%H:%M:%S") + "  "
            line = line[match.end() :]
        if line.startswith(_END_GROUP):
            continue
        if line.startswith(_GROUP):
            out.append(("group", f"{stamp}▸ {line[len(_GROUP) :]}"))
        elif line.startswith(_ERROR):
            out.append(("error", f"{stamp}错误：{line[len(_ERROR) :]}"))
        elif line.startswith(_WARNING):
            out.append(("warning", f"{stamp}警告：{line[len(_WARNING) :]}"))
        else:
            out.append(("plain", stamp + line))
    return out


def clean_lines(text: str, tz: tzinfo | None = None) -> list[str]:
    """The log as shown: local HH:MM:SS instead of the ISO stamp, no ANSI colours, markers
    spelled out (▸ group, 错误：, 警告：)."""
    return [line for _kind, line in _parse(text, tz)]


def error_messages(text: str) -> list[str]:
    """The ##[error] messages, without timestamps."""
    found: list[str] = []
    for raw in text.splitlines():
        line = _STAMP_RE.sub("", _ANSI_RE.sub("", raw).rstrip("\r"), count=1)
        if line.startswith(_ERROR):
            found.append(line[len(_ERROR) :].strip())
    return found


def failure_excerpt(text: str, tz: tzinfo | None = None, lines: int = EXCERPT_LINES) -> list[str]:
    """The last `lines` lines up to and including the first ##[error] ([] without one)."""
    parsed = _parse(text, tz)
    first = next((i for i, (kind, _line) in enumerate(parsed) if kind == "error"), None)
    if first is None:
        return []
    return [line for _kind, line in parsed[max(0, first + 1 - lines) : first + 1]]


def summarize(
    job: WorkflowJob | None, log_text: str | None, tz: tzinfo | None = None
) -> JobSnapshot:
    """A snapshot for the Job tab; the log parts only for a completed job with a log."""
    if job is None or not job.done:
        return JobSnapshot(job=job)
    if log_text is None:
        return JobSnapshot(job=job, log_pending=True)
    return JobSnapshot(
        job=job,
        log=tuple(clean_lines(log_text, tz)),
        errors=tuple(error_messages(log_text)),
        excerpt=tuple(failure_excerpt(log_text, tz)),
    )


def runner_job(jobs: Iterable[WorkflowJob], runner_id: int) -> WorkflowJob | None:
    """The newest of `jobs` that ran on `runner_id`."""
    mine = [job for job in jobs if job.runner_id == runner_id]
    return max(mine, key=lambda job: job.id, default=None)


def duration(start: datetime | None, end: datetime | None, now: datetime | None = None) -> str:
    """ "11m22s" / "40s" / "1h02m" ("" without a start); a running item counts up to `now`."""
    if start is None:
        return ""
    stop = end or now
    if stop is None:
        return ""
    seconds = max(0, int((stop - start) / timedelta(seconds=1)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def first_failed_step(job: WorkflowJob) -> JobStep | None:
    failed = job.failed_steps
    return failed[0] if failed else None


def summary_line(snapshot: JobSnapshot) -> str | None:
    """One line for 实时输出 and the tray once a job completed, e.g. "job build 失败
    （Install dependencies，耗时 11m22s）：Process completed with exit code 1." (None while
    it runs)."""
    job = snapshot.job
    if job is None or not job.done:
        return None
    verdict = {"success": "成功", "failure": "失败", "cancelled": "已取消"}.get(
        job.conclusion or "", job.conclusion or "结束"
    )
    notes: list[str] = []
    step = first_failed_step(job)
    if step is not None:
        notes.append(step.name)
    took = duration(job.started_at, job.completed_at)
    if took:
        notes.append(f"耗时 {took}")
    paren = f"（{'，'.join(notes)}）" if notes else ""
    error = f"：{snapshot.errors[0]}" if snapshot.errors and job.conclusion != "success" else ""
    return f"job {job.name} {verdict}{paren}{error}"


def failure_hint(job: WorkflowJob | None) -> str | None:
    """The runner card's line for a failed or cancelled last job (None otherwise)."""
    if job is None or not job.done or job.conclusion not in ("failure", "cancelled", "timed_out"):
        return None
    verdict = "已取消" if job.conclusion == "cancelled" else "失败"
    step = first_failed_step(job)
    where = f"：{step.name}" if step is not None else ""
    return f"上次 job {job.name} {verdict}{where}（点此查看）"


def step_states(steps: Sequence[JobStep]) -> list[tuple[str, JobStep]]:
    """(icon, step) for the step list: ✓ success, ✗ failure, ⟳ running, ⊘ skipped, ○ waiting,
    ■ cancelled."""
    icons = {"success": "✓", "failure": "✗", "skipped": "⊘", "cancelled": "■"}
    rows: list[tuple[str, JobStep]] = []
    for step in steps:
        if step.status == "completed":
            icon = icons.get(step.conclusion or "", "•")
        elif step.status == "in_progress":
            icon = "⟳"
        else:
            icon = "○"
        rows.append((icon, step))
    return rows
