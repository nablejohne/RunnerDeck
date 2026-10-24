"""core/workflows.py and core/update_check.py (docs/m4-design.md §4, §8)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from runnerdeck.core import update_check, workflows
from runnerdeck.core.errors import PermissionDeniedError
from runnerdeck.core.models import JobTarget, MatchKind, WorkflowRun

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
WORKFLOW = """
name: ci
on: [push]
jobs:
  plain:
    runs-on: [self-hosted, Windows, runnerdeck-spike]
    steps: [{run: echo}]
  string:
    runs-on: self-hosted
  missing:
    runs-on: [self-hosted, gpu]
  hosted:
    runs-on: ubuntu-latest
  matrix:
    runs-on: ${{ matrix.os }}
  group:
    runs-on: {group: build-pool, labels: [self-hosted]}
  group_only:
    runs-on: {group: build-pool}
  reusable:
    uses: ./.github/workflows/other.yml
"""


def test_scan() -> None:
    targets = {t.job: t for t in workflows.scan("ci.yml", WORKFLOW)}
    assert "reusable" not in targets
    assert targets["plain"].labels == ("self-hosted", "Windows", "runnerdeck-spike")
    assert targets["string"].labels == ("self-hosted",)
    assert targets["matrix"].dynamic and targets["matrix"].labels is None
    assert targets["group"].group == "build-pool" and targets["group"].labels == ("self-hosted",)
    assert targets["group_only"].labels is None


@pytest.mark.parametrize("text", ["jobs: [", "name: x", "- a\n- b"])
def test_scan_rejects_unreadable_files(text: str) -> None:
    with pytest.raises(workflows.WorkflowParseError):
        workflows.scan("x.yml", text)


def test_match_against_runner_labels() -> None:
    labels = workflows.runner_labels(["runnerdeck-spike"])
    assert labels == ["self-hosted", "Windows", "X64", "runnerdeck-spike"]
    kinds = {t.job: workflows.match(t, labels) for t in workflows.scan("ci.yml", WORKFLOW)}
    assert kinds["plain"].kind is MatchKind.OK  # case-insensitive: "windows" vs "Windows"
    assert kinds["string"].kind is MatchKind.OK
    assert (kinds["missing"].kind, kinds["missing"].missing) == (MatchKind.MISSING, ("gpu",))
    assert kinds["hosted"].kind is MatchKind.NOT_SELF_HOSTED
    assert kinds["matrix"].kind is MatchKind.UNKNOWN
    assert kinds["group_only"].kind is MatchKind.UNKNOWN
    shown = {m.target.job for m in workflows.relevant(kinds.values())}
    assert "hosted" not in shown and "plain" in shown


def test_labels_without_self_hosted_that_fit_are_ok() -> None:
    target = JobTarget("w.yml", "j", ("windows", "runnerdeck-spike"))
    assert workflows.match(target, ["self-hosted", "Windows", "runnerdeck-spike"]).kind is (
        MatchKind.OK
    )
    assert workflows.runner_labels(["x"], ["self-hosted", "Linux"]) == ["self-hosted", "Linux"]


class FakeClient:
    def __init__(
        self, *, files_error: Exception | None = None, runs_error: Exception | None = None
    ):
        self.files_error, self.runs_error = files_error, runs_error

    def list_workflow_files(self, owner: str, repo: str) -> list[str]:
        if self.files_error:
            raise self.files_error
        return [
            ".github/workflows/ci.yml",
            ".github/workflows/bad.yml",
            ".github/workflows/gone.yml",
        ]

    def get_text_file(self, owner: str, repo: str, path: str) -> str | None:
        return {".github/workflows/ci.yml": WORKFLOW, ".github/workflows/bad.yml": "jobs: ["}.get(
            path
        )

    def list_runs(self, owner: str, repo: str) -> list[WorkflowRun]:
        if self.runs_error:
            raise self.runs_error
        return [WorkflowRun(id=1, html_url="https://x")]


def test_collect_insights() -> None:
    insights = workflows.collect_insights(
        FakeClient(),
        "octo",
        "demo",
        ["self-hosted", "Windows"],
        now=NOW,  # type: ignore[arg-type]
    )
    assert insights.fetched_at == NOW and len(insights.runs) == 1
    assert {m.target.job for m in insights.matches} >= {"plain", "missing", "matrix"}
    assert len(insights.parse_errors) == 1 and "bad.yml" in insights.parse_errors[0]
    assert insights.workflow_error is None and insights.runs_error is None


def test_collect_insights_halves_fail_on_their_own() -> None:
    denied = PermissionDeniedError("PAT 缺少 Contents: Read 权限", status=403)
    insights = workflows.collect_insights(
        FakeClient(files_error=denied),
        "o",
        "r",
        [],
        now=NOW,  # type: ignore[arg-type]
    )
    assert "Contents" in (insights.workflow_error or "") and len(insights.runs) == 1
    insights = workflows.collect_insights(
        FakeClient(runs_error=PermissionDeniedError("Actions", status=403)),
        "o",
        "r",
        [],
        now=NOW,  # type: ignore[arg-type]
    )
    assert insights.runs_error == "Actions" and insights.matches


# --- update_check ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "parsed"),
    [("1.0.0", (1, 0, 0)), ("v1.2.10", (1, 2, 10)), ("1.0.0-rc.1", None), ("x", None)],
)
def test_parse_version(value: str, parsed: Any) -> None:
    assert update_check.parse_version(value) == parsed


def test_compare() -> None:
    release = {"tag_name": "v1.0.1", "html_url": f"{update_check.RELEASES_URL}/tag/v1.0.1"}
    newer = update_check.compare("1.0.0", release)
    assert (newer.latest, newer.newer, newer.url) == ("1.0.1", True, release["html_url"])
    assert not update_check.compare("1.0.1", release).newer
    assert not update_check.compare("1.1.0", release).newer
    assert update_check.compare("1.0.0", None).latest is None
    assert update_check.compare("1.0.0", {"tag_name": "nightly"}).error


def test_partly_dynamic_runs_on_keeps_the_static_labels() -> None:
    text = 'jobs:\n  spike:\n    runs-on: [self-hosted, Windows, "${{ inputs.label }}"]\n'
    (target,) = workflows.scan("spike.yml", text)
    assert target.dynamic and target.labels == ("self-hosted", "Windows")
    assert workflows.match(target, ["self-hosted", "Windows"]).kind is MatchKind.UNKNOWN
    linux = workflows.match(target, ["self-hosted", "Linux"])
    assert (linux.kind, linux.missing) == (MatchKind.MISSING, ("Windows",))


def test_compare_only_links_to_our_release_page() -> None:
    evil = update_check.compare(
        "1.0.0", {"tag_name": "v2.0.0", "html_url": "https://evil.example/x"}
    )
    assert evil.url == update_check.RELEASES_URL
    ours = "https://github.com/nablejohne/RunnerDeck/releases/tag/v2.0.0"
    assert update_check.compare("1.0.0", {"tag_name": "v2.0.0", "html_url": ours}).url == ours
