"""FakeController (tests/ui/conftest.py) must not drift from the real RunnerController.

The ui tests only ever see the fake; this guards that every signal, method and attribute they
rely on exists on RunnerController, and that methods accept the same keyword arguments.
"""

from __future__ import annotations

import inspect

import pytest
from PyQt5.QtCore import pyqtBoundSignal, pyqtSignal

from runnerdeck.app.controller import RunnerController
from ui.conftest import FakeController

# Test-only helpers on the fake (not part of the controller surface).
FAKE_ONLY = {"calls", "calls_of", "set_state", "set_display", "states", "busy", "outputs",
             "displays", "shutdown_leftovers", "auto_plan", "auto_add_steps", "auto_added",
             "remote_busy_ids", "emergency_count"}  # fmt: skip


def _public_class_members(cls: type) -> dict[str, object]:
    return {
        name: value
        for klass in reversed(cls.__mro__)
        if klass.__module__.startswith(("ui.", "tests.", "runnerdeck."))
        for name, value in vars(klass).items()
        if not name.startswith("_")
    }


FAKE_MEMBERS = _public_class_members(FakeController)
SIGNALS = sorted(n for n, v in FAKE_MEMBERS.items() if isinstance(v, pyqtSignal))
METHODS = sorted(
    n for n, v in FAKE_MEMBERS.items() if callable(v) and not isinstance(v, pyqtSignal)
)
METHODS = [n for n in METHODS if n not in FAKE_ONLY]
# Attributes the fake sets in __init__ that the ui reads (properties on the real controller).
ATTRIBUTES = ["config", "is_logged_in", "refreshing", "autostart_supported", "records"]


def test_surface_was_collected() -> None:
    assert "display_changed" in SIGNALS and "take_over" in METHODS


@pytest.mark.parametrize("name", SIGNALS)
def test_signal_exists_on_controller(name: str) -> None:
    assert isinstance(getattr(RunnerController, name, None), pyqtSignal | pyqtBoundSignal)


@pytest.mark.parametrize("name", METHODS)
def test_method_exists_with_compatible_keywords(name: str) -> None:
    real = getattr(RunnerController, name, None)
    assert callable(real), f"RunnerController.{name} is missing"
    fake_params = inspect.signature(getattr(FakeController, name)).parameters
    real_params = inspect.signature(real).parameters
    for param in fake_params.values():
        if param.name == "self" or param.kind is param.VAR_KEYWORD:
            continue
        assert param.name in real_params, f"{name}() has no parameter {param.name!r}"
        if param.kind is param.KEYWORD_ONLY:
            assert real_params[param.name].kind is param.KEYWORD_ONLY, f"{name}({param.name})"


@pytest.mark.parametrize("name", ATTRIBUTES)
def test_attribute_exists_on_controller(name: str) -> None:
    assert hasattr(RunnerController, name), f"RunnerController.{name} is missing"
