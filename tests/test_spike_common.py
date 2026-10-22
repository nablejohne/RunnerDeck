"""Tests for the M0 spike helpers (scripts/spike_common.py)."""

import json

import pytest

from spike_common import (
    Redactor,
    RunnerEvent,
    asset_sha256,
    classify_line,
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


# Provisional: patterns come from the plan; M0 replaces these with real stdout samples.
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("2026-10-22 07:00:00Z: Listening for Jobs", RunnerEvent("idle", None)),
        ("2026-10-22 07:01:00Z: Running job: build", RunnerEvent("busy", "build")),
        (
            "2026-10-22 07:02:00Z: Job build completed with result: Succeeded",
            RunnerEvent("completed", "build: Succeeded"),
        ),
        ("Connected to GitHub", None),
    ],
)
def test_classify_line(line, expected):
    assert classify_line(line) == expected


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
