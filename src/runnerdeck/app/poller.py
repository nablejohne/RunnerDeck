"""StatusPoller: keeps the dashboard status fresh in the background (docs/m3-design.md §4.2).

Every `settings.poll_interval_s` seconds it asks the controller for a quiet refresh: a local
process scan always, plus GitHub's runner lists when logged in. When every repo failed in the
last GitHub poll (offline, proxy down, PAT revoked) GitHub is only asked every SLOW_INTERVAL_S
seconds until a repo answers again, the user logs in or the config changes; the local scan never
slows down. Local state changes that GitHub will reflect soon (a runner went idle / stopped /
failed, an external runner was killed, a runner was added or imported) `kick()` one extra
GitHub poll KICK_DELAY_MS later; kicks within that delay collapse into one.

Only Qt timers and the controller's own signals: everything blocking runs in the controller's
workers. The clock is injectable so tests drive the slow-mode window without sleeping.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from PyQt5.QtCore import QObject, QTimer

from runnerdeck.core.models import AppConfig, LocalState, RemoteStatus

if TYPE_CHECKING:
    from runnerdeck.app.controller import RunnerController
    from runnerdeck.core.manager import RunnerStatusRow

log = logging.getLogger(__name__)

# A runner entering one of these will soon look different on GitHub: poll it a bit later.
KICK_STATES = frozenset({LocalState.IDLE, LocalState.STOPPED, LocalState.ERROR})


class StatusPoller(QObject):
    """Periodic `controller.refresh_statuses(remote=..., quiet=True)`.

    parent: defaults to the controller, so `StatusPoller(controller).start()` keeps running
    without the caller holding a reference (Qt owns it, and it dies with the controller).
    """

    SLOW_INTERVAL_S = 300
    KICK_DELAY_MS = 3000

    def __init__(
        self,
        controller: RunnerController,
        *,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent if parent is not None else controller)
        self._controller = controller
        self._clock = clock
        self._interval_s = controller.config.settings.poll_interval_s
        self._slow = False
        self._last_remote: float | None = None  # clock() of the last GitHub poll we asked for
        self._active = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._kick_timer = QTimer(self)
        self._kick_timer.setSingleShot(True)
        self._kick_timer.setInterval(self.KICK_DELAY_MS)
        self._kick_timer.timeout.connect(self._on_kick)

        controller.runner_state.connect(self._on_runner_state)
        controller.external_stopped.connect(self._on_kick_signal)
        controller.runner_added.connect(self._on_kick_signal)
        controller.runner_imported.connect(self._on_kick_signal)
        controller.config_changed.connect(self._on_config_changed)
        controller.logged_in.connect(self._on_logged_in)
        controller.statuses_updated.connect(self._on_statuses_updated)

    # --- public ---------------------------------------------------------------------------------

    @property
    def interval_s(self) -> int:
        """The regular poll interval (settings.poll_interval_s)."""
        return self._interval_s

    @property
    def slow(self) -> bool:
        """Every repo failed in the last GitHub poll: GitHub is asked every SLOW_INTERVAL_S."""
        return self._slow

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> None:
        """Poll now, then every interval_s seconds."""
        self._active = True
        self._timer.start(self._interval_s * 1000)
        self._poll(kicked=False)

    def stop(self) -> None:
        self._active = False
        self._timer.stop()
        self._kick_timer.stop()

    def kick(self) -> None:
        """One GitHub poll KICK_DELAY_MS from now; a pending kick is pushed back (debounce)."""
        if self._active:
            self._kick_timer.start()

    # --- timers ---------------------------------------------------------------------------------

    def _on_tick(self) -> None:
        self._poll(kicked=False)

    def _on_kick(self) -> None:
        if not self._poll(kicked=True) and self._controller.refreshing and self._active:
            self._kick_timer.start()  # a refresh is running: try again after another delay

    def _poll(self, *, kicked: bool) -> bool:
        """One quiet refresh; False if skipped (no records, or a refresh is in flight)."""
        controller = self._controller
        if not controller.records or controller.refreshing:
            return False
        remote = controller.is_logged_in and (kicked or self._remote_due())
        if remote:
            self._last_remote = self._clock()
        controller.refresh_statuses(remote=remote, quiet=True)
        return True

    def _remote_due(self) -> bool:
        if not self._slow or self._last_remote is None:
            return True
        return self._clock() - self._last_remote >= self.SLOW_INTERVAL_S

    # --- controller signals ---------------------------------------------------------------------

    def _on_runner_state(self, _record_id: str, state: object, _job: object) -> None:
        if state in KICK_STATES:
            self.kick()

    def _on_kick_signal(self, *_args: object) -> None:
        self.kick()

    def _on_config_changed(self, config: AppConfig) -> None:
        self._set_slow(False)
        interval = config.settings.poll_interval_s
        if interval != self._interval_s:
            self._interval_s = interval
            if self._active:
                self._timer.start(interval * 1000)

    def _on_logged_in(self, _user: object) -> None:
        self._set_slow(False)

    def _on_statuses_updated(self, rows: list[RunnerStatusRow]) -> None:
        """Judge GitHub's reachability from any refresh's rows (ours or the user's).

        A repo answered -> its rows carry a fetched snapshot and no error. A local-only refresh
        (remote=False) has UNKNOWN without an error everywhere and says nothing.
        """
        if any(
            r.remote_error is None and r.remote.status is not RemoteStatus.UNKNOWN for r in rows
        ):
            self._set_slow(False)
        elif rows and all(r.remote_error is not None for r in rows):
            self._set_slow(True)

    def _set_slow(self, slow: bool) -> None:
        if slow != self._slow:
            self._slow = slow
            if slow:
                log.info("every repo failed: asking GitHub every %d s", self.SLOW_INTERVAL_S)
            else:
                log.info("GitHub polling back to every %d s", self._interval_s)
