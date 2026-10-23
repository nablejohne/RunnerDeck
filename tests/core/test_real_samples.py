"""Checks against output captured on a real Windows machine (2026-10-23, runner 2.338.1).

See docs/spike-notes.md "第 2 轮". These pin behaviour that unit tests elsewhere only assume.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from runnerdeck.core import installer, runner_file
from runnerdeck.core.models import GitHubUser, RemoteRunner, Repo, RunnerToken
from runnerdeck.core.release import parse_release

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _fake_run(output: bytes):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=output)

    return run


def _runner_dir(tmp_path: Path) -> Path:
    for name in ("config.cmd", "run.cmd"):
        (tmp_path / name).write_text("@echo off\r\n", encoding="ascii")
    return tmp_path


def test_replace_output_counts_as_registered(tmp_path: Path) -> None:
    out = (FIXTURES / "config" / "config_replaced.txt").read_bytes()
    assert b"Successfully replaced the runner" in out
    result = installer.configure(
        _runner_dir(tmp_path),
        url="https://github.com/nablejohne/runnerdeck-sandbox",
        token="AABBCCDDEEFF00112233",
        name="host-sandbox",
        labels=[],
        run=_fake_run(out),
    )
    assert "Successfully replaced the runner" in result.output


def test_remove_of_a_runner_gone_from_github_still_succeeds(tmp_path: Path) -> None:
    out = (FIXTURES / "config" / "remove_already_gone.txt").read_bytes()
    assert b"Does not exist. Skipping Removing runner from the server" in out
    result = installer.unconfigure(
        _runner_dir(tmp_path), token="AABBCCDDEEFF00112233", run=_fake_run(out)
    )
    assert result.returncode == 0


def test_real_runner_file_parses(tmp_path: Path) -> None:
    shutil.copy(FIXTURES / "runner" / "runner_file.json", tmp_path / ".runner")
    info = runner_file.read_runner_file(tmp_path)
    assert info is not None
    assert (info.agent_id, info.agent_name, info.work_folder) == (
        23,
        "LAPTOP-8LU6A21K-runnerdeck-sandbox",
        "_work",
    )
    assert runner_file.parse_github_url(info.github_url) == ("nablejohne", "runnerdeck-sandbox")
    assert info.raw["useV2Flow"] is True


def _api(name: str) -> object:
    return json.loads((FIXTURES / "api" / name).read_text(encoding="utf-8"))


def test_real_api_responses_validate() -> None:
    assert GitHubUser.model_validate(_api("user.json")).login == "nablejohne"
    assert Repo.model_validate({**_api("repo.json"), "owner": "nablejohne"}).private is True
    token = RunnerToken.model_validate({**_api("registration_token.json"), "token": "x" * 29})
    assert token.expires_at.utcoffset() is not None  # GitHub sends +08:00, not Z
    runners = _api("runners.json")
    assert [RemoteRunner.model_validate(r) for r in runners["runners"]] == []
    info = parse_release(_api("release_latest.json"))
    assert info.asset_name == f"actions-runner-win-x64-{info.version}.zip"
    assert len(info.sha256) == 64
