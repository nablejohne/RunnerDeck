"""Shared helpers for the M0 spike scripts.

Throwaway by design: whatever survives the spike gets reimplemented in core/ during M1.
Only the pure helpers here are unit-tested.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import httpx

API = "https://api.github.com"
REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
SPIKE_OUT = REPO_ROOT / "spike-out"  # gitignored: raw logs, results

RUNNER_PROCESS_NAMES = frozenset(
    {"runner.listener.exe", "runner.worker.exe", "runner.pluginhost.exe"}
)


# --- environment -----------------------------------------------------------


@dataclass(frozen=True)
class SpikeEnv:
    pat: str
    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_repo(value: str) -> tuple[str, str]:
    owner, sep, name = value.strip().partition("/")
    if not sep or not owner or not name or "/" in name:
        raise ValueError(f"expected owner/repo, got {value!r}")
    return owner, name


def load_env() -> SpikeEnv:
    pat = os.environ.get("RUNNERDECK_TEST_PAT", "").strip()
    repo = os.environ.get("RUNNERDECK_TEST_REPO", "").strip()
    if not pat or not repo:
        sys.exit("Set RUNNERDECK_TEST_PAT and RUNNERDECK_TEST_REPO (owner/repo) first.")
    try:
        owner, name = parse_repo(repo)
    except ValueError as e:
        sys.exit(f"RUNNERDECK_TEST_REPO: {e}")
    return SpikeEnv(pat, owner, name)


# --- redaction ---------------------------------------------------------------

_TOKEN_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(--token\s+)\S+"),
)
_SECRET_KEYS = frozenset({"token"})


class Redactor:
    """Masks known secret values plus anything that looks like a GitHub token."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def add(self, secret: str | None) -> None:
        if secret:
            self._secrets.add(secret)

    def __call__(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        for pattern in _TOKEN_PATTERNS:
            text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
        return text


def scrub(obj: Any) -> Any:
    """Return a copy of a JSON value with secret-bearing keys masked."""
    if isinstance(obj, dict):
        return {k: "***" if k in _SECRET_KEYS else scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


# --- GitHub API --------------------------------------------------------------


def make_client(pat: str) -> httpx.Client:
    # trust_env (default) picks up HTTPS_PROXY / NO_PROXY, which is what we want to verify.
    return httpx.Client(
        base_url=API,
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "RunnerDeck-spike",
        },
        timeout=30,
    )


def check(resp: httpx.Response) -> httpx.Response:
    if resp.is_error:
        try:
            message = resp.json().get("message", "")
        except ValueError:
            message = resp.text[:200]
        raise RuntimeError(
            f"{resp.request.method} {resp.request.url.path} -> {resp.status_code}: {message}"
        )
    return resp


def get_paginated(client: httpx.Client, path: str, params: dict[str, Any]) -> list[dict]:
    items: list[dict] = []
    url: str | None = path
    while url:
        resp = check(client.get(url, params=params if url == path else None))
        items.extend(resp.json())
        url = resp.links.get("next", {}).get("url")
    return items


def registration_token(client: httpx.Client, env: SpikeEnv) -> dict:
    path = f"/repos/{env.full_name}/actions/runners/registration-token"
    return check(client.post(path)).json()


def remove_token(client: httpx.Client, env: SpikeEnv) -> dict:
    return check(client.post(f"/repos/{env.full_name}/actions/runners/remove-token")).json()


def list_runners(client: httpx.Client, env: SpikeEnv) -> list[dict]:
    resp = check(client.get(f"/repos/{env.full_name}/actions/runners", params={"per_page": 100}))
    return resp.json()["runners"]


def find_runner(client: httpx.Client, env: SpikeEnv, name: str) -> dict | None:
    return next((r for r in list_runners(client, env) if r["name"] == name), None)


def get_release(client: httpx.Client, version: str | None = None) -> dict:
    path = "/repos/actions/runner/releases/" + (f"tags/v{version}" if version else "latest")
    return check(client.get(path)).json()


# --- release parsing ---------------------------------------------------------


def extract_sha256(body: str, platform: str = "win-x64") -> str | None:
    """Read the checksum the runner release notes embed between BEGIN/END SHA markers."""
    m = re.search(
        rf"<!--\s*BEGIN SHA {re.escape(platform)}\s*-->\s*([0-9a-fA-F]{{64}})\s*"
        rf"<!--\s*END SHA {re.escape(platform)}\s*-->",
        body,
    )
    return m.group(1).lower() if m else None


def pick_asset(release: dict, platform: str = "win-x64") -> dict:
    pattern = re.compile(rf"^actions-runner-{re.escape(platform)}-[\d.]+\.zip$")
    for asset in release.get("assets", []):
        if pattern.match(asset["name"]):
            return asset
    raise LookupError(f"no {platform} zip in release {release.get('tag_name')}")


def asset_sha256(asset: dict) -> str | None:
    digest = asset.get("digest") or ""
    return digest.removeprefix("sha256:").lower() or None


# --- runner directory ----------------------------------------------------------


def read_runner_file(path: Path) -> dict:
    """Parse a runner's `.runner` JSON, which is written with a UTF-8 BOM."""
    return json.loads(path.read_text(encoding="utf-8-sig"))


class RunnerEvent(NamedTuple):
    kind: str  # idle | busy | completed
    detail: str | None


# Provisional patterns taken from the plan; M0 replaces them with what real output shows.
_EVENT_PATTERNS = (
    (re.compile(r"Listening for Jobs"), "idle"),
    (re.compile(r"Running job:\s*(?P<job>.+?)\s*$"), "busy"),
    (re.compile(r"Job (?P<job>.+?) completed with result:\s*(?P<result>\w+)"), "completed"),
)


def classify_line(line: str) -> RunnerEvent | None:
    for pattern, kind in _EVENT_PATTERNS:
        m = pattern.search(line)
        if m:
            groups = m.groupdict()
            if kind == "completed":
                return RunnerEvent(kind, f"{groups['job']}: {groups['result']}")
            return RunnerEvent(kind, groups.get("job"))
    return None


def decode_output(raw: bytes) -> str:
    """Runner output is expected to be UTF-8; fall back to the console code page."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("mbcs" if sys.platform == "win32" else "latin-1", errors="replace")


def find_runner_processes(runner_dir: Path) -> list[Any]:
    """Runner.* processes whose executable lives under runner_dir (psutil.Process list)."""
    import psutil

    root = str(runner_dir.resolve()).lower()
    found = []
    for proc in psutil.process_iter(["name", "exe"]):
        name = (proc.info.get("name") or "").lower()
        exe = (proc.info.get("exe") or "").lower()
        if name in RUNNER_PROCESS_NAMES and exe.startswith(root):
            found.append(proc)
    return found


# --- output -------------------------------------------------------------------


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def save_fixture(relpath: str, data: Any) -> Path:
    path = FIXTURES / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scrub(data), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def require_windows() -> None:
    if sys.platform != "win32":
        sys.exit("This command drives config.cmd / run.cmd and only runs on Windows.")
