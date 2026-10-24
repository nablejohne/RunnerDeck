"""Which workflow jobs a runner can pick up: `runs-on` vs. its labels (docs/m4-design.md §4).

Pure functions over YAML text (PyYAML safe_load). GitHub hands a job to a self-hosted runner
only when the runner has every label the job asks for (case-insensitive); a job asking for a
label nobody has stays queued forever, which is exactly what this is meant to point out.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

import yaml

from runnerdeck.core.errors import RunnerDeckError
from runnerdeck.core.models import JobMatch, JobTarget, MatchKind, RepoInsights, WorkflowRun

if TYPE_CHECKING:
    from runnerdeck.core.github_client import GitHubClient

DEFAULT_LABELS = ("self-hosted", "Windows", "X64")  # what every Windows x64 runner adds itself
SELF_HOSTED = "self-hosted"
MAX_WORKFLOW_FILES = 30


class WorkflowParseError(ValueError):
    """The file is not a workflow we can read (bad YAML, no `jobs` mapping)."""


def scan(path: str, text: str) -> list[JobTarget]:
    """The `runs-on` of every job in one workflow file; reusable-workflow calls (`uses:`)
    have no runs-on and are skipped. WorkflowParseError for unreadable YAML."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise WorkflowParseError(f"{path}：YAML 无法解析（{type(e).__name__}）") from e
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, dict):
        raise WorkflowParseError(f"{path}：没有 jobs")
    targets: list[JobTarget] = []
    for job_id, job in jobs.items():
        if not isinstance(job, dict) or "uses" in job or "runs-on" not in job:
            continue
        targets.append(_target(path, str(job_id), job["runs-on"]))
    return targets


def _target(path: str, job: str, runs_on: Any) -> JobTarget:
    group: str | None = None
    if isinstance(runs_on, dict):
        group = runs_on.get("group") if isinstance(runs_on.get("group"), str) else None
        runs_on = runs_on.get("labels")
    if runs_on is None:
        return JobTarget(path, job, None, group=group)
    values = [runs_on] if isinstance(runs_on, str) else runs_on
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        return JobTarget(path, job, None, group=group, dynamic=True)
    static = tuple(v.strip() for v in values if v.strip() and "${{" not in v)
    if len(static) < len([v for v in values if v.strip()]):
        # e.g. [self-hosted, Windows, "${{ inputs.label }}"]: the static labels still count.
        return JobTarget(path, job, static or None, group=group, dynamic=True)
    return JobTarget(path, job, static, group=group)


def runner_labels(custom: Iterable[str], remote: Sequence[str] | None = None) -> list[str]:
    """The runner's labels: GitHub's list when known, else defaults + the record's own."""
    if remote:
        return list(remote)
    return [*DEFAULT_LABELS, *custom]


def match(target: JobTarget, labels: Iterable[str]) -> JobMatch:
    have = {label.casefold() for label in labels}
    if target.labels is None:
        return JobMatch(target, MatchKind.UNKNOWN)
    wanted = target.labels
    if target.dynamic:
        # Only the static part is known: missing there is certain, the rest is not.
        missing = tuple(w for w in wanted if w.casefold() not in have)
        return JobMatch(target, MatchKind.MISSING if missing else MatchKind.UNKNOWN, missing)
    if SELF_HOSTED not in {w.casefold() for w in wanted}:
        # GitHub-hosted (ubuntu-latest, ...), or a group-only / label-only target that still
        # could be ours: only call it hosted when it does not fit this runner at all.
        if wanted and all(w.casefold() in have for w in wanted):
            return JobMatch(target, MatchKind.OK)
        if target.group is not None:
            return JobMatch(target, MatchKind.UNKNOWN)
        return JobMatch(target, MatchKind.NOT_SELF_HOSTED)
    missing = tuple(w for w in wanted if w.casefold() not in have)
    return JobMatch(target, MatchKind.MISSING if missing else MatchKind.OK, missing)


def relevant(matches: Iterable[JobMatch]) -> list[JobMatch]:
    """What the detail page lists: everything except GitHub-hosted jobs."""
    return [m for m in matches if m.kind is not MatchKind.NOT_SELF_HOSTED]


def collect_insights(
    client: GitHubClient, owner: str, repo: str, labels: Sequence[str], *, now: datetime
) -> RepoInsights:
    """Workflow jobs matched against `labels` and the newest runs, each half failing on its
    own (a PAT without Contents: Read still shows the runs, and the other way round)."""
    matches: list[JobMatch] = []
    parse_errors: list[str] = []
    workflow_error: str | None = None
    try:
        for path in client.list_workflow_files(owner, repo)[:MAX_WORKFLOW_FILES]:
            text = client.get_text_file(owner, repo, path)
            if text is None:
                continue
            try:
                targets = scan(path, text)
            except WorkflowParseError as e:
                parse_errors.append(str(e))
                continue
            matches.extend(match(t, labels) for t in targets)
    except RunnerDeckError as e:
        workflow_error = str(e)
    runs: tuple[WorkflowRun, ...] = ()
    runs_error: str | None = None
    try:
        runs = tuple(client.list_runs(owner, repo))
    except RunnerDeckError as e:
        runs_error = str(e)
    return RepoInsights(
        matches=tuple(relevant(matches)),
        runs=runs,
        workflow_error=workflow_error,
        runs_error=runs_error,
        parse_errors=tuple(parse_errors),
        fetched_at=now,
    )
