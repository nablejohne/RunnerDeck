"""M0 spike: exercise every GitHub REST call RunnerDeck M1 depends on.

Usage (any OS):
    set RUNNERDECK_TEST_PAT / RUNNERDECK_TEST_REPO=owner/repo
    uv run python scripts/spike_api.py [--save-fixtures]

--save-fixtures writes trimmed, token-scrubbed responses to tests/fixtures/api/.
Only the test repo's own entry is kept from /user/repos, so other repo names never
reach the (public) repository.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from typing import Any

from spike_common import (
    REPO_ROOT,
    Redactor,
    asset_sha256,
    check,
    extract_sha256,
    get_paginated,
    get_release,
    list_runners,
    load_env,
    make_client,
    pick_asset,
    registration_token,
    remove_token,
    save_fixture,
)

REPO_FIELDS = ("name", "full_name", "private", "html_url", "fork", "archived")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--save-fixtures", action="store_true")
    args = parser.parse_args()

    env = load_env()
    redact = Redactor()
    redact.add(env.pat)
    fixtures: dict[str, Any] = {}
    failures = 0

    def step(title: str, fn: Callable[[], str]) -> None:
        nonlocal failures
        t0 = time.monotonic()
        try:
            summary = fn()
            print(f"[ OK ] {title} ({time.monotonic() - t0:.2f}s)\n       {summary}")
        except Exception as e:  # spike: report and continue
            failures += 1
            print(f"[FAIL] {title}\n       {redact(str(e))}")

    with make_client(env.pat) as client:

        def user() -> str:
            resp = check(client.get("/user"))
            data = resp.json()
            fixtures["user.json"] = {k: data.get(k) for k in ("login", "id", "type")}
            scopes = resp.headers.get("x-oauth-scopes")
            expiry = resp.headers.get("github-authentication-token-expiration")
            kind = "classic" if scopes is not None else "fine-grained/other"
            return (
                f"login={data['login']} token={kind} scopes={scopes!r} expires={expiry!r} "
                f"rate_remaining={resp.headers.get('x-ratelimit-remaining')}"
            )

        def repos() -> str:
            items = get_paginated(
                client,
                "/user/repos",
                {"affiliation": "owner", "per_page": 100, "sort": "full_name"},
            )
            mine = next((r for r in items if r["full_name"].lower() == env.full_name.lower()), None)
            if mine is None:
                raise RuntimeError(
                    f"{env.full_name} not in /user/repos ({len(items)} repos listed)"
                )
            fixtures["repo.json"] = {k: mine.get(k) for k in REPO_FIELDS}
            private = sum(1 for r in items if r["private"])
            return f"{len(items)} repos ({private} private); test repo private={mine['private']}"

        def reg_token() -> str:
            data = registration_token(client, env)
            redact.add(data["token"])
            fixtures["registration_token.json"] = data
            return f"token=*** (len {len(data['token'])}) expires_at={data['expires_at']}"

        def rm_token() -> str:
            data = remove_token(client, env)
            redact.add(data["token"])
            fixtures["remove_token.json"] = data
            return f"token=*** expires_at={data['expires_at']}"

        def runners() -> str:
            data = list_runners(client, env)
            fixtures["runners.json"] = {"total_count": len(data), "runners": data}
            rows = [
                f"{r['id']}:{r['name']} {r['status']} busy={r['busy']} "
                f"labels={[lb['name'] for lb in r['labels']]}"
                for r in data
            ]
            return f"{len(data)} runner(s)" + "".join(f"\n       - {row}" for row in rows)

        def release() -> str:
            rel = get_release(client)
            asset = pick_asset(rel)
            from_body = extract_sha256(rel.get("body") or "")
            from_digest = asset_sha256(asset)
            fixtures["release_latest.json"] = {
                "tag_name": rel["tag_name"],
                "body": rel.get("body"),
                "assets": [
                    {k: a.get(k) for k in ("name", "size", "digest", "browser_download_url")}
                    for a in rel["assets"]
                ],
            }
            agree = "match" if from_body and from_body == from_digest else "MISMATCH/missing"
            return (
                f"{rel['tag_name']} {asset['name']} {asset['size'] / 1e6:.1f} MB; "
                f"sha(body)={from_body} sha(digest)={from_digest} -> {agree}"
            )

        step("GET /user", user)
        step("GET /user/repos (paginated)", repos)
        step("POST registration-token", reg_token)
        step("POST remove-token", rm_token)
        step("GET actions/runners", runners)
        step("GET actions/runner releases/latest", release)

    if args.save_fixtures:
        for name, data in fixtures.items():
            print(f"saved {save_fixture(f'api/{name}', data).relative_to(REPO_ROOT)}")
    print(f"\n{6 - failures}/6 steps passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
