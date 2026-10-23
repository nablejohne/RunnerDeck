"""Dependency rules from CLAUDE.md, checked on the AST (backs up core/ruff.toml's TID251)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[2] / "src" / "runnerdeck" / "core"
BANNED_IN_CORE = ("PyQt5", "qfluentwidgets", "runnerdeck.app", "runnerdeck.ui")
# Only these modules may invoke config.cmd / run.cmd (CLAUDE.md 架构规则). winproc only
# supplies the absolute cmd.exe path (winproc.cmd_exe) and never runs a script itself.
CMD_SCRIPT_OWNERS = {"installer.py", "process.py", "winproc.py"}
# Only github_client talks HTTP.
HTTP_OWNERS = {"github_client.py"}


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


CORE_FILES = sorted(CORE.glob("*.py"))


def test_core_files_found() -> None:
    assert len(CORE_FILES) > 10


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda p: p.name)
def test_core_does_not_import_qt_or_upper_layers(path: Path) -> None:
    bad = [
        name
        for name in _imports(path)
        if any(name == b or name.startswith(b + ".") for b in BANNED_IN_CORE)
    ]
    assert not bad, f"{path.name} imports {bad}"


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda p: p.name)
def test_only_installer_and_process_run_runner_scripts(path: Path) -> None:
    if path.name in CMD_SCRIPT_OWNERS:
        return
    tree = ast.parse(path.read_text(encoding="utf-8"))
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    # Every invocation goes through `cmd.exe /c <script>`; checking that config.cmd exists
    # (runner_file, manager) is fine.
    assert not [s for s in literals if s.strip().lower() == "cmd.exe"], (
        f"{path.name} builds a cmd.exe command; only installer.py / process.py may"
    )


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda p: p.name)
def test_only_github_client_imports_httpx(path: Path) -> None:
    if path.name in HTTP_OWNERS:
        return
    assert not [n for n in _imports(path) if n == "httpx" or n.startswith("httpx.")], (
        f"{path.name} imports httpx; GitHub calls must go through github_client.py"
    )
