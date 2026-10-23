# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

### Added
- M0 spike scripts for the GitHub REST API and runner lifecycle (`scripts/spike_*.py`).
- Real runner stdout and config samples under `tests/fixtures/`.
- M1 core layer: config store, keyring PAT storage, GitHub client (OS trust store), runner
  release cache with SHA256 check, config.cmd register/remove, run.cmd supervision with
  graceful Ctrl+Break stop, status merging, add/remove/import flows with rollback.
- `runnerdeck-cli` debug CLI: login, repos, add, list, status, start, stop, remove, import,
  scan, settings.
- Project tooling: uv, ruff, pytest, GitHub Actions CI.
