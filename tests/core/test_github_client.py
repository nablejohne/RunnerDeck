"""GitHubClient tests: respx for HTTP, local socket stubs for proxy routing. No real network."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socketserver
import threading
import urllib.request
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest
import respx
from httpcore._backends.sync import SyncBackend

from runnerdeck.core import github_client as gc
from runnerdeck.core.errors import (
    AuthError,
    GitHubError,
    InvalidInputError,
    NetworkError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    RunnerDeckError,
)
from runnerdeck.core.github_client import API_URL, GitHubClient, env_proxy_map
from runnerdeck.core.redact import redact

# Obviously fake values. FINE/CLASSIC match redact's token patterns; opaque ones do not, so
# they are only masked if the client registered them.
FINE_PAT = "github_pat_" + "FAKE" * 15
CLASSIC_PAT = "ghp_" + "0" * 36
REPO_PATH = "/repos/octo/demo"
RUNNERS_PATH = f"{REPO_PATH}/actions/runners"
DL_URL = (
    "https://github.com/actions/runner/releases/download/v2.338.0/"
    "actions-runner-win-x64-2.338.0.zip"
)
CDN_URL = (
    "https://release-assets.githubusercontent.com/github-production-release-asset/1/asset"
    "?X-Amz-Signature=FAKESIGNATURE0000&sp=r"
)


def opaque(prefix: str = "FAKE-opaque") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def api(path: str) -> str:
    return f"{API_URL}{path}"


def repo_json(name: str = "demo", private: bool = True) -> dict[str, Any]:
    return {
        "name": name,
        "full_name": f"octo/{name}",
        "owner": {"login": "octo", "id": 1},
        "private": private,
        "html_url": f"https://github.com/octo/{name}",
        "fork": False,
        "archived": False,
    }


def runner_json(runner_id: int = 7, name: str = "host-demo") -> dict[str, Any]:
    return {
        "id": runner_id,
        "name": name,
        "os": "Windows",
        "status": "online",
        "busy": False,
        "labels": [{"id": 1, "name": "self-hosted", "type": "read-only"}, {"name": "gpu"}],
    }


@pytest.fixture(autouse=True)
def no_system_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """This dev machine (and maybe CI) has HTTPS_PROXY / a system proxy: pin it to none."""
    monkeypatch.setattr(gc, "_getproxies", lambda: {})


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def client() -> Iterator[GitHubClient]:
    with GitHubClient(FINE_PAT) as c:
        yield c


# --- construction / headers / secrets ----------------------------------------------------


def test_default_headers_sent_through_real_transport(mock: respx.MockRouter) -> None:
    route = mock.get(api("/user")).respond(200, json={"login": "octo", "id": 1})
    with GitHubClient(f"  {FINE_PAT}\n") as c:  # stripped
        c.get_user()
    headers = route.calls.last.request.headers
    assert headers["authorization"] == f"Bearer {FINE_PAT}"
    assert headers["accept"] == "application/vnd.github+json"
    assert headers["x-github-api-version"] == "2022-11-28"
    assert headers["user-agent"].startswith("RunnerDeck/")


@pytest.mark.parametrize("bad", ["", "   ", "abc def", "tok\nen", "токен-не-ascii"])
def test_malformed_pat_rejected_without_echo(bad: str) -> None:
    with pytest.raises(InvalidInputError) as ei:
        GitHubClient(bad)
    assert "token 的值" in str(ei.value)
    if bad.strip():
        assert bad.strip() not in str(ei.value)


def test_pat_is_registered_for_redaction() -> None:
    pat = opaque("FAKE-pat")
    GitHubClient(pat).close()
    assert redact(f"x {pat} y") == "x *** y"


def test_context_manager_closes_http_client() -> None:
    with GitHubClient(FINE_PAT) as c:
        assert isinstance(c, GitHubClient)
    assert c._http.is_closed


def test_injected_transport_serves_api_and_download(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"login": "octo", "id": 1})
        return httpx.Response(200, content=b"zip-bytes")

    transport = httpx.MockTransport(handler)
    with GitHubClient(FINE_PAT, transport=transport) as c:
        c.get_user()
        c.download(DL_URL, tmp_path / "a.zip")
        c.get_user()  # the download client must not have closed the shared transport
    assert [r.url.host for r in seen] == ["api.github.com", "github.com", "api.github.com"]
    assert "authorization" not in seen[1].headers
    assert seen[2].headers["authorization"] == f"Bearer {FINE_PAT}"


# --- get_user -------------------------------------------------------------------------------


def test_get_user_fine_grained_with_expiration(
    mock: respx.MockRouter, client: GitHubClient
) -> None:
    mock.get(api("/user")).respond(
        200,
        json={"login": "octo", "id": 42, "type": "User"},
        headers={"github-authentication-token-expiration": "2026-12-01 00:00:00 +0800"},
    )
    user = client.get_user()
    assert (user.login, user.id, user.token_kind, user.scopes) == ("octo", 42, "fine-grained", None)
    assert user.token_expires_at == datetime(2026, 12, 1, tzinfo=timezone(timedelta(hours=8)))


@pytest.mark.parametrize(
    ("header", "scopes"),
    [("repo, workflow, ", ["repo", "workflow"]), ("", []), ("admin:org", ["admin:org"])],
)
def test_get_user_classic_scopes(mock: respx.MockRouter, header: str, scopes: list[str]) -> None:
    mock.get(api("/user")).respond(
        200, json={"login": "octo", "id": 1}, headers={"X-OAuth-Scopes": header}
    )
    with GitHubClient(CLASSIC_PAT) as c:
        user = c.get_user()
    assert user.token_kind == "classic"
    assert user.scopes == scopes
    assert user.token_expires_at is None


def test_get_user_unknown_kind_and_bad_expiration(mock: respx.MockRouter) -> None:
    mock.get(api("/user")).respond(
        200,
        json={"login": "octo", "id": 1},
        headers={"github-authentication-token-expiration": "next tuesday"},
    )
    with GitHubClient(opaque()) as c:
        user = c.get_user()
    assert user.token_kind == "unknown"
    assert user.token_expires_at is None


@pytest.mark.parametrize("body", [[{"login": "octo"}], {"login": "octo"}])
def test_get_user_unexpected_payload(
    mock: respx.MockRouter, client: GitHubClient, body: Any
) -> None:
    mock.get(api("/user")).respond(200, json=body)
    with pytest.raises(GitHubError, match="格式不符合预期") as ei:
        client.get_user()
    assert type(ei.value) is GitHubError
    assert ei.value.status == 200


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-12-01 00:00:00 +0800", datetime(2026, 12, 1, tzinfo=timezone(timedelta(hours=8)))),
        ("2026-12-01 00:00:00 +08:00", datetime(2026, 12, 1, tzinfo=timezone(timedelta(hours=8)))),
        (
            "2026-12-01 05:30 -0130",
            datetime(2026, 12, 1, 5, 30, tzinfo=timezone(-timedelta(hours=1.5))),
        ),
        ("2026-12-01 00:00:00 UTC", datetime(2026, 12, 1, tzinfo=UTC)),
        ("2026-12-01T00:00:00Z", datetime(2026, 12, 1, tzinfo=UTC)),
        ("2026-12-01 00:00:00", datetime(2026, 12, 1, tzinfo=UTC)),
        ("  2026-12-01 00:00:00 +0000 ", datetime(2026, 12, 1, tzinfo=UTC)),
        ("garbage", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_token_expiration(value: str | None, expected: datetime | None) -> None:
    parsed = gc.parse_token_expiration(value)
    assert parsed == expected
    if parsed is not None:
        assert parsed.tzinfo is not None


# --- repos / pagination ---------------------------------------------------------------------


def test_list_repos_follows_link_pagination(mock: respx.MockRouter, client: GitHubClient) -> None:
    next_url = api("/user/repos?affiliation=owner&per_page=100&sort=full_name&page=2")
    route = mock.get(api("/user/repos")).mock(
        side_effect=[
            httpx.Response(
                200,
                json=[repo_json("a"), repo_json("b", private=False)],
                headers={"Link": f'<{next_url}>; rel="next", <{next_url}>; rel="last"'},
            ),
            httpx.Response(200, json=[repo_json("c")]),
        ]
    )
    repos = client.list_repos()
    assert [r.full_name for r in repos] == ["octo/a", "octo/b", "octo/c"]
    assert [r.private for r in repos] == [True, False, True]
    assert repos[0].owner == "octo"
    first, second = (call.request.url.params for call in route.calls)
    assert dict(first) == {"affiliation": "owner", "per_page": "100", "sort": "full_name"}
    assert second["page"] == "2"


def test_pagination_accepts_relative_next_link(
    mock: respx.MockRouter, client: GitHubClient
) -> None:
    route = mock.get(api("/user/repos")).mock(
        side_effect=[
            httpx.Response(
                200, json=[repo_json("a")], headers={"Link": '</user/repos?page=2>; rel="next"'}
            ),
            httpx.Response(200, json=[]),
        ]
    )
    assert [r.name for r in client.list_repos()] == ["a"]
    assert route.calls[1].request.url.params["page"] == "2"


def test_pagination_refuses_other_host(mock: respx.MockRouter, client: GitHubClient) -> None:
    mock.get(api("/user/repos")).respond(
        200,
        json=[repo_json("a")],
        headers={"Link": '<https://evil.example.com/user/repos?page=2>; rel="next"'},
    )
    evil = mock.get(url__startswith="https://evil.example.com/").respond(200, json=[])
    with pytest.raises(GitHubError, match="其他主机"):
        client.list_repos()
    assert evil.call_count == 0


def test_pagination_stops_after_max_pages(
    mock: respx.MockRouter, client: GitHubClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gc, "MAX_PAGES", 3)
    route = mock.get(api("/user/repos")).respond(
        200, json=[repo_json("a")], headers={"Link": f'<{api("/user/repos?page=2")}>; rel="next"'}
    )
    with pytest.raises(GitHubError, match="超过 3 页"):
        client.list_repos()
    assert route.call_count == 3


@pytest.mark.parametrize("body", [{"repos": []}, [{"name": "missing-fields"}]])
def test_list_repos_bad_payload(mock: respx.MockRouter, client: GitHubClient, body: Any) -> None:
    mock.get(api("/user/repos")).respond(200, json=body)
    with pytest.raises(GitHubError, match="格式不符合预期"):
        client.list_repos()


def test_get_repo(mock: respx.MockRouter, client: GitHubClient) -> None:
    mock.get(api(REPO_PATH)).respond(200, json=repo_json(private=False))
    repo = client.get_repo("octo", "demo")
    assert (repo.full_name, repo.private, repo.html_url) == (
        "octo/demo",
        False,
        "https://github.com/octo/demo",
    )


@pytest.mark.parametrize(("owner", "repo"), [("..", "x"), ("octo", "a/b"), ("", "x"), ("o", "..")])
def test_invalid_owner_repo_never_hits_network(
    mock: respx.MockRouter, client: GitHubClient, owner: str, repo: str
) -> None:
    with pytest.raises(InvalidInputError):
        client.get_repo(owner, repo)
    with pytest.raises(InvalidInputError):
        client.list_runners(owner, repo)
    assert not mock.calls


def test_non_json_success_body(mock: respx.MockRouter, client: GitHubClient) -> None:
    mock.get(api(REPO_PATH)).respond(200, text="<html>captive portal</html>")
    with pytest.raises(GitHubError, match="不是 JSON"):
        client.get_repo("octo", "demo")


# --- tokens -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method_name", "suffix"),
    [
        ("create_registration_token", "registration-token"),
        ("create_remove_token", "remove-token"),
    ],
)
def test_runner_tokens(
    mock: respx.MockRouter, client: GitHubClient, method_name: str, suffix: str
) -> None:
    token = opaque("AFAKETOKEN")
    route = mock.post(api(f"{RUNNERS_PATH}/{suffix}")).respond(
        201, json={"token": token, "expires_at": "2026-10-22T17:09:12.123+08:00"}
    )
    result = getattr(client, method_name)("octo", "demo")
    assert route.call_count == 1
    assert result.token.get_secret_value() == token
    assert result.expires_at.utcoffset() == timedelta(hours=8)
    assert token not in repr(result)
    assert redact(f"--token {token}") == "--token ***"
    assert redact(f"value {token}") == "value ***"


def test_malformed_token_response_does_not_leak_token(
    mock: respx.MockRouter, client: GitHubClient
) -> None:
    token = opaque("AFAKETOKEN")
    mock.post(api(f"{RUNNERS_PATH}/registration-token")).respond(
        201, json={"token": token, "expires_at": "not-a-date"}
    )
    with pytest.raises(GitHubError, match="格式不符合预期") as ei:
        client.create_registration_token("octo", "demo")
    assert ei.value.__cause__ is None
    assert ei.value.__suppress_context__
    assert token not in str(ei.value)
    assert redact(token) == "***"


# --- runners ------------------------------------------------------------------------------------


def test_list_runners_paginates_runners_key(mock: respx.MockRouter, client: GitHubClient) -> None:
    route = mock.get(api(RUNNERS_PATH)).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"total_count": 2, "runners": [runner_json(1, "a")]},
                headers={"Link": f'<{api(RUNNERS_PATH)}?per_page=100&page=2>; rel="next"'},
            ),
            httpx.Response(200, json={"total_count": 2, "runners": [runner_json(2, "b")]}),
        ]
    )
    runners = client.list_runners("octo", "demo")
    assert [(r.id, r.name) for r in runners] == [(1, "a"), (2, "b")]
    assert runners[0].labels == ["self-hosted", "gpu"]
    assert route.calls[0].request.url.params["per_page"] == "100"


def test_list_runners_empty_and_malformed(mock: respx.MockRouter, client: GitHubClient) -> None:
    route = mock.get(api(RUNNERS_PATH))
    route.respond(200, json={"total_count": 0, "runners": []})
    assert client.list_runners("octo", "demo") == []
    route.respond(200, json={"total_count": 0})
    with pytest.raises(GitHubError, match="格式不符合预期"):
        client.list_runners("octo", "demo")
    route.respond(200, json=[runner_json()])
    with pytest.raises(GitHubError, match="格式不符合预期"):
        client.list_runners("octo", "demo")


def test_get_runner(mock: respx.MockRouter, client: GitHubClient) -> None:
    route = mock.get(api(f"{RUNNERS_PATH}/7"))
    route.respond(200, json=runner_json(7))
    runner = client.get_runner("octo", "demo", 7)
    assert runner is not None
    assert (runner.id, runner.status, runner.busy) == (7, "online", False)
    route.respond(404, json={"message": "Not Found"})
    assert client.get_runner("octo", "demo", 7) is None
    route.respond(500, json={"message": "boom"})
    with pytest.raises(GitHubError, match="暂时不可用"):
        client.get_runner("octo", "demo", 7)


def test_delete_runner(mock: respx.MockRouter, client: GitHubClient) -> None:
    route = mock.delete(api(f"{RUNNERS_PATH}/7"))
    route.respond(204)
    assert client.delete_runner("octo", "demo", 7) is True
    route.respond(404, json={"message": "Not Found"})
    assert client.delete_runner("octo", "demo", 7) is False
    route.respond(403, json={"message": "Resource not accessible by personal access token"})
    with pytest.raises(PermissionDeniedError):
        client.delete_runner("octo", "demo", 7)


@pytest.mark.parametrize("runner_id", [0, -1, True, "7", 1.5])
def test_invalid_runner_id(mock: respx.MockRouter, client: GitHubClient, runner_id: Any) -> None:
    with pytest.raises(InvalidInputError):
        client.get_runner("octo", "demo", runner_id)
    with pytest.raises(InvalidInputError):
        client.delete_runner("octo", "demo", runner_id)
    assert not mock.calls


# --- release ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "path"),
    [
        (None, "/repos/actions/runner/releases/latest"),
        ("2.338.0", "/repos/actions/runner/releases/tags/v2.338.0"),
        (" v2.337.0 ", "/repos/actions/runner/releases/tags/v2.337.0"),
    ],
)
def test_get_runner_release_returns_raw_json(
    mock: respx.MockRouter, client: GitHubClient, version: str | None, path: str
) -> None:
    body = {"tag_name": "v2.338.0", "body": "notes", "assets": [{"name": "x.zip"}]}
    mock.get(api(path)).respond(200, json=body)
    assert client.get_runner_release(version) == body


@pytest.mark.parametrize("version", ["2.338", "latest", "2.338.0; rm", "../2.3.4"])
def test_get_runner_release_rejects_bad_version(
    mock: respx.MockRouter, client: GitHubClient, version: str
) -> None:
    with pytest.raises(InvalidInputError):
        client.get_runner_release(version)
    assert not mock.calls


def test_get_runner_release_errors(mock: respx.MockRouter, client: GitHubClient) -> None:
    mock.get(api("/repos/actions/runner/releases/tags/v9.9.9")).respond(
        404, json={"message": "Not Found"}
    )
    with pytest.raises(NotFoundError, match="找不到 runner 版本 v9.9.9") as ei:
        client.get_runner_release("9.9.9")
    assert "仓库不存在" not in str(ei.value)
    mock.get(api("/repos/actions/runner/releases/latest")).respond(200, json=["not", "a", "dict"])
    with pytest.raises(GitHubError, match="格式不符合预期"):
        client.get_runner_release()


# --- error mapping ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "headers", "body", "exc_type", "fragments"),
    [
        (
            401,
            {},
            {"message": "Bad credentials"},
            AuthError,
            ["PAT 无效或已过期", "Bad credentials"],
        ),
        (
            403,
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1893456000"},
            {"message": "API rate limit exceeded for user ID 1."},
            RateLimitError,
            ["超限", "API rate limit exceeded"],
        ),
        (429, {"retry-after": "60"}, {}, RateLimitError, ["超限"]),
        (
            403,
            {},
            {"message": "You have exceeded a secondary rate limit."},
            RateLimitError,
            ["稍后重试"],
        ),
        (
            403,
            {"x-ratelimit-remaining": "4999"},
            {"message": "Resource not accessible by personal access token"},
            PermissionDeniedError,
            ["PAT 权限不足", "Administration: Read and write"],
        ),
        (404, {}, {"message": "Not Found"}, NotFoundError, ["仓库不存在或 PAT 没有访问权限"]),
        (
            422,
            {},
            {"message": "Validation Failed"},
            GitHubError,
            ["GitHub 请求失败", "Validation Failed"],
        ),
        (429, {}, {}, GitHubError, ["GitHub 请求失败"]),
        (502, {}, "<html>bad gateway</html>", GitHubError, ["暂时不可用"]),
    ],
)
def test_error_mapping(
    mock: respx.MockRouter,
    client: GitHubClient,
    status: int,
    headers: dict[str, str],
    body: Any,
    exc_type: type[GitHubError],
    fragments: list[str],
) -> None:
    kwargs: dict[str, Any] = {"json": body} if isinstance(body, dict) else {"text": body}
    mock.get(api(REPO_PATH)).respond(status, headers=headers, **kwargs)
    with pytest.raises(GitHubError) as ei:
        client.get_repo("octo", "demo")
    err = ei.value
    assert type(err) is exc_type
    assert (err.status, err.method, err.path) == (status, "GET", REPO_PATH)
    assert f"GET {REPO_PATH} -> {status}" in str(err)
    for fragment in fragments:
        assert fragment in str(err)


def test_rate_limit_reset_from_ratelimit_header(
    mock: respx.MockRouter, client: GitHubClient
) -> None:
    mock.get(api("/user")).respond(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1893456000"}
    )
    with pytest.raises(RateLimitError) as ei:
        client.get_user()
    assert ei.value.reset_at == datetime(2030, 1, 1, tzinfo=UTC)
    local = datetime(2030, 1, 1, tzinfo=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    assert local in str(ei.value)


def test_rate_limit_reset_from_retry_after_seconds(
    mock: respx.MockRouter, client: GitHubClient
) -> None:
    mock.get(api("/user")).respond(429, headers={"retry-after": "60"})
    before = datetime.now(UTC)
    with pytest.raises(RateLimitError) as ei:
        client.get_user()
    after = datetime.now(UTC)
    assert ei.value.reset_at is not None
    assert before + timedelta(seconds=60) <= ei.value.reset_at <= after + timedelta(seconds=60)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (
            {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"},
            datetime(2026, 10, 21, 7, 28, tzinfo=UTC),
        ),
        (
            {"retry-after": "soon", "x-ratelimit-reset": "1893456000"},
            datetime(2030, 1, 1, tzinfo=UTC),
        ),
        ({"retry-after": "soon"}, None),
        ({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9" * 30}, None),
    ],
)
def test_rate_limit_reset_variants(
    mock: respx.MockRouter, client: GitHubClient, headers: dict[str, str], expected: datetime | None
) -> None:
    mock.get(api("/user")).respond(403, headers=headers)
    with pytest.raises(RateLimitError) as ei:
        client.get_user()
    assert ei.value.reset_at == expected


@pytest.mark.parametrize(
    ("pat", "hint"),
    [
        (FINE_PAT, "fine-grained PAT 需要选中该仓库"),
        (CLASSIC_PAT, "classic PAT 需要勾选 repo 权限"),
        (
            "FAKE-opaque-hint-pat",
            "fine-grained PAT 需要 Administration: Read and write 权限；classic",
        ),
    ],
)
def test_permission_hint_by_token_kind(mock: respx.MockRouter, pat: str, hint: str) -> None:
    mock.post(api(f"{RUNNERS_PATH}/registration-token")).respond(
        403,
        json={"message": "Resource not accessible by personal access token"},
        headers={"x-accepted-github-permissions": "administration=write"},
    )
    with GitHubClient(pat) as c, pytest.raises(PermissionDeniedError) as ei:
        c.create_registration_token("octo", "demo")
    assert hint in str(ei.value)
    assert "administration=write" in str(ei.value)
    assert f"POST {RUNNERS_PATH}/registration-token -> 403" in str(ei.value)


def test_github_message_is_redacted_and_truncated(mock: respx.MockRouter) -> None:
    pat = opaque("FAKE-pat")
    mock.get(api("/user")).respond(401, json={"message": f"token {pat} is bad " + "x" * 1000})
    with GitHubClient(pat) as c, pytest.raises(AuthError) as ei:
        c.get_user()
    assert pat not in str(ei.value)
    assert "token *** is bad" in str(ei.value)
    assert len(str(ei.value)) < 500


@pytest.mark.parametrize(
    ("exc", "fragment"),
    [
        (httpx.ConnectError("refused"), "无法连接到 GitHub，"),
        (httpx.ConnectTimeout("slow"), "请求 GitHub 超时"),
        (httpx.ReadTimeout("slow"), "请求 GitHub 超时"),
        (httpx.ProxyError("407 Proxy Authentication Required"), "代理连接失败"),
        (httpx.RemoteProtocolError("peer closed"), "与 GitHub 通信时网络出错"),
        (httpx.ReadError(""), "ReadError"),
    ],
)
def test_transport_errors_become_network_error(
    mock: respx.MockRouter, client: GitHubClient, exc: Exception, fragment: str
) -> None:
    mock.get(api("/user")).mock(side_effect=exc)
    with pytest.raises(NetworkError) as ei:
        client.get_user()
    assert fragment in str(ei.value)
    assert (ei.value.status, ei.value.method, ei.value.path) == (None, "GET", "/user")
    assert ei.value.__cause__ is exc


def test_too_many_redirects_is_github_error(mock: respx.MockRouter, client: GitHubClient) -> None:
    mock.get(api("/user")).mock(side_effect=httpx.TooManyRedirects("loop"))
    with pytest.raises(GitHubError, match="GitHub 请求失败") as ei:
        client.get_user()
    assert not isinstance(ei.value, NetworkError)


def test_http_errors_are_not_retried(mock: respx.MockRouter, client: GitHubClient) -> None:
    route = mock.get(api("/user")).respond(503)
    with pytest.raises(GitHubError):
        client.get_user()
    assert route.call_count == 1


def test_logs_have_method_path_status_but_no_secrets(
    mock: respx.MockRouter, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # setup_logging() (other tests) turns propagation off on the package logger.
    monkeypatch.setattr(logging.getLogger("runnerdeck"), "propagate", True)
    caplog.set_level(logging.DEBUG, logger=gc.__name__)
    pat, token = opaque("FAKE-pat"), opaque("AFAKETOKEN")
    mock.post(api(f"{RUNNERS_PATH}/registration-token")).respond(
        201, json={"token": token, "expires_at": "2026-10-22T17:09:12+08:00"}
    )
    mock.get(api("/user")).respond(401, json={"message": "Bad credentials"})
    with GitHubClient(pat) as c:
        c.create_registration_token("octo", "demo")
        with pytest.raises(AuthError):
            c.get_user()
    text = caplog.text
    assert f"POST {RUNNERS_PATH}/registration-token -> 201" in text
    assert "GET /user -> 401" in text
    for secret in (pat, token, "Bearer", "Bad credentials"):
        assert secret not in text


# --- retries (real httpx transports, fake sockets) ---------------------------------------------


@pytest.fixture
def refused_connects(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    attempts: list[tuple[str, int]] = []

    def connect_tcp(self: SyncBackend, host: str, port: int, *args: Any, **kwargs: Any) -> Any:
        attempts.append((host, port))
        raise httpcore.ConnectError("connection refused")

    monkeypatch.setattr(SyncBackend, "connect_tcp", connect_tcp)
    monkeypatch.setattr(SyncBackend, "sleep", lambda self, seconds: None)
    return attempts


def test_direct_connect_errors_are_retried_twice(refused_connects: list[tuple[str, int]]) -> None:
    with GitHubClient(FINE_PAT) as c, pytest.raises(NetworkError, match="无法连接到 GitHub"):
        c.create_registration_token("octo", "demo")
    assert refused_connects == [("api.github.com", 443)] * 3


def test_env_proxy_still_applies_and_retry_is_not_forwarded_by_httpx(
    refused_connects: list[tuple[str, int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pins the httpx 0.28 / httpcore 1.0 limitation documented in the module docstring:
    # connections through a proxy get no retries. If this starts failing, update the docs.
    monkeypatch.setattr(gc, "_getproxies", lambda: {"https": "http://127.0.0.1:9"})
    with GitHubClient(FINE_PAT) as c, pytest.raises(NetworkError):
        c.get_user()
    assert refused_connects == [("127.0.0.1", 9)]


# --- proxies ------------------------------------------------------------------------------------


class StubServer:
    """Local TCP server: records each request line, answers with a canned HTTP response."""

    def __init__(self, reply: bytes) -> None:
        lines: list[str] = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                lines.append(self.rfile.readline(65536).decode("latin-1").rstrip("\r\n"))
                while self.rfile.readline(65536) not in (b"", b"\r\n", b"\n"):
                    pass
                self.wfile.write(reply)

        self.lines = lines
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def address(self) -> str:
        host, port = self._server.server_address[:2]
        return f"{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _reply(status: str, body: bytes = b"") -> bytes:
    head = f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n"
    if body:
        head += "Content-Type: application/json\r\n"
    return head.encode() + b"\r\n" + body


@pytest.fixture
def proxy_stub() -> Iterator[StubServer]:
    stub = StubServer(_reply("502 Bad Gateway"))
    yield stub
    stub.close()


@pytest.fixture
def origin_stub() -> Iterator[StubServer]:
    stub = StubServer(_reply("200 OK", json.dumps({"login": "octo", "id": 1}).encode()))
    yield stub
    stub.close()


@pytest.fixture
def real_env_proxies(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Undo the autouse pin: read proxies from (a cleaned) environment like production does."""
    monkeypatch.setattr(gc, "_getproxies", urllib.request.getproxies)
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    return monkeypatch


@pytest.mark.parametrize("scheme", ["http://", ""])
def test_explicit_proxy_tunnels_api_calls(proxy_stub: StubServer, scheme: str) -> None:
    proxy = f" {scheme}{proxy_stub.address} "
    with (
        GitHubClient(FINE_PAT, proxy=proxy) as c,
        pytest.raises(NetworkError, match="代理连接失败"),
    ):
        c.get_user()
    assert proxy_stub.lines == ["CONNECT api.github.com:443 HTTP/1.1"]


def test_env_https_proxy_is_honoured(proxy_stub: StubServer, real_env_proxies: Any) -> None:
    real_env_proxies.setenv("HTTPS_PROXY", f"http://{proxy_stub.address}")
    with GitHubClient(FINE_PAT) as c, pytest.raises(NetworkError, match="代理连接失败"):
        c.list_runners("octo", "demo")
    assert proxy_stub.lines == ["CONNECT api.github.com:443 HTTP/1.1"]


def test_env_no_proxy_bypasses_proxy(
    proxy_stub: StubServer, origin_stub: StubServer, real_env_proxies: Any
) -> None:
    real_env_proxies.setenv("HTTP_PROXY", f"http://{proxy_stub.address}")
    base_url = f"http://{origin_stub.address}"

    real_env_proxies.setenv("NO_PROXY", "localhost,127.0.0.1")
    with GitHubClient(FINE_PAT, base_url=base_url) as c:
        assert c.get_user().login == "octo"
    assert proxy_stub.lines == []
    assert origin_stub.lines == ["GET /user HTTP/1.1"]

    real_env_proxies.delenv("NO_PROXY")
    with GitHubClient(FINE_PAT, base_url=base_url) as c, pytest.raises(GitHubError) as ei:
        c.get_user()
    assert ei.value.status == 502
    assert proxy_stub.lines == [f"GET {base_url}/user HTTP/1.1"]


def test_download_uses_the_same_proxy(proxy_stub: StubServer, tmp_path: Path) -> None:
    dest = tmp_path / "runner.zip"
    proxy = f"http://{proxy_stub.address}"
    with (
        GitHubClient(FINE_PAT, proxy=proxy) as c,
        pytest.raises(NetworkError, match="代理连接失败"),
    ):
        c.download(DL_URL, dest)
    assert proxy_stub.lines == ["CONNECT github.com:443 HTTP/1.1"]
    assert not dest.exists()


@pytest.mark.parametrize(
    "proxy", ["ftp://127.0.0.1:21", "http://", "http://:7890", "http://host:notaport"]
)
def test_invalid_explicit_proxy(proxy: str) -> None:
    with pytest.raises(InvalidInputError, match="代理地址不合法"):
        GitHubClient(FINE_PAT, proxy=proxy)


@pytest.mark.skipif(find_spec("socksio") is not None, reason="socksio installed")
def test_socks_proxy_without_socksio_is_invalid_input() -> None:
    with pytest.raises(InvalidInputError, match="代理地址不合法"):
        GitHubClient(FINE_PAT, proxy="socks5://127.0.0.1:1080")


def test_invalid_env_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gc, "_getproxies", lambda: {"https": "ftp://127.0.0.1:21"})
    with pytest.raises(InvalidInputError, match="HTTPS_PROXY"):
        GitHubClient(FINE_PAT)


def test_blank_proxy_means_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gc, "_getproxies", lambda: {"https": "ftp://127.0.0.1:21"})
    with pytest.raises(InvalidInputError, match="HTTPS_PROXY"):
        GitHubClient(FINE_PAT, proxy="   ")


def test_proxy_password_is_registered() -> None:
    password = opaque("FAKEproxypw")
    GitHubClient(FINE_PAT, proxy=f"http://user:{password}@127.0.0.1:7890").close()
    assert redact(f"pw={password}") == "pw=***"


@pytest.mark.parametrize(
    ("proxies", "expected"),
    [
        ({}, {}),
        (
            {"https": "127.0.0.1:7890", "http": "http://p:3128", "all": "socks5://s:1080"},
            {
                "https://": "http://127.0.0.1:7890",
                "http://": "http://p:3128",
                "all://": "socks5://s:1080",
            },
        ),
        (
            {"https": "http://p:1", "no": "localhost, 127.0.0.1,::1,.example.com,http://x.y,,"},
            {
                "https://": "http://p:1",
                "all://localhost": None,
                "all://127.0.0.1": None,
                "all://[::1]": None,
                "all://*.example.com": None,
                "http://x.y": None,
            },
        ),
        ({"https": "http://p:1", "no": "example.com,*"}, {}),
        ({"no": "10.0.0.0/8"}, {"all://10.0.0.0/8": None}),
    ],
)
def test_env_proxy_map(proxies: dict[str, str], expected: dict[str, str | None]) -> None:
    assert env_proxy_map(proxies) == expected


def test_typical_no_proxy_builds_a_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gc,
        "_getproxies",
        lambda: {
            "https": "http://127.0.0.1:7890",
            "http": "http://127.0.0.1:7890",
            "no": "localhost,127.0.0.1,::1,192.168.0.0/16,.corp.example,*.local",
        },
    )
    GitHubClient(FINE_PAT).close()


# --- download -----------------------------------------------------------------------------------


def test_download_follows_redirect_without_authorization(
    mock: respx.MockRouter, client: GitHubClient, tmp_path: Path
) -> None:
    payload = os.urandom(200_000)
    mock.get(DL_URL).respond(302, headers={"Location": CDN_URL})
    cdn = mock.get(url__startswith="https://release-assets.githubusercontent.com/").respond(
        200, content=payload, headers={"Content-Type": "application/octet-stream"}
    )
    user_route = mock.get(api("/user")).respond(200, json={"login": "octo", "id": 1})
    progress: list[tuple[int, int | None]] = []
    dest = tmp_path / "nested" / "dir" / "runner.zip"

    digest = client.download(DL_URL, dest, on_progress=lambda d, t: progress.append((d, t)))
    client.get_user()

    assert digest == hashlib.sha256(payload).hexdigest()
    assert dest.read_bytes() == payload
    assert cdn.call_count == 1
    download_calls = [c for c in mock.calls if c.request.url.host != "api.github.com"]
    assert len(download_calls) == 2
    for call in download_calls:
        assert "authorization" not in call.request.headers
        assert call.request.headers["user-agent"].startswith("RunnerDeck/")
    assert user_route.calls.last.request.headers["authorization"] == f"Bearer {FINE_PAT}"
    assert progress[0] == (0, len(payload))
    assert progress[-1] == (len(payload), len(payload))
    assert [d for d, _ in progress] == sorted(d for d, _ in progress)
    assert len(progress) > 2  # streamed in chunks


def test_download_unknown_length(
    mock: respx.MockRouter, client: GitHubClient, tmp_path: Path
) -> None:
    mock.get(DL_URL).respond(200, content=iter([b"abc", b"def"]))
    progress: list[tuple[int, int | None]] = []
    digest = client.download(
        DL_URL, tmp_path / "a.zip", on_progress=lambda d, t: progress.append((d, t))
    )
    assert digest == hashlib.sha256(b"abcdef").hexdigest()
    assert progress[0] == (0, None)
    assert progress[-1] == (6, None)


def test_download_content_encoded_length_is_unknown(
    mock: respx.MockRouter, client: GitHubClient, tmp_path: Path
) -> None:
    import gzip

    raw = b"zip-bytes" * 100
    mock.get(DL_URL).respond(200, content=gzip.compress(raw), headers={"Content-Encoding": "gzip"})
    progress: list[tuple[int, int | None]] = []
    digest = client.download(
        DL_URL, tmp_path / "a.zip", on_progress=lambda d, t: progress.append((d, t))
    )
    assert digest == hashlib.sha256(raw).hexdigest()
    assert progress[-1] == (len(raw), None)


def test_download_http_error_deletes_dest(
    mock: respx.MockRouter, client: GitHubClient, tmp_path: Path
) -> None:
    dest = tmp_path / "runner.zip"
    dest.write_bytes(b"stale partial")
    mock.get(DL_URL).respond(302, headers={"Location": CDN_URL})
    mock.get(url__startswith="https://release-assets.githubusercontent.com/").respond(403)
    with pytest.raises(GitHubError) as ei:
        client.download(DL_URL, dest)
    assert type(ei.value) is GitHubError
    assert ei.value.status == 403
    assert "FAKESIGNATURE" not in str(ei.value)
    assert not dest.exists()


def test_download_midstream_failure_deletes_dest(
    mock: respx.MockRouter, client: GitHubClient, tmp_path: Path
) -> None:
    def body() -> Iterator[bytes]:
        yield b"first chunk"
        raise httpx.ReadError("connection reset")

    dest = tmp_path / "runner.zip"
    mock.get(DL_URL).respond(200, content=body(), headers={"Content-Length": "1000"})
    with pytest.raises(NetworkError, match="下载服务器"):
        client.download(DL_URL, dest)
    assert not dest.exists()


def test_download_truncated_body(
    mock: respx.MockRouter, client: GitHubClient, tmp_path: Path
) -> None:
    dest = tmp_path / "runner.zip"
    mock.get(DL_URL).respond(200, content=b"x" * 50, headers={"Content-Length": "100"})
    with pytest.raises(NetworkError, match="下载不完整"):
        client.download(DL_URL, dest)
    assert not dest.exists()


def test_download_transport_error(
    mock: respx.MockRouter, client: GitHubClient, tmp_path: Path
) -> None:
    mock.get(DL_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(NetworkError, match="请求下载服务器超时"):
        client.download(DL_URL, tmp_path / "a.zip")


def test_download_progress_callback_can_abort(
    mock: respx.MockRouter, client: GitHubClient, tmp_path: Path
) -> None:
    class Cancelled(Exception):
        pass

    def on_progress(done: int, total: int | None) -> None:
        if done > 0:
            raise Cancelled

    dest = tmp_path / "runner.zip"
    mock.get(DL_URL).respond(200, content=b"x" * 200_000)
    with pytest.raises(Cancelled):
        client.download(DL_URL, dest, on_progress=on_progress)
    assert not dest.exists()


def test_download_write_failure_is_not_a_network_error(
    mock: respx.MockRouter, client: GitHubClient, tmp_path: Path
) -> None:
    dest = tmp_path / "is-a-directory"
    dest.mkdir()
    mock.get(DL_URL).respond(200, content=b"data")
    with pytest.raises(RunnerDeckError, match="无法写入下载文件") as ei:
        client.download(DL_URL, dest)
    assert not isinstance(ei.value, GitHubError)
    assert dest.is_dir()  # never removes a directory


def test_download_parent_not_creatable(client: GitHubClient, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(RunnerDeckError, match="无法创建下载目录"):
        client.download(DL_URL, blocker / "runner.zip")


@pytest.mark.parametrize(
    "url", ["http://github.com/a.zip", "ftp://github.com/a.zip", "not a url", "https://", "//x"]
)
def test_download_requires_https(
    mock: respx.MockRouter, client: GitHubClient, url: str, tmp_path: Path
) -> None:
    with pytest.raises(InvalidInputError, match="下载地址"):
        client.download(url, tmp_path / "a.zip")
    assert not mock.calls
