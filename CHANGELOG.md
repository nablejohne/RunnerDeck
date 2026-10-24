# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

M3: RunnerDeck can be left running all day. It lives in the tray, starts with Windows if
asked to, starts your runners itself, keeps their GitHub status current and tells you when a
job fails.

### Added
- Tray icon with a status dot (grey: all stopped, green: online, blue: running a job, red:
  something needs attention) and a menu: open the window, start / stop all, start or stop
  each runner, quit. A left click opens the window.
- Closing the window hides it to the tray (setting, on by default); "退出" in the tray menu
  stops every runner gracefully first, after a confirmation that names the jobs in progress.
- Launch on login through `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
  (`RunnerDeck.exe --autostart`: tray only, no window). The entry is checked at every start
  and rewritten if the unzipped folder was moved; an entry disabled in Task Manager is
  reported, not overridden.
- "随 RunnerDeck 启动" per runner (add wizard, card menu, detail page): those runners start
  when RunnerDeck starts; one already run by another process is skipped with a notice.
- Status poller: GitHub status is refreshed every 15–300 s (settings page, default 30 s) and
  a few seconds after a runner changes state; while every repository request fails it backs
  off to every 5 minutes. Local process checks are never slowed down.
- Runner detail page (click a card): status, repository, directory, labels, GitHub id and
  runner version; live output and the runner's `_diag` logs (last 1 MB, redacted) in one
  log view. It replaces the output dialog.
- Import an existing runner directory ("导入" on the dashboard). A banner lists unregistered
  runner directories under the runner root and runner processes RunnerDeck did not start,
  with import / end-process buttons.
- Take over a runner that another process runs (for example `runnerdeck-cli start`): end
  that process and start the runner from RunnerDeck.
- "取消登记" (CLI: `runnerdeck-cli forget <key>`): drop RunnerDeck's record only; the
  directory and the GitHub registration stay as they are.
- Tray notifications: failed jobs by default (all / failed only / off in settings), a runner
  entering an error state, and new unregistered directories or stray runner processes found
  while the window is hidden.
- Settings: launch on login, start minimised to the tray, close to the tray, job
  notifications, theme (system / light / dark), poll interval.
- The proxy from the settings page is also written to each runner's `.env` (`https_proxy`,
  `http_proxy`), so a runner uses it however it is started. Changing it rewrites the values
  RunnerDeck wrote and leaves a proxy you set yourself alone; running runners pick it up on
  their next start.
- Service mode: "以 Windows 服务运行" in the add wizard (CLI: `add --service`) registers a
  runner as a Windows service (NETWORK SERVICE, starts with Windows, no sign-in needed, keeps
  running when RunnerDeck quits) after one UAC prompt; starting and stopping it later needs no
  prompt. "转为服务…" turns a stopped runner into a service (its GitHub runner id changes);
  "授权启停…" grants start / stop rights for a service runner you imported. Status, live output
  and job notifications of a service runner come from its `_diag` Runner log; removing one
  deletes the service too. CLI: `to-service`, `grant-service`.
- Detail page "Workflow" tab: which jobs of the repository's workflows this runner can take
  (labels compared with `runs-on`; a job asking for a label the runner lacks is flagged, as it
  would stay queued forever) and the latest runs with links. Needs Contents: Read and
  Actions: Read on a fine-grained PAT (optional).
- Update check: "检查更新" on the settings page, and once shortly after start (can be turned
  off). It only tells you about a newer release and links to it; nothing is downloaded.
- "加固权限" on the settings page restricts the runner root to Administrators, SYSTEM and you
  (other users can only list it). A root RunnerDeck creates itself gets this automatically.

### Changed
- Starting RunnerDeck a second time brings the running window to the front instead of
  showing "已经在运行" (a per-user local server next to the lock file).
- Right after a graceful stop GitHub keeps a runner online for up to about a minute; that
  window now shows "已停止" instead of "被其他进程运行". A GitHub answer older than the latest
  local state change no longer overrides it, and dashboard counts use the same grouping as
  the tray colour.
- A status refresh scans the runner processes once instead of once per runner.
- On Windows logoff or shutdown running runners are told to stop without holding up the
  session end.

### Fixed
- Cancelling a Windows sign-out ("仍要注销 / 取消") left the runners RunnerDeck had already
  told to stop stopped; they now start again once they have stopped.
- Whether a runner was killed or stopped gracefully was only kept in memory: after a restart,
  or a `runnerdeck-cli stop` from another terminal, a killed runner showed as "被其他进程运行"
  until its GitHub session expired. Stop records are now saved per runner under
  `%LOCALAPPDATA%\RunnerDeck\stops`, so the status shows "GitHub 会话未过期" instead.

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
