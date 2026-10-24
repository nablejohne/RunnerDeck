"""Dependency rules from CLAUDE.md, checked on the AST (backs up core/ruff.toml's TID251)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[2] / "src" / "runnerdeck" / "core"
BANNED_IN_CORE = ("PyQt5", "qfluentwidgets", "runnerdeck.app", "runnerdeck.ui")
# Only these modules may invoke config.cmd / run.cmd (CLAUDE.md 架构规则). winproc only
# supplies the absolute cmd.exe path (winproc.cmd_exe) and never runs a script itself.
CMD_SCRIPT_OWNERS = {"installer.py", "process.py", "winproc.py", "console_helper.py"}
# Only github_client talks HTTP.
HTTP_OWNERS = {"github_client.py"}
# Service mode (docs/m4-design.md §10): the SCM only through service.py, the UAC prompt only
# from elevate.py.
SCM_OWNERS = {"service.py"}
SHELL_EXECUTE_OWNERS = {"elevate.py"}


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
SRC = CORE.parent
APP_FILES = sorted((SRC / "app").glob("*.py"))
UI_FILES = sorted((SRC / "ui").rglob("*.py"))
# ui/ talks to core only through app/controller.py (CLAUDE.md: the UI thread never does
# network or subprocess work); the pure data / text modules are fine to import directly.
BANNED_IN_UI = (
    "runnerdeck.core.manager",
    "runnerdeck.core.github_client",
    "runnerdeck.core.process",
    "runnerdeck.core.installer",
    "runnerdeck.core.release",
    "runnerdeck.core.store",
    "runnerdeck.core.secrets",
    "runnerdeck.core.winproc",
    "runnerdeck.core.cli",
    # M3 IO modules (registry, stop records on disk, _diag files): also controller-only
    "runnerdeck.core.autostart",
    "runnerdeck.core.stop_marks",
    "runnerdeck.core.diag",
    # M4 service mode: SCM, UAC, the elevated helper
    "runnerdeck.core.service",
    "runnerdeck.core.elevate",
    "runnerdeck.core.elevated",
    "runnerdeck.core.workflows",  # calls GitHub through the client it is given
    "httpx",
    "subprocess",
    "winreg",
    "psutil",
)
BANNED_IN_APP = ("runnerdeck.ui", "qfluentwidgets")


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


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda p: p.name)
def test_only_service_uses_the_service_control_manager(path: Path) -> None:
    if path.name in SCM_OWNERS:
        return
    banned = ("win32service", "win32security", "win32serviceutil")
    assert not [n for n in _imports(path) if n.split(".")[0] in banned], (
        f"{path.name} imports the service / security API; use core/service.py"
    )


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda p: p.name)
def test_only_elevate_starts_elevated_processes(path: Path) -> None:
    if path.name in SHELL_EXECUTE_OWNERS:
        return
    text = path.read_text(encoding="utf-8")
    assert "ShellExecute" not in text and '"runas"' not in text, (
        f"{path.name} elevates a process; use core/elevate.py"
    )


def test_elevated_reaches_config_cmd_only_through_installer() -> None:
    text = (CORE / "elevated.py").read_text(encoding="utf-8")
    assert "subprocess" not in text and 'config.cmd"' not in text
    assert "installer.configure" in text and "installer.unconfigure" in text


def test_console_helper_is_stdlib_only() -> None:
    """It is a PyInstaller entry point of its own and must not drag runnerdeck / Qt in."""
    import sys

    names = _imports(CORE / "console_helper.py")
    assert names
    assert all(n.split(".")[0] in sys.stdlib_module_names for n in names), names


def test_cli_is_console_launch_only() -> None:
    text = (CORE / "cli.py").read_text(encoding="utf-8")
    assert "Launch.HELPER" not in text  # the CLI shares its console with run.cmd


@pytest.mark.parametrize("path", UI_FILES, ids=lambda p: str(p.relative_to(SRC)))
def test_ui_only_reaches_core_through_the_controller(path: Path) -> None:
    bad = [
        name
        for name in _imports(path)
        if any(name == b or name.startswith(b + ".") for b in BANNED_IN_UI)
    ]
    assert not bad, f"{path.relative_to(SRC)} imports {bad}"


@pytest.mark.parametrize("path", APP_FILES, ids=lambda p: p.name)
def test_app_does_not_import_ui(path: Path) -> None:
    bad = [
        name
        for name in _imports(path)
        if any(name == b or name.startswith(b + ".") for b in BANNED_IN_APP)
    ]
    assert not bad, f"app/{path.name} imports {bad}"
