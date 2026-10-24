"""Print one version's section of CHANGELOG.md, for use as GitHub Release notes.

    python scripts/changelog_section.py <version> [changelog_path]

Finds the Keep-a-Changelog heading ``## [<version>]`` (the heading may continue with
`` - YYYY-MM-DD``) and prints its body up to the next ``## `` heading or the link-reference
block at the end of the file. Exit 0 with the body on stdout, 1 with a message on stderr
when the section is missing or empty, 2 for bad arguments. Standard library only: release.yml
runs it on a bare checkout.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path

DEFAULT_CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"

_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\]")
# Keep a Changelog puts `[1.0.0]: https://...` link definitions at the end of the file.
_LINK_REF = re.compile(r"^\[[^\]]+\]:\s*\S")


def extract_section(text: str, version: str) -> str | None:
    """Return the body of ``## [version]`` without the heading, or None when absent.

    The body keeps its inner formatting and loses surrounding blank lines. An empty body
    comes back as "" so the caller can distinguish "missing" from "nothing written yet".
    """
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match and match.group("version") == version:
            start = index + 1
            break
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("## ") or _LINK_REF.match(line):
            break
        body.append(line)
    return "\n".join(body).strip()


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (1, 2):
        print(
            f"用法：{Path(sys.argv[0]).name or 'changelog_section.py'} <version> [CHANGELOG.md]",
            file=sys.stderr,
        )
        return 2
    version = args[0].removeprefix("v")
    path = Path(args[1]) if len(args) == 2 else DEFAULT_CHANGELOG
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"错误：读取 {path} 失败：{e}", file=sys.stderr)
        return 1
    body = extract_section(text, version)
    if body is None:
        print(f'错误：{path.name} 里没有 "## [{version}]" 段落', file=sys.stderr)
        return 1
    if not body:
        print(f'错误：{path.name} 的 "## [{version}]" 段落是空的', file=sys.stderr)
        return 1
    # Release notes are UTF-8 whatever the console code page is (cp1252 on the Windows runner
    # cannot encode the Chinese UI texts quoted in the changelog).
    sys.stdout.buffer.write((body + "\n").encode("utf-8"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
