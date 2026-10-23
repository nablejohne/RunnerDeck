"""Coloured pill showing a runner's dashboard state (docs/m2-design.md §5).

Colours follow DisplayState: green for healthy running states, amber for transitions,
red for anything that needs the user, grey for stopped / unknown. Text comes from
`runnerdeck.core.status` so the GUI and the CLI always agree.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from PyQt5.QtWidgets import QWidget
from qfluentwidgets import InfoBadge, InfoLevel

from runnerdeck.core.models import DisplayState, LocalState
from runnerdeck.core.status import DisplayStatus, display_label

BADGE_LEVELS: Mapping[DisplayState, InfoLevel] = MappingProxyType(
    {
        DisplayState.IDLE: InfoLevel.SUCCESS,
        DisplayState.BUSY: InfoLevel.ATTENTION,  # theme colour: running a job, not just idle
        DisplayState.STARTING: InfoLevel.WARNING,
        DisplayState.CONNECTING: InfoLevel.WARNING,
        DisplayState.RECONNECTING: InfoLevel.WARNING,
        DisplayState.STOPPING: InfoLevel.WARNING,
        DisplayState.SESSION_LINGERING: InfoLevel.WARNING,
        DisplayState.STALLED: InfoLevel.ERROR,
        DisplayState.ERROR: InfoLevel.ERROR,
        DisplayState.MISSING: InfoLevel.ERROR,
        DisplayState.EXTERNAL: InfoLevel.ERROR,
        DisplayState.STOPPED: InfoLevel.INFOAMTION,
        DisplayState.UNKNOWN: InfoLevel.INFOAMTION,
    }
)


def local_to_display(state: LocalState) -> DisplayState:
    """Display state for a purely local view (no GitHub information yet).

    LocalState values are a subset of DisplayState values, so the mapping is by name.
    """
    return DisplayState(state.value)


class StatusBadge(InfoBadge):
    """InfoBadge whose text and colour follow a DisplayState."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = DisplayState.STOPPED
        self.set_state(DisplayState.STOPPED)

    @property
    def state(self) -> DisplayState:
        return self._state

    def set_state(self, state: DisplayState, job: str | None = None) -> None:
        """Show `state` with the standard label (BUSY gets the job name appended)."""
        self._apply(state, display_label(state, job))

    def set_display(self, display: DisplayStatus) -> None:
        """Show a merged local + remote status (label already computed by status.py)."""
        self._apply(display.state, display.label)

    def _apply(self, state: DisplayState, label: str) -> None:
        self._state = state
        self.setText(label)
        self.setLevel(BADGE_LEVELS[state])
        self.adjustSize()
