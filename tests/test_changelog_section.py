"""Tests for scripts/changelog_section.py (release notes extraction)."""

from __future__ import annotations

from pathlib import Path

import pytest

from changelog_section import DEFAULT_CHANGELOG, extract_section, main

SAMPLE = """\
# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- Something not yet released.

## [0.2.0] - 2026-11-30

### Added
- Tray icon.

### Fixed
- Stop timeout.

## [0.1.0] - 2026-10-30

### Added
- First GUI.

[Unreleased]: https://github.com/example/RunnerDeck/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/example/RunnerDeck/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/example/RunnerDeck/releases/tag/v0.1.0
"""


def test_middle_section_stops_at_next_heading() -> None:
    body = extract_section(SAMPLE, "0.2.0")
    assert body == "### Added\n- Tray icon.\n\n### Fixed\n- Stop timeout."


def test_last_section_stops_at_link_references() -> None:
    body = extract_section(SAMPLE, "0.1.0")
    assert body == "### Added\n- First GUI."


def test_last_section_without_link_references_runs_to_end_of_file() -> None:
    text = SAMPLE.split("[Unreleased]: ")[0]
    assert extract_section(text, "0.1.0") == "### Added\n- First GUI."


def test_unreleased_and_missing_versions() -> None:
    assert extract_section(SAMPLE, "Unreleased") == "### Added\n- Something not yet released."
    assert extract_section(SAMPLE, "0.3.0") is None
    # Exact match on the bracket contents: a prerelease does not match the final version.
    assert extract_section(SAMPLE, "0.1") is None
    assert extract_section("## [0.1.0-rc1]\n- rc\n## [0.1.0]\n- final\n", "0.1.0") == "- final"


def test_empty_section_is_empty_string_not_none() -> None:
    assert extract_section("## [1.0.0]\n\n## [0.9.0]\n- old\n", "1.0.0") == ""


def test_main_prints_section(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(SAMPLE, encoding="utf-8")
    assert main(["0.2.0", str(changelog)]) == 0
    out = capsys.readouterr()
    assert out.out == "### Added\n- Tray icon.\n\n### Fixed\n- Stop timeout.\n"
    assert out.err == ""


def test_main_accepts_v_prefixed_tag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(SAMPLE, encoding="utf-8")
    assert main(["v0.1.0", str(changelog)]) == 0
    assert capsys.readouterr().out == "### Added\n- First GUI.\n"


def test_main_fails_when_section_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(SAMPLE, encoding="utf-8")
    assert main(["9.9.9", str(changelog)]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "## [9.9.9]" in out.err


def test_main_fails_when_section_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## [1.0.0]\n\n## [0.9.0]\n- old\n", encoding="utf-8")
    assert main(["1.0.0", str(changelog)]) == 1
    assert "空" in capsys.readouterr().err


def test_main_fails_when_file_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["1.0.0", str(tmp_path / "nope.md")]) == 1
    assert "读取" in capsys.readouterr().err


def test_main_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert main(["a", "b", "c"]) == 2
    assert "用法" in capsys.readouterr().err


def test_repo_changelog_has_a_section_for_the_project_version() -> None:
    """The real file must stay parseable: release.yml extracts the notes for this version.

    Between releases the version still names the last release (or 0.0.0 before the first),
    whose section must exist; `## [Unreleased]` may be empty right after a release.
    """
    import tomllib

    text = DEFAULT_CHANGELOG.read_text(encoding="utf-8")
    pyproject = DEFAULT_CHANGELOG.parent / "pyproject.toml"
    version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert "## [Unreleased]" in text
    assert extract_section(text, version if version != "0.0.0" else "Unreleased")


def test_main_writes_utf8_bytes(tmp_path: Path, capfdbinary: pytest.CaptureFixture[bytes]) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## [1.0.0] - 2026-10-24\n\n- 托盘图标\n", encoding="utf-8")
    assert main(["1.0.0", str(changelog)]) == 0
    assert "托盘图标".encode() in capfdbinary.readouterr().out
