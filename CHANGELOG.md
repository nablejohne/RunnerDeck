# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-24

First pre-release: a Windows desktop app that registers and runs repo-level GitHub Actions
self-hosted runners. Download `RunnerDeck-0.1.0-win-x64.zip`, unzip it anywhere and run
`RunnerDeck.exe`; sign in with a fine-grained PAT that selects your repositories and grants
Administration: Read and write. The exe is not code-signed yet, so SmartScreen may warn about an
unknown publisher; compare the zip's SHA-256 with the release page before running it.

### Added
- M0 spike scripts for the GitHub REST API and runner lifecycle (`scripts/spike_*.py`).
- Real runner stdout and config samples under `tests/fixtures/`.
- M1 core layer: config store, keyring PAT storage, GitHub client (OS trust store), runner
  release cache with SHA256 check, config.cmd register/remove, run.cmd supervision with
  graceful Ctrl+Break stop, status merging, add/remove/import flows with rollback.
- `runnerdeck-cli` debug CLI: login, repos, add, list, status, start, stop, remove, import,
  scan, settings.
- Project tooling: uv, ruff, pytest, GitHub Actions CI.
- M2 GUI (PyQt5 + PyQt-Fluent-Widgets): login dialog for the PAT, dashboard with one card per
  runner (status badge, start/stop, output log, open directory, remove), add wizard
  (repository picker, name/labels, PowerShell execution policy option, step-by-step progress),
  settings page (account, runner root, default labels, proxy, about). Closing the window
  stops every running runner gracefully first.
- `runnerdeck-helper` console helper so the GUI, which has no console of its own, can stop a
  runner gracefully with Ctrl+Break instead of killing it.
- PyInstaller onedir build (`scripts/build.ps1`) producing `RunnerDeck.exe`,
  `runnerdeck-helper.exe` and `runnerdeck-cli.exe` in one folder, zipped as
  `RunnerDeck-<version>-win-x64.zip`; `--version` and `--smoke-test` flags on the GUI exe.
- CI: offscreen Qt tests on Windows plus a `build` job that packages and smoke-tests the
  exes; `release.yml` publishes the zip as a GitHub Release on `v*` tags with notes taken
  from this file (`scripts/changelog_section.py`).

[Unreleased]: https://github.com/nablejohne/RunnerDeck/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/nablejohne/RunnerDeck/releases/tag/v0.1.0
