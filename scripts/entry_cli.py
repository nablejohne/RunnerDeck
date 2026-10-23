"""PyInstaller entry script for runnerdeck-cli.exe (RunnerDeck.spec).

The console script `runnerdeck-cli` points at `runnerdeck.core.cli:main`; PyInstaller needs
a file, and using cli.py itself would load that module twice (as __main__ and as
runnerdeck.core.cli). This one-liner avoids that.
"""

from __future__ import annotations

import sys

from runnerdeck.core.cli import main

if __name__ == "__main__":
    sys.exit(main())
