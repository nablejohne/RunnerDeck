"""Tests for the M0 spike helpers (scripts/spike_common.py)."""

import json
from pathlib import Path

import pytest

from spike_common import (
    Redactor,
    RunnerEvent,
    asset_sha256,
    classify_line,
    decode_output,
    extract_sha256,
    looks_like_pat,
    parse_repo,
    pick_asset,
    read_runner_file,
    scrub,
)

# Excerpt of the actions/runner v2.338.0 release notes.
RELEASE_BODY = """\
## SHA-256 Checksums
- actions-runner-win-x64-2.338.0.zip <!-- BEGIN SHA win-x64 -->f48e0750a21812bca5f82de5f7f5aeae71abee647fab5a582f1742d07eba455f<!-- END SHA win-x64 -->
- actions-runner-win-arm64-2.338.0.zip <!-- BEGIN SHA win-arm64 -->a57a4d29c0dcf02210a16b01906ed76cb8091b26d728c41232736c0f627a557e<!-- END SHA win-arm64 -->
"""  # noqa: E501
WIN_X64_SHA = "f48e0750a21812bca5f82de5f7f5aeae71abee647fab5a582f1742d07eba455f"


def test_extract_sha256_picks_requested_platform():
    assert extract_sha256(RELEASE_BODY) == WIN_X64_SHA
    assert extract_sha256(RELEASE_BODY, "win-arm64").startswith("a57a4d29")


def test_extract_sha256_missing_platform():
    assert extract_sha256(RELEASE_BODY, "osx-x64") is None


def test_pick_asset_and_digest():
    release = {
        "tag_name": "v2.338.0",
        "assets": [
            {"name": "actions-runner-win-arm64-2.338.0.zip", "digest": "sha256:aa"},
            {"name": "actions-runner-win-x64-2.338.0.zip", "digest": f"sha256:{WIN_X64_SHA}"},
        ],
    }
    asset = pick_asset(release)
    assert asset["name"] == "actions-runner-win-x64-2.338.0.zip"
    assert asset_sha256(asset) == WIN_X64_SHA


def test_pick_asset_missing():
    with pytest.raises(LookupError):
        pick_asset({"tag_name": "v1", "assets": []})


@pytest.mark.parametrize("value", ["owner/repo", " owner/repo "])
def test_parse_repo(value):
    assert parse_repo(value) == ("owner", "repo")


@pytest.mark.parametrize("value", ["repo", "/repo", "owner/", "a/b/c"])
def test_parse_repo_rejects(value):
    with pytest.raises(ValueError):
        parse_repo(value)


def test_redactor_masks_known_and_pattern_tokens():
    redact = Redactor()
    redact.add("AABBCCDDEEFF00112233")
    text = (
        "reg AABBCCDDEEFF00112233 pat ghp_"
        + "x" * 36
        + " fg github_pat_"
        + "y" * 40
        + " cmd --token SECRETVALUE"
    )
    out = redact(text)
    assert "AABBCC" not in out
    assert "ghp_" not in out and "github_pat_" not in out
    assert "SECRETVALUE" not in out
    assert "--token ***" in out


def test_scrub_masks_token_keys_recursively():
    data = {"token": "abc", "nested": [{"token": "def", "expires_at": "x"}]}
    assert scrub(data) == {"token": "***", "nested": [{"token": "***", "expires_at": "x"}]}


def test_read_runner_file_handles_bom(tmp_path):
    path = tmp_path / ".runner"
    payload = {"agentId": 42, "agentName": "host-repo", "workFolder": "_work"}
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode())
    assert read_runner_file(path) == payload


STDOUT = Path(__file__).parent / "fixtures" / "stdout"


def events_of(name):
    lines = (STDOUT / name).read_text(encoding="utf-8").splitlines()
    return [e for e in map(classify_line, lines) if e]


def test_classify_job_then_ctrl_break():
    assert events_of("job_then_ctrl_break.txt") == [
        RunnerEvent("connected", None),
        RunnerEvent("version", "2.338.0"),
        RunnerEvent("idle", None),
        RunnerEvent("busy", "spike"),
        RunnerEvent("completed", "spike: Failed"),
        RunnerEvent("stopping", None),
        RunnerEvent("exited", "0"),
    ]


def test_classify_session_conflict_then_reconnect():
    kinds = [e.kind for e in events_of("session_conflict_reconnect.txt")]
    assert kinds.count("conflict") == 4
    assert kinds[-3:] == ["reconnected", "version", "idle"]


def test_classify_killed_listener_exit_code():
    assert events_of("conflict_then_kill_listener.txt")[-1] == RunnerEvent("exited", "15")


def test_decode_output_falls_back_to_console_codepage():
    lines = (STDOUT / "job_then_ctrl_break.cp936.raw").read_bytes().splitlines()
    assert decode_output(lines[0], fallback="gbk").startswith("已复制")
    assert "(Y/N)" in decode_output(lines[-3], fallback="gbk")
    assert decode_output(b"Listening for Jobs") == "Listening for Jobs"


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("github_pat_" + "a" * 70, True),
        ("ghp_" + "b" * 36, True),
        ("runnerdeck-sandbox", False),
        ("ghp_short", False),
    ],
)
def test_looks_like_pat(value, ok):
    assert looks_like_pat(value) is ok
