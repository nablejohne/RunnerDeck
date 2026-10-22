"""RunnerDeck: manage repo-level GitHub Actions self-hosted runners on Windows."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("runnerdeck")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0+unknown"
