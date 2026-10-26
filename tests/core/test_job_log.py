"""job_log on a real failed job (runnerdeck-sandbox spike.yml with fail=true, spike-notes §13)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from runnerdeck.core import job_log
from runnerdeck.core.models import JobStep, WorkflowJob

API = Path(__file__).resolve().parents[1] / "fixtures" / "api"
LOG = (API / "job_failed.log").read_bytes().decode("utf-8-sig")


def failed_job() -> WorkflowJob:
    return WorkflowJob.model_validate(json.loads((API / "run_jobs.json").read_text())["jobs"][0])


def test_fixture_job() -> None:
    job = failed_job()
    assert (job.done, job.conclusion, job.runner_id) == (True, "failure", 28)
    assert [s.name for s in job.failed_steps] == ["Fail on request"]
    assert job_log.first_failed_step(job) is job.failed_steps[0]


def test_clean_lines() -> None:
    lines = job_log.clean_lines(LOG, UTC)
    assert lines[0] == "03:29:45  Current runner version: '2.338.1'"
    assert "03:29:45  ▸ Run echo spike job start on %RUNNER_NAME%" in lines
    assert "03:29:45  echo spike job start on %RUNNER_NAME%" in lines  # ANSI colours gone
    assert "03:30:25  错误：Process completed with exit code 1." in lines
    assert not [line for line in lines if "##[" in line or "\x1b" in line]


def test_errors_and_excerpt() -> None:
    assert job_log.error_messages(LOG) == ["Process completed with exit code 1."]
    excerpt = job_log.failure_excerpt(LOG, UTC, lines=4)
    assert excerpt == [
        "03:30:25  ▸ Run exit 1",
        "03:30:25  exit 1",
        '03:30:25  shell: C:\\WINDOWS\\system32\\cmd.EXE /D /E:ON /V:OFF /S /C "CALL "{0}""',
        "03:30:25  错误：Process completed with exit code 1.",
    ]
    assert job_log.failure_excerpt("2026-10-26T03:29:45.1Z all good\n") == []


def test_summarize() -> None:
    job = failed_job()
    snapshot = job_log.summarize(job, LOG, UTC)
    assert snapshot.errors == ("Process completed with exit code 1.",)
    assert snapshot.excerpt[-1].endswith("错误：Process completed with exit code 1.")
    assert len(snapshot.log) == len(job_log.clean_lines(LOG))
    assert job_log.summarize(job, None).log_pending
    running = job.model_copy(update={"status": "in_progress", "conclusion": None})
    assert job_log.summarize(running, LOG).log == ()
    assert job_log.summarize(None, None).job is None


def test_summary_line() -> None:
    job = failed_job()
    line = job_log.summary_line(job_log.summarize(job, LOG))
    assert (
        line == "job spike 失败（Fail on request，耗时 48s）：Process completed with exit code 1."
    )
    ok = job.model_copy(update={"conclusion": "success", "steps": []})
    assert job_log.summary_line(job_log.summarize(ok, "")) == "job spike 成功（耗时 48s）"
    cancelled = job.model_copy(update={"conclusion": "cancelled", "steps": []})
    assert job_log.summary_line(job_log.summarize(cancelled, "")) == "job spike 已取消（耗时 48s）"
    running = job.model_copy(update={"status": "in_progress"})
    assert job_log.summary_line(job_log.summarize(running, None)) is None


def test_runner_job_and_steps() -> None:
    job = failed_job()
    other = job.model_copy(update={"id": job.id + 1, "runner_id": 99})
    newer = job.model_copy(update={"id": job.id + 2})
    assert job_log.runner_job([job, other, newer], 28) is newer
    assert job_log.runner_job([other], 28) is None
    icons = [icon for icon, _ in job_log.step_states(job.steps)]
    assert icons == ["✓", "⊘", "✓", "✓", "✗", "✓"]
    waiting = JobStep(number=1, name="a", status="queued")
    running = JobStep(number=2, name="b", status="in_progress")
    assert [i for i, _ in job_log.step_states([waiting, running])] == ["○", "⟳"]


@pytest.mark.parametrize(
    ("seconds", "text"), [(0, "0s"), (40, "40s"), (682, "11m22s"), (3720, "1h02m")]
)
def test_duration(seconds: int, text: str) -> None:
    start = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)
    assert job_log.duration(start, start + timedelta(seconds=seconds)) == text


def test_duration_of_a_running_item() -> None:
    start = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)
    assert job_log.duration(start, None, now=start + timedelta(seconds=5)) == "5s"
    assert job_log.duration(start, None) == ""
    assert job_log.duration(None, start) == ""
