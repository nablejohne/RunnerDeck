"""Live GitHub checks for GitHubClient. Skipped unless RUNNERDECK_TEST_PAT / _REPO are set.

The PAT needs Administration: Read and write on the test repo (registration token). Nothing
here prints or asserts on token values; the client registers them for redaction.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from runnerdeck.core.github_client import GitHubClient
from runnerdeck.core.models import parse_full_name
from runnerdeck.core.redact import redact

PAT = os.environ.get("RUNNERDECK_TEST_PAT", "").strip()
REPO = os.environ.get("RUNNERDECK_TEST_REPO", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (PAT and REPO), reason="RUNNERDECK_TEST_PAT / RUNNERDECK_TEST_REPO not set"
    ),
]


@pytest.fixture(scope="module")
def owner_repo() -> tuple[str, str]:
    return parse_full_name(REPO)


@pytest.fixture(scope="module")
def client() -> Iterator[GitHubClient]:
    with GitHubClient(PAT) as c:  # HTTPS_PROXY / system proxy apply as in production
        yield c


def test_get_user(client: GitHubClient, owner_repo: tuple[str, str]) -> None:
    user = client.get_user()
    assert user.login
    assert user.id > 0
    expected_kind = "fine-grained" if PAT.startswith("github_pat_") else "classic"
    assert user.token_kind == expected_kind
    if user.token_expires_at is not None:
        assert user.token_expires_at.tzinfo is not None


def test_list_repos_contains_test_repo(client: GitHubClient, owner_repo: tuple[str, str]) -> None:
    repos = client.list_repos()
    names = {r.full_name.lower() for r in repos}
    assert f"{owner_repo[0]}/{owner_repo[1]}".lower() in names
    assert len(names) == len(repos)  # pagination did not repeat a page


def test_get_repo(client: GitHubClient, owner_repo: tuple[str, str]) -> None:
    repo = client.get_repo(*owner_repo)
    assert repo.full_name.lower() == REPO.lower()


def test_create_registration_token(client: GitHubClient, owner_repo: tuple[str, str]) -> None:
    token = client.create_registration_token(*owner_repo)
    secret = token.token.get_secret_value()
    assert secret not in repr(token)
    assert len(secret) >= 20
    assert token.expires_at > datetime.now(UTC)
    assert secret not in redact(f"config.cmd --token {secret} {secret}")


def test_list_runners(client: GitHubClient, owner_repo: tuple[str, str]) -> None:
    runners = client.list_runners(*owner_repo)
    assert isinstance(runners, list)
    for runner in runners:
        assert runner.status in ("online", "offline")
        found = client.get_runner(*owner_repo, runner.id)
        assert found is not None
        assert found.id == runner.id


def test_get_runner_release(client: GitHubClient) -> None:
    latest = client.get_runner_release()
    tag = latest["tag_name"]
    assert tag.startswith("v")
    assert any(a["name"].startswith("actions-runner-win-x64-") for a in latest["assets"])
    pinned = client.get_runner_release(tag.removeprefix("v"))
    assert pinned["tag_name"] == tag
