"""Light / dark / follow-the-system theme (settings.theme, docs/m3-design.md §5).

qfluentwidgets keeps the theme in its global `qconfig`; RunnerDeck never lets it write its own
config file (`save=False`): the choice lives in AppSettings.theme.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType

from qfluentwidgets import Theme, qconfig, setTheme

log = logging.getLogger(__name__)

THEMES: Mapping[str, Theme] = MappingProxyType(
    {"auto": Theme.AUTO, "light": Theme.LIGHT, "dark": Theme.DARK}
)
# Settings page labels, in the order of the ComboBox.
THEME_LABELS: Mapping[str, str] = MappingProxyType(
    {"auto": "跟随系统", "light": "浅色", "dark": "深色"}
)


def apply_theme(name: str) -> None:
    """Switch the application theme; unknown names fall back to "auto"."""
    theme = THEMES.get(name)
    if theme is None:
        log.warning("unknown theme %r, following the system", name)
        theme = Theme.AUTO
    if qconfig.themeMode.value == theme:
        return  # restyling every widget is not free
    setTheme(theme, save=False)
