"""GitHub REST client: the only module that talks to the GitHub API (CLAUDE.md).

Proxy and retry wiring, checked against the httpx 0.28.1 / httpcore 1.0.9 sources:

- httpx reads HTTP(S)_PROXY / ALL_PROXY / NO_PROXY (and, via urllib.request.getproxies(), the
  Windows registry or macOS system proxy) only when no `transport=` is passed:
  `allow_env_proxies = trust_env and transport is None` in `Client.__init__`.
- Connection retries are an `HTTPTransport(retries=...)` option, so passing
  `transport=HTTPTransport(retries=2)` would silently switch the environment proxy off. Instead the
  proxy mounts are rebuilt here with httpx's own rules (`env_proxy_map`) and passed as `mounts=`,
  each one an `HTTPTransport(proxy=..., retries=2)`, next to a retrying direct transport.
- `HTTPTransport` forwards `retries` only to its direct connection pool, and httpcore's `HTTPProxy`
  does not pass it on to tunnel connections either: connections made through a proxy are
  currently not retried. Only connection setup (ConnectError / ConnectTimeout) is ever retried,
  never a request that got an HTTP status.
- respx patches httpcore's `ConnectionPool` / `HTTPProxy.handle_request`, so it intercepts requests
  through these transports (direct or proxied) without a transport being injected.
- A SOCKS entry httpx cannot use (socks5 without socksio, or the socks4:// that urllib derives from
  a SOCKS-only Windows system proxy) is skipped with a warning when it does not carry https://
  traffic; only when it does is the client refused (InvalidInputError naming the variable).

TLS trust: httpx's default `verify=True` trusts only the certifi bundle (or SSL_CERT_FILE /
SSL_CERT_DIR), never the OS store, so TLS-inspecting proxies and local GitHub accelerators whose
root CA sits in the Windows certificate store would fail every call. Each client therefore uses a
`truststore.SSLContext` (the OS store, plus SSL_CERT_FILE / SSL_CERT_DIR on top), shared by the
direct transport, every proxy mount (also for TLS to an https:// proxy) and the download client.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import logging
import os
import re
import ssl
import time
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self, TypeVar
from urllib.parse import quote

import httpx
import truststore
from pydantic import BaseModel, ValidationError

from runnerdeck import __version__
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
from runnerdeck.core.models import (
    DownloadSample,
    GitHubUser,
    RemoteRunner,
    Repo,
    RunnerToken,
    WorkflowJob,
    WorkflowRun,
    validate_owner_repo,
)
from runnerdeck.core.redact import redact, register_secret

log = logging.getLogger(__name__)

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = f"RunnerDeck/{__version__}"
CONNECT_RETRIES = 2
PER_PAGE = 100
MAX_PAGES = 50  # 5000 items; a longer Link chain is treated as broken
DOWNLOAD_CHUNK = 64 * 1024
JOB_LOG_MAX_BYTES = 8 * 1024 * 1024  # a job log beyond this keeps its end

_RUNNER_RELEASES = "/repos/actions/runner/releases"
_VERSION_RE = re.compile(r"v?(\d+\.\d+\.\d+)")
_MESSAGE_MAX = 300
_BAD_PROXY = "代理地址不合法：只支持 http:// 或 https:// 代理，例如 http://127.0.0.1:7890"
_BAD_ENV_PROXY = (
    "系统代理设置不可用（HTTPS_PROXY / HTTP_PROXY / ALL_PROXY / NO_PROXY），"
    "请修正，或在设置里填写代理地址"
)
# env_proxy_map() pattern -> where the value usually comes from (named in messages and logs).
_ENV_PROXY_VARS = {"https://": "HTTPS_PROXY", "http://": "HTTP_PROXY", "all://": "ALL_PROXY"}
_EXPIRATION_FORMATS = ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M %z")

ProgressFn = Callable[[int, int | None], None]
TokenKind = Literal["classic", "fine-grained", "unknown"]
_M = TypeVar("_M", bound=BaseModel)


# --- proxies -------------------------------------------------------------------------


def _getproxies() -> dict[str, str]:
    """Indirection so tests can pin the proxy environment."""
    return urllib.request.getproxies()


def _is_ip(host: str, version: Literal[4, 6]) -> bool:
    address = host.split("/")[0]
    try:
        if version == 4:
            ipaddress.IPv4Address(address)
        else:
            ipaddress.IPv6Address(address)
    except ValueError:
        return False
    return True


def env_proxy_map(proxies: Mapping[str, str] | None = None) -> dict[str, str | None]:
    """URL pattern -> proxy URL (None = connect directly), same rules as httpx's trust_env.

    `proxies` defaults to urllib.request.getproxies(): environment variables first, then the
    Windows registry / macOS system settings. Mirrors httpx._utils.get_environment_proxies,
    which is private.
    """
    info = _getproxies() if proxies is None else proxies
    mounts: dict[str, str | None] = {}
    for scheme in ("http", "https", "all"):
        url = (info.get(scheme) or "").strip()
        if url:
            mounts[f"{scheme}://"] = url if "://" in url else f"http://{url}"
    for host in (h.strip() for h in (info.get("no") or "").split(",")):
        if not host:
            continue
        if host == "*":
            return {}
        if "://" in host:
            mounts[host] = None
        elif _is_ip(host, 4):
            mounts[f"all://{host}"] = None
        elif _is_ip(host, 6):
            mounts[f"all://[{host}]"] = None
        elif host.lower() == "localhost":
            mounts[f"all://{host}"] = None
        else:
            mounts[f"all://*{host}"] = None
    return mounts


def _normalize_proxy(proxy: str | None) -> str | None:
    """'' -> None; '127.0.0.1:7890' -> 'http://127.0.0.1:7890' (as httpx does for env values)."""
    if proxy is None:
        return None
    value = proxy.strip()
    if not value:
        return None
    return value if "://" in value else f"http://{value}"


def _ssl_context() -> ssl.SSLContext:
    """TLS trust for one client: the OS store via truststore, plus SSL_CERT_FILE / SSL_CERT_DIR.

    httpx's default context uses those variables in place of certifi; here they are added on top
    of the OS store, so a CA bundle exported as a workaround keeps working. One that cannot be
    loaded is skipped with a warning (httpx would have failed to build the client at all).
    """
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    for var, kind in (("SSL_CERT_FILE", "cafile"), ("SSL_CERT_DIR", "capath")):
        location = os.environ.get(var, "").strip()
        if not location:
            continue
        try:
            if kind == "cafile":
                ctx.load_verify_locations(cafile=location)
            else:
                ctx.load_verify_locations(capath=location)
        except (OSError, ValueError) as e:  # ssl.SSLError is an OSError
            log.warning("ignoring %s: cannot load CA certificates (%s)", var, type(e).__name__)
    return ctx


def _retrying_transport(
    ssl_context: ssl.SSLContext, proxy: str | None = None
) -> httpx.HTTPTransport:
    proxy_config: httpx.Proxy | str | None = proxy
    if proxy is not None and httpx.URL(proxy).scheme == "https":
        # TLS to the proxy itself; httpcore rejects a proxy ssl_context for http:// proxies.
        proxy_config = httpx.Proxy(proxy, ssl_context=ssl_context)
    return httpx.HTTPTransport(verify=ssl_context, proxy=proxy_config, retries=CONNECT_RETRIES)


def _env_mounts(ssl_context: ssl.SSLContext) -> dict[str, httpx.BaseTransport | None]:
    """Mounts for the environment / system proxy (see env_proxy_map).

    An unusable SOCKS entry is dropped with a warning unless it is the one https:// requests
    would use (the "https://" entry, else "all://"); then the client cannot be built.
    """
    proxies = env_proxy_map()
    https_carrier = "https://" if "https://" in proxies else "all://"
    mounts: dict[str, httpx.BaseTransport | None] = {}
    for pattern, url in proxies.items():
        if url is None:
            mounts[pattern] = None
            continue
        try:
            mounts[pattern] = _retrying_transport(ssl_context, url)
        except (httpx.InvalidURL, ValueError, ImportError) as e:
            scheme = url.split("://", 1)[0].strip().lower()
            if not scheme.startswith("socks"):
                raise
            var = _ENV_PROXY_VARS.get(pattern, pattern)
            if pattern != https_carrier:
                log.warning(
                    "ignoring unsupported %s:// proxy from %s (or the system proxy); "
                    "https requests do not use it",
                    scheme,
                    var,
                )
                continue
            raise InvalidInputError(
                f"系统代理 {var}（或系统代理设置）是 {scheme}:// 代理，"
                "RunnerDeck 不支持 SOCKS 代理；请改用 http:// 代理，或在设置里填写代理地址"
            ) from e
    return mounts


class _SharedTransport(httpx.BaseTransport):
    """Lets a short-lived download client use an injected transport without closing it."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._inner.handle_request(request)


# --- response helpers ------------------------------------------------------------------


def _needs(e: PermissionDeniedError, permission: str) -> PermissionDeniedError:
    return PermissionDeniedError(
        f"PAT 缺少 {permission} 权限：fine-grained PAT 请在 token 设置里为该仓库加上它；"
        "classic PAT 勾选 repo 即可",
        status=e.status,
        method=e.method,
        path=e.path,
    )


def parse_token_expiration(value: str | None) -> datetime | None:
    """Parse `github-authentication-token-expiration` leniently; None if absent or unparsable.

    Seen formats: "2026-12-01 00:00:00 +0800", "2026-12-01 00:00:00 UTC". Naive -> UTC.
    """
    if not value or not value.strip():
        return None
    text = value.strip()
    for fmt in _EXPIRATION_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    if text.upper().endswith(" UTC"):
        text = text[:-4].strip() + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        log.debug("unparsable token expiration header")
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _github_message(resp: httpx.Response) -> str | None:
    try:
        data = resp.json()
    except ValueError:
        return None
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, str) or not message.strip():
        return None
    return redact(message.strip()[:_MESSAGE_MAX])


def _is_rate_limited(resp: httpx.Response, message: str | None) -> bool:
    if resp.status_code not in (403, 429):
        return False
    headers = resp.headers
    return (
        headers.get("x-ratelimit-remaining", "").strip() == "0"
        or "retry-after" in headers
        # Secondary limits may come without retry-after; GitHub says so in the message.
        or "rate limit" in (message or "").lower()
    )


def _rate_limit_reset(headers: httpx.Headers, now: datetime) -> datetime | None:
    retry_after = headers.get("retry-after", "").strip()
    if retry_after.isdigit():
        return now + timedelta(seconds=int(retry_after))
    if retry_after:
        try:
            when = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError):
            when = None
        if when is not None:
            return when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    reset = headers.get("x-ratelimit-reset", "").strip()
    if reset.isdigit():
        try:
            return datetime.fromtimestamp(int(reset), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _network_error(e: httpx.HTTPError, method: str, target: str, where: str) -> NetworkError:
    """`where` is spliced into Chinese text: " GitHub " (spaced, ASCII) or "下载服务器"."""
    if isinstance(e, httpx.ProxyError):
        what = "代理连接失败，请检查代理设置"
    elif isinstance(e, httpx.TimeoutException):
        what = f"请求{where}超时，请检查网络或代理设置"
    elif isinstance(e, httpx.ConnectError):
        what = f"无法连接到{where.rstrip()}，请检查网络或代理设置"
    else:
        what = f"与{where}通信时网络出错"
    detail = redact(str(e)).strip() or type(e).__name__
    return NetworkError(f"{what}（{method} {target}：{detail}）", method=method, path=target)


def _content_length(resp: httpx.Response) -> int | None:
    """Expected body size, or None when unknown (or when the body is content-encoded)."""
    if resp.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
        return None
    raw = resp.headers.get("content-length", "").strip()
    return int(raw) if raw.isdigit() else None


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # a directory, or locked on Windows: leave it, the original error matters
        log.warning("could not remove partial download %s", path)


# --- client ------------------------------------------------------------------------------


class GitHubClient:
    """Synchronous GitHub REST client. Not thread-safe: use one instance per worker thread."""

    def __init__(
        self,
        pat: str | None,
        *,
        proxy: str | None = None,
        base_url: str = API_URL,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """`proxy` None -> environment / system proxy; otherwise all traffic goes through it.

        `transport` is for tests only: it replaces the network (proxy settings are then ignored).
        Raises InvalidInputError for an empty / malformed PAT or an unusable proxy URL.
        pat=None: anonymous, no Authorization header; public endpoints only (the app's own
        update check must not send the PAT anywhere it is not needed).
        """
        if pat is not None:
            register_secret(pat)
            pat = pat.strip()
            register_secret(pat)
            if not pat or not pat.isascii() or not pat.isprintable() or " " in pat:
                raise InvalidInputError("PAT 格式不正确：请粘贴 token 的值（不要包含空格或换行）")
        self._pat = pat or ""
        self._proxy = _normalize_proxy(proxy)
        if self._proxy is not None:
            try:
                proxy_url = httpx.URL(self._proxy)
            except (httpx.InvalidURL, ValueError) as e:
                raise InvalidInputError(_BAD_PROXY) from e
            register_secret(proxy_url.password)
            if not proxy_url.host:  # httpx accepts "http://" and only fails per request
                raise InvalidInputError(_BAD_PROXY)
        self._timeout = httpx.Timeout(timeout)
        self._transport = transport
        # Shared by the API client and every download client (unused with an injected transport).
        self._ssl_context = _ssl_context()
        self._http = self._new_client(
            base_url=base_url,
            headers={
                **({"Authorization": f"Bearer {pat}"} if pat else {}),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
            # Renamed repos answer 301/307; httpx drops Authorization on cross-origin redirects.
            follow_redirects=True,
        )

    # --- lifecycle

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- API

    def get_user(self) -> GitHubUser:
        """GET /user plus what the response headers say about the token itself."""
        resp = self._request("GET", "/user")
        data = self._json(resp, "GET", "/user")
        if not isinstance(data, dict):
            raise self._bad_payload(resp, "GET", "/user")
        scopes_header = resp.headers.get("x-oauth-scopes")
        kind: TokenKind
        scopes: list[str] | None = None
        if scopes_header is not None:
            kind = "classic"
            scopes = [s.strip() for s in scopes_header.split(",") if s.strip()]
        elif self._pat.startswith("github_pat_"):
            kind = "fine-grained"
        else:
            kind = "unknown"
        expires_at = parse_token_expiration(
            resp.headers.get("github-authentication-token-expiration")
        )
        payload = {
            "login": data.get("login"),
            "id": data.get("id"),
            "token_kind": kind,
            "scopes": scopes,
            "token_expires_at": expires_at,
        }
        return self._model(GitHubUser, payload, resp, "GET", "/user")

    def list_repos(self) -> list[Repo]:
        """Repos owned by the PAT's user, all pages."""
        params = {"affiliation": "owner", "per_page": PER_PAGE, "sort": "full_name"}
        path = "/user/repos"
        items = self._paginate(path, params)
        return [self._model(Repo, item, None, "GET", path) for item in items]

    def get_repo(self, owner: str, repo: str) -> Repo:
        path = self._repo_path(owner, repo)
        resp = self._request("GET", path)
        return self._model(Repo, self._json(resp, "GET", path), resp, "GET", path)

    def create_registration_token(self, owner: str, repo: str) -> RunnerToken:
        return self._runner_token(
            self._repo_path(owner, repo) + "/actions/runners/registration-token"
        )

    def create_remove_token(self, owner: str, repo: str) -> RunnerToken:
        return self._runner_token(self._repo_path(owner, repo) + "/actions/runners/remove-token")

    def list_runners(self, owner: str, repo: str) -> list[RemoteRunner]:
        path = self._repo_path(owner, repo) + "/actions/runners"
        items = self._paginate(path, {"per_page": PER_PAGE}, key="runners")
        return [self._model(RemoteRunner, item, None, "GET", path) for item in items]

    def get_runner(self, owner: str, repo: str, runner_id: int) -> RemoteRunner | None:
        """None when GitHub answers 404 (runner deleted, or repo not visible)."""
        path = self._runner_path(owner, repo, runner_id)
        resp = self._request("GET", path, ok_statuses=frozenset({404}))
        if resp.status_code == 404:
            return None
        return self._model(RemoteRunner, self._json(resp, "GET", path), resp, "GET", path)

    def delete_runner(self, owner: str, repo: str, runner_id: int) -> bool:
        """True when deleted (204), False when GitHub answers 404 (already gone)."""
        path = self._runner_path(owner, repo, runner_id)
        resp = self._request("DELETE", path, ok_statuses=frozenset({404}))
        return resp.status_code != 404

    def get_runner_release(self, version: str | None = None) -> dict[str, Any]:
        """Raw release JSON of actions/runner (latest, or tag v{version}); parsed in release.py."""
        if version is None:
            path = f"{_RUNNER_RELEASES}/latest"
            not_found = "找不到 actions/runner 的最新版本"
        else:
            match = _VERSION_RE.fullmatch(version.strip())
            if match is None:
                raise InvalidInputError(f"runner 版本号不合法：{version!r}（应为 x.y.z）")
            path = f"{_RUNNER_RELEASES}/tags/v{match.group(1)}"
            not_found = f"找不到 runner 版本 v{match.group(1)}"
        resp = self._request("GET", path, not_found=not_found)
        data = self._json(resp, "GET", path)
        if not isinstance(data, dict):
            raise self._bad_payload(resp, "GET", path)
        return data

    # --- M4: workflows, runs, releases (docs/m4-design.md §4, §8)

    def list_workflow_files(self, owner: str, repo: str) -> list[str]:
        """Paths of `.github/workflows/*.yml|yaml`; [] when the directory does not exist.
        403 -> PermissionDeniedError naming Contents: Read."""
        path = self._repo_path(owner, repo) + "/contents/.github/workflows"
        try:
            resp = self._request("GET", path, ok_statuses=frozenset({404}))
        except PermissionDeniedError as e:
            raise _needs(e, "Contents: Read（读取 workflow 文件）") from e
        if resp.status_code == 404:
            return []
        data = self._json(resp, "GET", path)
        if not isinstance(data, list):
            raise self._bad_payload(resp, "GET", path)
        return sorted(
            str(item["path"])
            for item in data
            if isinstance(item, dict)
            and item.get("type") == "file"
            and str(item.get("name", "")).lower().endswith((".yml", ".yaml"))
            and isinstance(item.get("path"), str)
        )

    def get_text_file(self, owner: str, repo: str, file_path: str) -> str | None:
        """A repository file's text (contents API, base64); None for a missing file, a
        directory or anything over 1 MB (the API then omits the content)."""
        if ".." in file_path.split("/") or file_path.startswith("/"):
            raise InvalidInputError(f"文件路径不合法：{file_path!r}")
        path = self._repo_path(owner, repo) + "/contents/" + quote(file_path)
        try:
            resp = self._request("GET", path, ok_statuses=frozenset({404}))
        except PermissionDeniedError as e:
            raise _needs(e, "Contents: Read（读取 workflow 文件）") from e
        if resp.status_code == 404:
            return None
        data = self._json(resp, "GET", path)
        if not isinstance(data, dict) or data.get("type") != "file":
            return None
        content = data.get("content")
        if data.get("encoding") != "base64" or not isinstance(content, str) or not content:
            return None
        try:
            return base64.b64decode(content).decode("utf-8-sig", errors="replace")
        except (ValueError, binascii.Error):
            return None

    def list_runs(self, owner: str, repo: str, per_page: int = 10) -> list[WorkflowRun]:
        """The newest workflow runs of the repo. 403 -> PermissionDeniedError (Actions: Read)."""
        path = self._repo_path(owner, repo) + "/actions/runs"
        try:
            resp = self._request("GET", path, params={"per_page": per_page})
        except PermissionDeniedError as e:
            raise _needs(e, "Actions: Read（读取运行记录）") from e
        data = self._json(resp, "GET", path)
        runs = data.get("workflow_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list):
            raise self._bad_payload(resp, "GET", path)
        return [self._model(WorkflowRun, item, resp, "GET", path) for item in runs]

    def list_run_jobs(self, owner: str, repo: str, run_id: int) -> list[WorkflowJob]:
        """The jobs of one workflow run (latest attempt). 403 -> PermissionDeniedError."""
        path = self._repo_path(owner, repo) + f"/actions/runs/{run_id}/jobs"
        try:
            resp = self._request("GET", path, params={"per_page": 100})
        except PermissionDeniedError as e:
            raise _needs(e, "Actions: Read（读取 job）") from e
        data = self._json(resp, "GET", path)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise self._bad_payload(resp, "GET", path)
        return [self._model(WorkflowJob, item, resp, "GET", path) for item in jobs]

    def get_job(self, owner: str, repo: str, job_id: int) -> WorkflowJob:
        """One job with its steps (they update while it runs). 403 -> PermissionDeniedError."""
        path = self._repo_path(owner, repo) + f"/actions/jobs/{job_id}"
        try:
            resp = self._request("GET", path, not_found=f"job {job_id} 不存在")
        except PermissionDeniedError as e:
            raise _needs(e, "Actions: Read（读取 job）") from e
        return self._model(WorkflowJob, self._json(resp, "GET", path), resp, "GET", path)

    def download_job_log(self, owner: str, repo: str, job_id: int) -> str | None:
        """A completed job's plain-text log (the redirect to the log store is followed without
        Authorization). None when GitHub has no log for it (yet): while a job runs the
        redirect leads to a 404 (spike-notes §13). Longer logs keep their last
        JOB_LOG_MAX_BYTES."""
        path = self._repo_path(owner, repo) + f"/actions/jobs/{job_id}/logs"
        try:
            resp = self._request("GET", path, ok_statuses=frozenset({404}))
        except PermissionDeniedError as e:
            raise _needs(e, "Actions: Read（读取 job 日志）") from e
        if resp.status_code == 404:
            return None
        content = resp.content
        if len(content) > JOB_LOG_MAX_BYTES:
            content = content[-JOB_LOG_MAX_BYTES:]
        return content.decode("utf-8-sig", errors="replace")

    def get_latest_release(self, owner: str, repo: str) -> dict[str, Any] | None:
        """Raw JSON of the repo's latest (non pre-release) release; None when it has none."""
        path = self._repo_path(owner, repo) + "/releases/latest"
        resp = self._request("GET", path, ok_statuses=frozenset({404}))
        if resp.status_code == 404:
            return None
        data = self._json(resp, "GET", path)
        if not isinstance(data, dict):
            raise self._bad_payload(resp, "GET", path)
        return data

    def download(self, url: str, dest: Path, on_progress: ProgressFn | None = None) -> str:
        """Stream `url` into `dest` and return its sha256 (lowercase hex).

        Uses a separate client without Authorization (same proxy settings), following
        redirects. `on_progress(done_bytes, total_bytes_or_None)` runs in the calling thread,
        once with 0 before the first chunk; an exception it raises aborts the download and
        propagates unchanged. On any failure `dest` is deleted. HTTP error status ->
        GitHubError, transport failure or truncated body -> NetworkError, local write
        failure -> RunnerDeckError.
        """
        try:
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, ValueError) as e:
            raise InvalidInputError("下载地址不合法") from e
        if parsed.scheme != "https" or not parsed.host:
            raise InvalidInputError(f"下载地址必须是 https 链接（{parsed.scheme}://{parsed.host}）")
        target = f"{parsed.host}{parsed.path}"  # never the query: signed URLs carry credentials
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RunnerDeckError(f"无法创建下载目录 {dest.parent}：{e.strerror or e}") from e
        client = self._new_client(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, for_download=True
        )
        digest = hashlib.sha256()
        done = 0
        started = time.monotonic()
        try:
            with client, client.stream("GET", parsed) as resp:
                if resp.is_error:
                    raise GitHubError(
                        f"下载失败（GET {target} -> {resp.status_code}）",
                        status=resp.status_code,
                        method="GET",
                        path=target,
                    )
                total = _content_length(resp)
                with dest.open("wb") as fh:
                    if on_progress is not None:
                        on_progress(0, total)
                    for chunk in resp.iter_bytes(DOWNLOAD_CHUNK):
                        fh.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        if on_progress is not None:
                            on_progress(done, total)
                if total is not None and done != total:
                    raise NetworkError(
                        f"下载不完整：收到 {done} 字节，应为 {total} 字节（GET {target}）",
                        method="GET",
                        path=target,
                    )
        except httpx.HTTPError as e:
            _discard(dest)
            log.warning("GET %s failed after %.2fs: %s", target, _since(started), type(e).__name__)
            raise _network_error(e, "GET", target, "下载服务器") from e
        except OSError as e:
            _discard(dest)
            raise RunnerDeckError(f"无法写入下载文件 {dest}：{e.strerror or e}") from e
        except BaseException:
            _discard(dest)
            raise
        log.info("GET %s -> %d bytes (%.2fs)", target, done, _since(started))
        return digest.hexdigest()

    def measure_download(self, url: str, *, max_bytes: int, max_seconds: float) -> DownloadSample:
        """A download-speed sample: GET `url` like download() (no Authorization, same proxy
        settings, redirects followed) and stop after `max_bytes` or `max_seconds` of body.
        The clock starts when the response headers arrive, so connection setup and a slow
        redirect do not count. HTTP error status -> GitHubError, transport failure ->
        NetworkError."""
        try:
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, ValueError) as e:
            raise InvalidInputError("测速地址不合法") from e
        if parsed.scheme not in ("http", "https") or not parsed.host:
            raise InvalidInputError(
                f"测速地址必须是 http(s) 链接（{parsed.scheme}://{parsed.host}）"
            )
        target = f"{parsed.host}{parsed.path}"
        client = self._new_client(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, for_download=True
        )
        received = 0
        seconds = 0.0
        try:
            with client, client.stream("GET", parsed) as resp:
                if resp.is_error:
                    raise GitHubError(
                        f"测速失败（GET {target} -> {resp.status_code}）",
                        status=resp.status_code,
                        method="GET",
                        path=target,
                    )
                started = time.monotonic()
                for chunk in resp.iter_raw(DOWNLOAD_CHUNK):
                    received += len(chunk)
                    seconds = time.monotonic() - started
                    if received >= max_bytes or seconds >= max_seconds:
                        break
        except httpx.HTTPError as e:
            log.warning("speed test GET %s failed: %s", target, type(e).__name__)
            raise _network_error(e, "GET", target, "镜像源") from e
        log.info("speed test GET %s -> %d bytes in %.2fs", target, received, seconds)
        return DownloadSample(received, seconds)

    # --- internals

    def _new_client(
        self,
        *,
        headers: Mapping[str, str],
        follow_redirects: bool,
        base_url: str = "",
        for_download: bool = False,
    ) -> httpx.Client:
        """API client (owns the injected test transport) or a short-lived download client."""
        if self._transport is not None:
            transport: httpx.BaseTransport = (
                _SharedTransport(self._transport) if for_download else self._transport
            )
            return httpx.Client(
                base_url=base_url,
                headers=headers,
                timeout=self._timeout,
                follow_redirects=follow_redirects,
                transport=transport,
            )
        ssl_context = self._ssl_context
        try:
            mounts: dict[str, httpx.BaseTransport | None]
            if self._proxy is not None:
                mounts = {"all://": _retrying_transport(ssl_context, self._proxy)}
            else:
                mounts = _env_mounts(ssl_context)
            return httpx.Client(
                base_url=base_url,
                headers=headers,
                timeout=self._timeout,
                follow_redirects=follow_redirects,
                transport=_retrying_transport(ssl_context),
                mounts=mounts,
            )
        except (httpx.InvalidURL, ValueError, ImportError) as e:
            if self._proxy is not None:
                raise InvalidInputError(_BAD_PROXY) from e
            raise InvalidInputError(_BAD_ENV_PROXY) from e

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        ok_statuses: frozenset[int] = frozenset(),
        not_found: str | None = None,
    ) -> httpx.Response:
        path = httpx.URL(url).path
        started = time.monotonic()
        try:
            resp = self._http.request(method, url, params=params)
        except httpx.TransportError as e:
            log.warning(
                "%s %s failed after %.2fs: %s", method, path, _since(started), type(e).__name__
            )
            raise _network_error(e, method, path, " GitHub ") from e
        except httpx.HTTPError as e:  # TooManyRedirects, DecodingError
            log.warning(
                "%s %s failed after %.2fs: %s", method, path, _since(started), type(e).__name__
            )
            detail = redact(str(e)).strip() or type(e).__name__
            raise GitHubError(
                f"GitHub 请求失败（{method} {path}：{detail}）", method=method, path=path
            ) from e
        status = resp.status_code
        level = logging.DEBUG if status < 400 else logging.WARNING
        log.log(level, "%s %s -> %d (%.2fs)", method, path, status, _since(started))
        if status in ok_statuses or not resp.is_error:
            return resp
        raise self._error_for(resp, method, path, not_found=not_found)

    def _error_for(
        self, resp: httpx.Response, method: str, path: str, *, not_found: str | None = None
    ) -> GitHubError:
        status = resp.status_code
        message = _github_message(resp)
        ctx = f"{method} {path} -> {status}" + (f"：{message}" if message else "")
        if status == 401:
            return AuthError(
                f"PAT 无效或已过期，请重新登录（{ctx}）", status=status, method=method, path=path
            )
        if _is_rate_limited(resp, message):
            reset_at = _rate_limit_reset(resp.headers, datetime.now(UTC))
            when = (
                f"在 {reset_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')} 之后"
                if reset_at is not None
                else "稍后"
            )
            return RateLimitError(
                f"GitHub API 调用次数超限，请{when}重试（{ctx}）",
                reset_at=reset_at,
                status=status,
                method=method,
                path=path,
            )
        if status == 403:
            hint = self._permission_hint()
            needed = resp.headers.get("x-accepted-github-permissions", "").strip()
            if needed:
                hint += f"（GitHub 提示需要：{needed}）"
            return PermissionDeniedError(
                f"PAT 权限不足（{ctx}）。{hint}", status=status, method=method, path=path
            )
        if status == 404:
            return NotFoundError(
                f"{not_found or '仓库不存在或 PAT 没有访问权限'}（{ctx}）",
                status=status,
                method=method,
                path=path,
            )
        if status >= 500:
            return GitHubError(
                f"GitHub 服务暂时不可用，请稍后重试（{ctx}）",
                status=status,
                method=method,
                path=path,
            )
        return GitHubError(f"GitHub 请求失败（{ctx}）", status=status, method=method, path=path)

    def _permission_hint(self) -> str:
        if self._pat.startswith("github_pat_"):
            return "fine-grained PAT 需要选中该仓库，并授予 Administration: Read and write 权限"
        if self._pat.startswith(("ghp_", "gho_", "ghu_")):
            return "classic PAT 需要勾选 repo 权限"
        return (
            "fine-grained PAT 需要 Administration: Read and write 权限；classic PAT 需要 repo 权限"
        )

    def _paginate(
        self, path: str, params: Mapping[str, Any], *, key: str | None = None
    ) -> list[Any]:
        """Follow `Link: rel="next"` (same origin only); `key` picks the list out of an object."""
        items: list[Any] = []
        url = path
        page_params: Mapping[str, Any] | None = params
        base = self._http.base_url
        for _ in range(MAX_PAGES):
            resp = self._request("GET", url, params=page_params)
            data = self._json(resp, "GET", path)
            page = data if key is None else (data.get(key) if isinstance(data, dict) else None)
            if not isinstance(page, list):
                raise self._bad_payload(resp, "GET", path)
            items.extend(page)
            next_link = resp.links.get("next", {}).get("url")
            if not next_link:
                return items
            next_url = base.join(next_link)
            if (next_url.scheme, next_url.host, next_url.port) != (
                base.scheme,
                base.host,
                base.port,
            ):
                raise GitHubError(
                    f"GitHub 返回的分页链接指向其他主机，已拒绝（GET {path}）",
                    status=resp.status_code,
                    method="GET",
                    path=path,
                )
            url, page_params = str(next_url), None
        raise GitHubError(
            f"GitHub 分页超过 {MAX_PAGES} 页，已停止（GET {path}）", method="GET", path=path
        )

    def _runner_token(self, path: str) -> RunnerToken:
        resp = self._request("POST", path)
        data = self._json(resp, "POST", path)
        token = data.get("token") if isinstance(data, dict) else None
        if isinstance(token, str):
            register_secret(token)
        try:
            return RunnerToken.model_validate(data)
        except ValidationError:
            # `from None`: the validation error would carry the token value in its text.
            raise self._bad_payload(resp, "POST", path) from None

    def _json(self, resp: httpx.Response, method: str, path: str) -> Any:
        try:
            return resp.json()
        except ValueError as e:
            raise GitHubError(
                f"GitHub 返回的内容不是 JSON（{method} {path} -> {resp.status_code}）",
                status=resp.status_code,
                method=method,
                path=path,
            ) from e

    def _model(
        self,
        model: type[_M],
        data: Any,
        resp: httpx.Response | None,
        method: str,
        path: str,
    ) -> _M:
        try:
            return model.model_validate(data)
        except ValidationError as e:
            raise self._bad_payload(resp, method, path) from e

    @staticmethod
    def _bad_payload(resp: httpx.Response | None, method: str, path: str) -> GitHubError:
        status = resp.status_code if resp is not None else None
        suffix = f" -> {status}" if status is not None else ""
        return GitHubError(
            f"GitHub 返回的数据格式不符合预期（{method} {path}{suffix}）",
            status=status,
            method=method,
            path=path,
        )

    @staticmethod
    def _repo_path(owner: str, repo: str) -> str:
        owner, repo = validate_owner_repo(owner, repo)
        return f"/repos/{owner}/{repo}"

    def _runner_path(self, owner: str, repo: str, runner_id: int) -> str:
        if isinstance(runner_id, bool) or not isinstance(runner_id, int) or runner_id <= 0:
            raise InvalidInputError(f"runner id 不合法：{runner_id!r}")
        return f"{self._repo_path(owner, repo)}/actions/runners/{runner_id}"


def _since(started: float) -> float:
    return time.monotonic() - started
