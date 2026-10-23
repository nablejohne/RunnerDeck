"""Repo-wide test setup: Qt renders offscreen (CI has no display; pytest-qt needs a platform)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
