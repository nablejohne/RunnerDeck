"""Business flows on top of the core modules: add / remove / import / scan / status / process.

The CLI (M1) and the Qt controller (M2) both call this module; the controller only moves the
calls onto worker threads. Every public method blocks (network, config.cmd, disk) and reports
progress through an optional `progress(step, text)` callback that runs in the calling thread.

Grounded in docs/spike-notes.md:
- §3: `config.cmd remove` deletes `.runner`, so remove reads `agentId` first and keeps it for the
  `DELETE /actions/runners/{id}` fallback.
- §5: a hard kill leaves the GitHub session alive for minutes; status merging lives in status.py.
  Who stopped a runner and how is kept across processes in stop_marks.py (record_stop), so a
  CLI `stop` in another terminal or a GUI restart still shows "GitHub 会话未过期".
Registration and remove tokens live about an hour: they are fetched right before config.cmd
runs and never cached.

GitHub answers 404 both for "this runner is gone" and for "this PAT cannot see the repo". A 404
from get_runner / delete_runner / the remove token is only read as "gone" after list_runners on
the same repo succeeded (_confirm_repo_access); otherwise the flow stops with NotFoundError and
deletes nothing, because the directory may hold a still-valid registration.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import socket
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

import psutil

from runnerdeck.core import installer, process, release, runner_file, winproc
from runnerdeck.core.errors import (
    GitHubError,
    InstallError,
    InvalidInputError,
    NotFoundError,
    ProcessError,
    RunnerDeckError,
    RunnerFileError,
    RunnerNotFoundError,
)
from runnerdeck.core.github_client import GitHubClient
from runnerdeck.core.installer import RunFn
from runnerdeck.core.models import (
    AppConfig,
    Discovery,
    LocalState,
    ProxyApplyResult,
    RemoteRunner,
    RemoteStatus,
    RunnerRecord,
    StrayRunner,
    default_runner_name,
    normalize_labels,
    validate_owner_repo,
    validate_runner_name,
)
from runnerdeck.core.process import (
    ExternalStop,
    Launch,
    PopenFn,
    RunnerProcess,
    RunnerProcessInfo,
)
from runnerdeck.core.redact import redact, register_secret
from runnerdeck.core.release import ReleaseCache
from runnerdeck.core.status import (
    DisplayStatus,
    LocalSnapshot,
    RemoteSnapshot,
    match_remote,
    merge_status,
)
from runnerdeck.core.stop_marks import StopMarks, path_key
from runnerdeck.core.store import ConfigStore, resolve_runner

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, str], None]  # (step id, Chinese description), calling thread
ClientFactory = Callable[[], GitHubClient]
ProcessFinder = Callable[[Path], Sequence[psutil.Process]]
# One pass over every runner process on the machine (process.scan_runner_processes).
ProcessScanner = Callable[[], Sequence[RunnerProcessInfo]]
ExternalStopper = Callable[[Path], ExternalStop]
Clock = Callable[[], datetime]

SERVICE_FILE = ".service"  # written by the runner's service installer (service mode is M4)

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
_MB = 1024 * 1024
_DOWNLOAD_UNKNOWN_STEP = 10 * _MB  # progress granularity when the size is unknown

# Real (spike §10): on a personal PC the default execution policy is Restricted, so every
# `shell: powershell` step fails. Written to the runner's `.env` it only affects that runner's
# jobs (process scope), not the system. The GUI's add wizard offers it, on by default.
POWERSHELL_POLICY_ENV: Mapping[str, str] = MappingProxyType(
    {"PSExecutionPolicyPreference": "RemoteSigned"}
)
# `.env` keys RunnerDeck manages for settings.proxy (m4-design §3): a service-mode runner does
# not inherit our environment, and NETWORK SERVICE has no system proxy of its own.
PROXY_ENV_KEYS: tuple[str, ...] = ("https_proxy", "http_proxy")


class Step(StrEnum):
    """Progress step ids passed as the first argument of ProgressFn."""

    # add_runner
    RELEASE = "release"
    DOWNLOAD = "download"
    EXTRACT = "extract"
    TOKEN = "token"
    CONFIGURE = "configure"
    ENV = "env"  # only when the plan has runner_env
    VERIFY = "verify"
    ROLLBACK = "rollback"
    # remove_runner
    CHECK = "check"
    READ = "read"
    UNCONFIGURE = "unconfigure"
    API_DELETE = "api_delete"
    DELETE_DIR = "delete_dir"
    # both
    SAVE = "save"
    DONE = "done"


class RemoteRemoval(StrEnum):
    """How the GitHub side of a runner was removed."""

    UNCONFIGURED = "unconfigured"  # `config.cmd remove` succeeded
    API_DELETED = "api_deleted"  # DELETE /actions/runners/{id} -> 204
    # 404, or no runner with that name; only after list_runners proved the repo is visible
    ALREADY_GONE = "already_gone"
    SKIPPED = "skipped"  # remove_runner(local_only=True): GitHub was not touched


class RollbackError(RunnerDeckError):
    """add_runner failed and its rollback could not undo everything.

    str(e) is the original message followed by what is left behind; `original` is the
    exception that started the rollback, `problems` the leftovers (Chinese, one per item).
    """

    def __init__(self, original: RunnerDeckError, problems: Sequence[str]) -> None:
        lines = "\n".join(f"  - {p}" for p in problems)
        super().__init__(redact(f"{original}\n回滚未完成，需要手动处理：\n{lines}"))
        self.original = original
        self.problems = tuple(problems)


@dataclass(frozen=True)
class AddPlan:
    """Read-only result of plan_add; add_runner executes it.

    owner / repo: canonical spelling as GitHub returns it. name: validated runner name.
    labels: normalized custom labels (the runner adds self-hosted / Windows / X64 itself).
    target_dir: absolute directory to extract into; missing or empty when planned.
    private / archived: repository flags from GitHub. version: runner version to install
    ("2.338.0"), None for the latest release. remote_conflict: a runner with the same name
    already registered in the repo on GitHub (config.cmd --replace takes it over).
    same_repo_runners: names of runners already recorded locally for this repo.
    warnings: Chinese texts the caller must show before add_runner.
    runner_env: KEY=VALUE pairs written to the runner's `.env` after registration (e.g.
    POWERSHELL_POLICY_ENV); empty means the file is left alone.
    """

    owner: str
    repo: str
    name: str
    labels: tuple[str, ...]
    target_dir: Path
    private: bool
    archived: bool = False
    version: str | None = None
    remote_conflict: RemoteRunner | None = None
    same_repo_runners: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    runner_env: tuple[tuple[str, str], ...] = ()

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"

    @property
    def actions_settings_url(self) -> str:
        """Where fork PR approval is configured (Settings -> Actions -> General)."""
        return f"{self.repo_url}/settings/actions"

    @property
    def needs_confirmation(self) -> bool:
        """Public repo (fork PRs can run code here) or an existing GitHub runner gets replaced."""
        return not self.private or self.remote_conflict is not None


@dataclass(frozen=True)
class RemoveResult:
    """Outcome of remove_runner (the record is always deleted when this is returned).

    remote: how the GitHub registration went away. runner_id: GitHub runner id used for the API
    fallback (None if it was never needed or not known). dir_removed: this call deleted the
    directory (with keep_work: everything except `_work`). warnings: Chinese notes for the user,
    e.g. why config.cmd remove failed or why the directory is still there.
    """

    record: RunnerRecord
    remote: RemoteRemoval
    runner_id: int | None
    dir_removed: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunnerStatusRow:
    """One line of the status table.

    remote_runner: the matched GitHub runner (None if the list failed or it is missing).
    remote_error: why the repo's runner list could not be fetched (remote is UNKNOWN then).
    """

    record: RunnerRecord
    display: DisplayStatus
    local: LocalSnapshot
    remote: RemoteSnapshot
    remote_runner: RemoteRunner | None = None
    remote_error: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _no_progress(step: str, text: str) -> None:
    pass


def _report_safely(report: ProgressFn, step: str, text: str) -> None:
    """For rollback and final messages: a failing callback must not stop the work."""
    try:
        report(step, text)
    except Exception:
        log.exception("progress callback failed at %s", step)


def _local_hostname() -> str:
    # "DESKTOP-AB12" on Windows; strip a domain such as "mac.local" on dev machines.
    return socket.gethostname().split(".")[0] or "runner"


_path_key = path_key  # one normalization for records and stop marks (stop_marks.path_key)


def _repo_key(owner: str, repo: str) -> tuple[str, str]:
    return owner.casefold(), repo.casefold()


def _same_repo(record: RunnerRecord, owner: str, repo: str) -> bool:
    return _repo_key(record.owner, record.repo) == _repo_key(owner, repo)


def _occupied(path: Path) -> bool:
    """paths.runner_dir_for's rule: taken if it exists and is not an empty directory."""
    try:
        if not path.exists():
            return False
        return not path.is_dir() or any(path.iterdir())
    except OSError:
        return True  # unreadable: never extract there


def _pick_target_dir(root: Path, repo: str, taken: set[str]) -> Path:
    """`root/repo`, then `repo-2`, `repo-3`, ... skipping non-empty dirs and recorded paths."""
    n = 1
    candidate = root / repo
    while _occupied(candidate) or _path_key(candidate) in taken:
        n += 1
        candidate = root / f"{repo}-{n}"
    return candidate


def _normalize_version(version: str | None) -> str | None:
    if version is None:
        return None
    value = version.strip().removeprefix("v")
    if not _VERSION_RE.fullmatch(value):
        raise InvalidInputError(f"runner 版本号不合法：{version!r}（应为 x.y.z）")
    return value


def _proxy_env(proxy: str | None) -> dict[str, str] | None:
    """Environment overrides for config.cmd and run.cmd, so the runner itself (registration,
    removal and the listener) reaches GitHub through the proxy. Only the overrides: installer /
    process build the full child environment from them (winproc.child_environment)."""
    value = (proxy or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = f"http://{value}"
    return {"https_proxy": value, "http_proxy": value}


# What the user is told was left alone when repo access could not be confirmed.
_REMOVE_KEPT = "没有删除任何东西：runner 目录和记录都保留着，换用有权限的 PAT 重新登录后再移除"
_IMPORT_KEPT = (
    "无法确认 runner 是否还在 GitHub 上；请不要删除该目录，换用有权限的 PAT 重新登录后再导入"
)


def _no_repo_access(owner: str, repo: str, cause: NotFoundError, kept: str) -> NotFoundError:
    where = f"{cause.method} {cause.path} -> 404" if cause.method and cause.path else "404"
    return NotFoundError(
        f"PAT 无法访问仓库 {owner}/{repo}（{where}）：仓库不存在，或这个 PAT 看不到它。"
        "fine-grained PAT 需要选中该仓库并授予 Administration: Read and write 权限，"
        f"classic PAT 需要 repo 权限。{kept}",
        status=cause.status,
        method=cause.method,
        path=cause.path,
    )


def _custom_labels(labels: Sequence[str]) -> list[str]:
    """Remote labels minus the automatic ones; labels RunnerDeck cannot represent are skipped."""
    kept: list[str] = []
    for label in labels:
        try:
            kept.extend(normalize_labels([label]))
        except InvalidInputError:
            log.warning("skipping label %r that RunnerDeck cannot represent", label)
    return normalize_labels(kept)


def _registration_happened(runner_dir: Path, error: BaseException) -> bool:
    """After a failed configure: did GitHub get a runner anyway?

    `.runner` written, or config.cmd printed a registration marker before failing.
    """
    try:
        if (runner_dir / runner_file.RUNNER_FILE).is_file():
            return True
    except OSError:
        pass
    output = error.output if isinstance(error, InstallError) else ""
    return any(m in line for m in installer.REGISTERED_MARKERS for line in output.splitlines())


def _download_reporter(report: ProgressFn, version: str) -> Callable[[int, int | None], None]:
    """Turn byte progress into at most ~11 Step.DOWNLOAD messages (every 10 %)."""
    last = -1

    def on_progress(done: int, total: int | None) -> None:
        nonlocal last
        if total:
            pct = min(100, done * 100 // total)
            bucket = pct // 10
            if bucket == last:
                return
            last = bucket
            report(
                Step.DOWNLOAD,
                f"下载 runner {version}：{pct}%（{done / _MB:.1f} / {total / _MB:.1f} MB）",
            )
        else:
            bucket = done // _DOWNLOAD_UNKNOWN_STEP
            if bucket == last:
                return
            last = bucket
            report(Step.DOWNLOAD, f"下载 runner {version}：已下载 {done / _MB:.1f} MB")

    return on_progress


class _ProcessIndex:
    """Runner processes looked up by directory, from at most one scan (taken lazily).

    scanner: the one-pass scan (RunnerManager's `scan_processes`). None means the legacy
    `find_processes` injection: one finder call per looked-up directory, as before M3.
    Lookups never raise: a failed scan is logged once and then counts as "no processes",
    because callers (status, discovery) only use the result as a hint.
    """

    def __init__(self, scanner: ProcessScanner | None, finder: ProcessFinder) -> None:
        self._scanner = scanner
        self._finder = finder
        self._found: list[RunnerProcessInfo] | None = None

    def _scan(self) -> list[RunnerProcessInfo]:
        if self._found is None:
            assert self._scanner is not None
            try:
                self._found = list(self._scanner())
            except (psutil.Error, OSError) as e:
                log.warning("cannot list runner processes: %s", e)
                self._found = []
        return self._found

    def pids_under(self, directory: Path) -> list[int]:
        """PIDs of runner processes whose executable lives under `directory`."""
        if self._scanner is None:
            try:
                return sorted({p.pid for p in self._finder(directory)})
            except (psutil.Error, OSError) as e:
                log.warning("cannot scan processes of %s: %s", directory, e)
                return []
        return sorted({p.pid for p in self._scan() if winproc.is_under(p.exe, directory)})


def is_service(runner_dir: str | os.PathLike[str]) -> bool:
    """The runner was installed as a Windows service (`.service` exists; stat() only)."""
    try:
        return (Path(runner_dir) / SERVICE_FILE).is_file()
    except OSError as e:
        log.warning("cannot stat %s: %s", Path(runner_dir) / SERVICE_FILE, e)
        return False


_SERVICE_REFUSED = "这个 runner 以 Windows 服务方式运行，请在“服务”里停止（服务模式 M4 支持）"


class RunnerManager:
    """Add / remove / import / scan / status flows with exact rollback.

    Thread safety: config.json read-modify-write sections are serialized by an internal lock,
    and add_runner calls are serialized as a whole (they share the download cache's `.part`
    file). Every method blocks; call them from a worker thread in the GUI. `progress`
    callbacks run in the calling thread; an exception raised by one aborts the operation
    (add_runner rolls back first), so it can be used for cancellation.
    """

    def __init__(
        self,
        store: ConfigStore,
        client_factory: ClientFactory,
        *,
        cache: ReleaseCache | None = None,
        hostname: str | None = None,
        installer_run: RunFn = installer.run_command,
        popen: PopenFn = subprocess.Popen,
        find_processes: ProcessFinder = process.running_processes,
        clock: Clock = _utcnow,
        stop_marks: StopMarks | None = None,
        scan_processes: ProcessScanner | None = None,
        stop_external: ExternalStopper = process.stop_external,
    ) -> None:
        """find_processes: runner processes of one directory (remove_runner's running check).
        scan_processes: all runner processes in one pass, grouped by directory for statuses()
        and discover(); default process.scan_runner_processes. When it is not given but
        find_processes is injected, statuses() / discover() fall back to one find_processes
        call per directory, so callers that only faked find_processes never scan for real.
        stop_marks: cross-process stop records (default StopMarks() under the local app dir).
        stop_external: hard kill of processes not owned by a RunnerProcess (kill_external,
        kill_stray); default process.stop_external.
        """
        self._store = store
        self._client_factory = client_factory
        self._cache = cache if cache is not None else ReleaseCache()
        self._hostname = hostname
        self._installer_run = installer_run
        self._popen = popen
        self._find_processes = find_processes
        if scan_processes is None and find_processes is process.running_processes:
            scan_processes = process.scan_runner_processes
        self._scan_processes = scan_processes
        self._stop_marks = stop_marks if stop_marks is not None else StopMarks()
        self._stop_external = stop_external
        self._clock = clock
        self._config_lock = threading.RLock()
        self._install_lock = threading.Lock()

    @property
    def hostname(self) -> str:
        return self._hostname or _local_hostname()

    def load_config(self) -> AppConfig:
        return self._store.load()

    # --- add ----------------------------------------------------------------------------------

    def plan_add(
        self,
        owner: str,
        repo: str,
        *,
        name: str | None = None,
        labels: Sequence[str] | None = None,
        root: str | os.PathLike[str] | None = None,
        version: str | None = None,
        runner_env: Mapping[str, str] | None = None,
    ) -> AddPlan:
        """Read-only preparation of add_runner.

        Looks the repo up (private flag for the public-repo warning, canonical spelling),
        fills in the default name `{hostname}-{repo}` and `settings.default_labels`, picks the
        target directory under `root` (default `settings.runners_root`) and checks GitHub for a
        runner with the same name. A local record with the same name in the same repo raises
        InvalidInputError (registering again would take over its registration).
        runner_env: validated here (installer's `.env` rules), written by add_runner.
        """
        owner, repo = validate_owner_repo(owner.strip(), repo.strip())
        version = _normalize_version(version)
        env_pairs = tuple(installer.validate_env_updates(runner_env or {}).items())
        config = self._store.load()
        with closing(self._client_factory()) as client:
            info = client.get_repo(owner, repo)
            try:
                owner, repo = validate_owner_repo(info.owner, info.name)
            except InvalidInputError:
                log.warning("GitHub returned an unexpected repo spelling: %s", info.full_name)
            runner_name = validate_runner_name(
                default_runner_name(self.hostname, repo) if name is None else name
            )
            if labels is None:
                labels = config.settings.default_labels
            label_list = normalize_labels(labels if isinstance(labels, str) else list(labels))
            self._check_local_conflict(config, owner, repo, runner_name)
            remotes = client.list_runners(owner, repo)

        folded = runner_name.casefold()
        remote_conflict = next((r for r in remotes if r.name.casefold() == folded), None)
        same_repo = tuple(r.name for r in config.runners if _same_repo(r, owner, repo))
        root_dir = Path(os.path.abspath(os.path.expanduser(root or config.settings.runners_root)))
        target = _pick_target_dir(root_dir, repo, {_path_key(r.path) for r in config.runners})

        plan = AddPlan(
            owner=owner,
            repo=repo,
            name=runner_name,
            labels=tuple(label_list),
            target_dir=target,
            private=info.private,
            archived=info.archived,
            version=version,
            remote_conflict=remote_conflict,
            same_repo_runners=same_repo,
            runner_env=env_pairs,
        )
        warnings: list[str] = []
        if not plan.private:
            warnings.append(
                f"{plan.full_name} 是公开仓库：任何人提交的 fork PR 都可能在这台电脑上执行代码。"
                f"建议在 {plan.actions_settings_url} 开启“fork PR 的工作流运行需要批准”，"
                "并且只让可信的工作流使用 self-hosted runner"
            )
        if plan.archived:
            warnings.append(f"{plan.full_name} 已归档，runner 不会收到任何 job")
        if remote_conflict is not None:
            warnings.append(
                f"GitHub 上已有同名 runner “{remote_conflict.name}”（id {remote_conflict.id}，"
                f"{remote_conflict.status}），继续会替换它，原来使用它的机器将无法再连接"
            )
        if same_repo:
            warnings.append(f"本机已为 {plan.full_name} 登记了 runner：{'、'.join(same_repo)}")
        return replace(plan, warnings=tuple(warnings))

    def add_runner(
        self, plan: AddPlan, *, auto_start: bool = False, progress: ProgressFn | None = None
    ) -> RunnerRecord:
        """Download -> verify -> extract -> registration token -> configure -> read .runner ->
        save the record (with `auto_start`: started whenever RunnerDeck starts).

        Rollback: once extraction succeeded, any failure deletes the directory. If GitHub got a
        registration (configure succeeded, or it failed after writing `.runner` / printing
        "Runner successfully added"), it is undone first: `config.cmd remove` with a fresh
        remove token, else DELETE by agentId (or by name). The record is written atomically as
        the very last step, so it never exists half-way. A rollback that cannot undo
        everything raises RollbackError (wrapping the original RunnerDeckError).
        """
        report = progress or _no_progress
        with self._install_lock:
            config = self._check_plan_still_valid(plan)
            child_env = _proxy_env(config.settings.proxy)
            with closing(self._client_factory()) as client:
                zip_path = self._prepare_package(client, plan, report)
                report(Step.EXTRACT, f"解压到 {plan.target_dir}")
                self._ensure_runners_root(plan.target_dir.parent)
                installer.extract_runner(zip_path, plan.target_dir)
                record = self._register_extracted(client, plan, report, child_env, auto_start)
            try:
                for pruned in self._cache.prune():
                    log.info("pruned old runner package %s", pruned.name)
            except Exception as e:  # housekeeping only; the runner is already added
                log.warning("pruning the runner package cache failed: %s", e)
        _report_safely(
            report, Step.DONE, f"已添加 runner {record.name}（GitHub id {record.github_runner_id}）"
        )
        log.info("added runner %s for %s at %s", record.name, record.full_name, record.path)
        return record

    @staticmethod
    def _ensure_runners_root(root: Path) -> None:
        """Create a missing runners root; a root RunnerDeck creates itself is hardened
        (installer.harden_root_dir, m4-design §6). An existing root is never touched here."""
        if root.exists():
            return
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise InstallError(f"无法创建 runner 根目录 {root}：{e.strerror or e}") from e
        if installer.root_hardening_refusal(root) is None:
            installer.harden_root_dir(root)  # never raises; a failure is only a warning

    def harden_runners_root(self) -> Path:
        """Give the configured runners root harden_root_dir's DACL (settings page button).

        InvalidInputError when the root must not be changed (drive root, profile, system
        directory) or off Windows; InstallError when icacls failed (details in the log).
        """
        root = self._runners_root(self._store.load())
        if not winproc.IS_WINDOWS:
            raise InvalidInputError("只有 Windows 支持加固目录权限")
        reason = installer.root_hardening_refusal(root)
        if reason is not None:
            raise InvalidInputError(reason)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise InstallError(f"无法创建 runner 根目录 {root}：{e.strerror or e}") from e
        if not installer.harden_root_dir(root):
            raise InstallError(f"加固 {root} 的权限失败，详见应用日志")
        return root

    def _prepare_package(self, client: GitHubClient, plan: AddPlan, report: ProgressFn) -> Path:
        report(Step.RELEASE, f"查询 runner 版本（{plan.version or '最新版'}）")
        info = release.parse_release(client.get_runner_release(plan.version))
        report(Step.DOWNLOAD, f"准备 runner {info.version} 安装包（{info.size / _MB:.1f} MB）")
        zip_path = self._cache.ensure(
            client, info, on_progress=_download_reporter(report, info.version)
        )
        report(Step.DOWNLOAD, f"安装包已就绪，SHA-256 校验通过：{zip_path.name}")
        return zip_path

    def _register_extracted(
        self,
        client: GitHubClient,
        plan: AddPlan,
        report: ProgressFn,
        child_env: Mapping[str, str] | None,
        auto_start: bool = False,
    ) -> RunnerRecord:
        """Everything after a successful extraction; rolls back on any failure.

        child_env: environment overrides (proxy) for config.cmd, also used by the rollback.
        """
        runner_dir = plan.target_dir
        registered = False
        agent_id: int | None = None
        record_id: str | None = None
        try:
            report(Step.TOKEN, "获取注册 token")
            token = client.create_registration_token(plan.owner, plan.repo)
            secret = token.token.get_secret_value()
            register_secret(secret)
            report(Step.CONFIGURE, "注册 runner（config.cmd，最长约 2 分钟）")
            try:
                installer.configure(
                    runner_dir,
                    url=plan.repo_url,
                    token=secret,
                    name=plan.name,
                    labels=plan.labels,
                    run=self._installer_run,
                    env=child_env,
                )
            except BaseException as e:
                registered = _registration_happened(runner_dir, e)
                raise
            registered = True

            env_updates = dict(plan.runner_env) | dict(child_env or {})
            if plan.runner_env:
                keys = ", ".join(env_updates)
                report(Step.ENV, f"写入 runner 环境变量（.env：{keys}）")
            if env_updates:  # the proxy alone has no wizard step: written silently
                installer.write_env_file(runner_dir, env_updates)

            report(Step.VERIFY, "读取 .runner")
            file_info = runner_file.read_runner_file(runner_dir)
            if file_info is None:
                raise InstallError(f"config.cmd 报告注册成功，但 {runner_dir} 下没有生成 .runner")
            agent_id = file_info.agent_id
            if file_info.agent_name.casefold() != plan.name.casefold():
                log.warning("registered as %r, planned %r", file_info.agent_name, plan.name)

            record = RunnerRecord(
                owner=plan.owner,
                repo=plan.repo,
                name=plan.name,
                labels=list(plan.labels),
                path=str(runner_dir),
                github_runner_id=agent_id,
                auto_start=auto_start,
            )
            record_id = record.id
            report(Step.SAVE, "保存 runner 记录")
            with self._config_lock:
                config = self._store.load()
                self._check_local_conflict(config, plan.owner, plan.repo, plan.name)
                config.upsert(record)
                self._store.save(config)
            return record
        except BaseException as e:
            problems = self._rollback_add(
                client,
                plan,
                e,
                registered=registered,
                agent_id=agent_id,
                record_id=record_id,
                report=report,
                child_env=child_env,
            )
            if problems and isinstance(e, RunnerDeckError):
                raise RollbackError(e, problems) from e
            if problems:
                log.error("rollback of %s incomplete: %s", plan.full_name, problems)
            raise

    def _rollback_add(
        self,
        client: GitHubClient,
        plan: AddPlan,
        error: BaseException,
        *,
        registered: bool,
        agent_id: int | None,
        record_id: str | None,
        report: ProgressFn,
        child_env: Mapping[str, str] | None,
    ) -> list[str]:
        """Undo add_runner after extraction. Returns what could not be undone (Chinese)."""
        log.warning("adding runner for %s failed (%s); rolling back", plan.full_name, error)
        problems: list[str] = []
        if record_id is not None:
            # Only reachable when saving itself failed; make sure no record survives.
            try:
                with self._config_lock:
                    config = self._store.load()
                    if config.remove(record_id) is not None:
                        self._store.save(config)
            except Exception as e:
                problems.append(f"无法确认 runner 记录已删除：{e}")
        if registered:
            problems.extend(
                self._unregister_after_failure(client, plan, agent_id, report, child_env)
            )
        _report_safely(report, Step.ROLLBACK, f"回滚：删除目录 {plan.target_dir}")
        try:
            installer.remove_runner_dir(plan.target_dir)
        except Exception as e:
            problems.append(f"目录 {plan.target_dir} 未能删除，请手动删除：{e}")
        return [redact(p) for p in problems]

    def _unregister_after_failure(
        self,
        client: GitHubClient,
        plan: AddPlan,
        agent_id: int | None,
        report: ProgressFn,
        child_env: Mapping[str, str] | None,
    ) -> list[str]:
        runner_dir = plan.target_dir
        if agent_id is None:
            with contextlib.suppress(RunnerDeckError):
                info = runner_file.read_runner_file(runner_dir)
                agent_id = info.agent_id if info is not None else None
        if runner_file.is_configured(runner_dir):
            _report_safely(report, Step.ROLLBACK, "回滚：注销刚注册的 runner（config.cmd remove）")
            try:
                token = client.create_remove_token(plan.owner, plan.repo)
                installer.unconfigure(
                    runner_dir,
                    token=token.token.get_secret_value(),
                    run=self._installer_run,
                    env=child_env,
                )
                log.info("rollback: unregistered %s with config.cmd remove", plan.name)
                return []
            except Exception as e:
                log.warning("rollback: config.cmd remove failed (%s); trying the API", e)
        _report_safely(report, Step.ROLLBACK, "回滚：通过 GitHub API 删除刚注册的 runner")
        try:
            if agent_id is None:
                remote = next(
                    (
                        r
                        for r in client.list_runners(plan.owner, plan.repo)
                        if r.name.casefold() == plan.name.casefold()
                    ),
                    None,
                )
                if remote is None:
                    log.info("rollback: no runner named %s on GitHub", plan.name)
                    return []
                agent_id = remote.id
            if client.delete_runner(plan.owner, plan.repo, agent_id):
                log.info("rollback: deleted runner %s (id %s) via the API", plan.name, agent_id)
            else:
                self._confirm_gone(client, plan.owner, plan.repo, agent_id, kept="")
                log.info("rollback: runner %s (id %s) already gone", plan.name, agent_id)
            return []
        except Exception as e:
            return [
                f"GitHub 上可能残留 runner “{plan.name}”，请到 "
                f"{plan.repo_url}/settings/actions/runners 手动删除（{e}）"
            ]

    def _check_plan_still_valid(self, plan: AddPlan) -> AppConfig:
        """Re-validate the plan against the current config; returns that config."""
        validate_owner_repo(plan.owner, plan.repo)
        validate_runner_name(plan.name)
        if not plan.target_dir.is_absolute():
            raise InvalidInputError(f"目标目录必须是绝对路径：{plan.target_dir}")
        config = self._store.load()
        self._check_local_conflict(config, plan.owner, plan.repo, plan.name)
        self._check_dir_unused(config, plan.target_dir)
        return config

    @staticmethod
    def _check_local_conflict(config: AppConfig, owner: str, repo: str, name: str) -> None:
        folded = name.casefold()
        for record in config.runners:
            if _same_repo(record, owner, repo) and record.name.casefold() == folded:
                raise InvalidInputError(
                    f"本机已有同名 runner “{record.name}”"
                    f"（{record.full_name}，目录 {record.path}），请换一个名称"
                )

    @staticmethod
    def _check_dir_unused(config: AppConfig, target: Path) -> None:
        key = _path_key(target)
        for record in config.runners:
            if _path_key(record.path) == key:
                raise InvalidInputError(
                    f"目录 {target} 已被 runner “{record.name}” 使用，请重新规划添加"
                )

    # --- remove ---------------------------------------------------------------------------------

    def remove_runner(
        self,
        key: str,
        *,
        keep_work: bool = False,
        local_only: bool = False,
        progress: ProgressFn | None = None,
    ) -> RemoveResult:
        """Unregister and delete a runner, then its record.

        Refuses (ProcessError) while runner processes live in its directory. Reads `.runner`
        before `config.cmd remove` (which deletes it, spike §3); if remove fails or the
        directory is gone, falls back to the API DELETE by agentId (or by name). A 404 counts
        as already removed only once list_runners proved the PAT can see the repo; otherwise
        NotFoundError ("PAT 无法访问仓库"). A GitHub-side failure aborts with nothing deleted
        locally. A directory that cannot be deleted becomes a warning; the record is deleted
        last. config.cmd gets the `settings.proxy` environment.

        local_only=True skips GitHub entirely (remote=SKIPPED) and only deletes the directory
        and the record: the way out when the repo was deleted or the PAT lost access, where
        every GitHub call answers 404. A registration that still exists is left behind (warned).
        """
        report = progress or _no_progress
        config = self._store.load()
        record = resolve_runner(config, key)
        child_env = _proxy_env(config.settings.proxy)
        runner_dir = Path(record.path)

        report(Step.CHECK, f"检查 runner {record.name} 是否在运行")
        pids = self._runner_pids(runner_dir)
        if pids:
            raise ProcessError(
                f"runner “{record.name}” 正在运行（PID {', '.join(map(str, pids))}），"
                "请先停止再移除"
            )

        warnings: list[str] = []
        file_info = None
        dir_exists = runner_dir.is_dir()
        if dir_exists:
            report(Step.READ, "读取 .runner")
            try:
                file_info = runner_file.read_runner_file(runner_dir)
            except RunnerFileError as e:
                warnings.append(f"无法读取 .runner，改用记录里的 runner id：{e}")
        runner_id = record.github_runner_id
        if file_info is not None:
            if runner_id is not None and runner_id != file_info.agent_id:
                log.warning(
                    "record says runner id %s, .runner says %s; using .runner",
                    runner_id,
                    file_info.agent_id,
                )
            runner_id = file_info.agent_id

        remote: RemoteRemoval | None = None
        if local_only:
            remote = RemoteRemoval.SKIPPED
            warnings.append(
                "没有在 GitHub 上注销；如果 runner 还在，请到 "
                f"{record.repo_url}/settings/actions/runners 手动删除"
            )
        else:
            remote, runner_id = self._remove_remote(
                record, runner_dir, dir_exists, runner_id, child_env, warnings, report
            )

        dir_removed = False
        if os.path.lexists(runner_dir):
            report(
                Step.DELETE_DIR,
                f"删除目录 {runner_dir}" + ("（保留 _work）" if keep_work else ""),
            )
            try:
                installer.remove_runner_dir(runner_dir, keep_work=keep_work)
                dir_removed = True
            except RunnerDeckError as e:
                warnings.append(f"目录未删除，请手动处理：{e}")

        report(Step.SAVE, "删除 runner 记录")
        with self._config_lock:
            config = self._store.load()
            config.remove(record.id)
            self._store.save(config)
        self._stop_marks.clear(runner_dir)
        _report_safely(report, Step.DONE, f"已移除 runner {record.name}")
        log.info("removed runner %s (%s, %s)", record.name, record.full_name, remote)
        return RemoveResult(
            record=record,
            remote=remote,
            runner_id=runner_id if remote is not RemoteRemoval.UNCONFIGURED else None,
            dir_removed=dir_removed,
            warnings=tuple(redact(w) for w in warnings),
        )

    def _remove_remote(
        self,
        record: RunnerRecord,
        runner_dir: Path,
        dir_exists: bool,
        runner_id: int | None,
        child_env: Mapping[str, str] | None,
        warnings: list[str],
        report: ProgressFn,
    ) -> tuple[RemoteRemoval, int | None]:
        """GitHub half of remove_runner: config.cmd remove, else the API DELETE."""
        remote: RemoteRemoval | None = None
        with closing(self._client_factory()) as client:
            if dir_exists and runner_file.is_configured(runner_dir):
                report(Step.UNCONFIGURE, "注销 runner（config.cmd remove）")
                try:
                    token = client.create_remove_token(record.owner, record.repo)
                    installer.unconfigure(
                        runner_dir,
                        token=token.token.get_secret_value(),
                        run=self._installer_run,
                        env=child_env,
                    )
                    remote = RemoteRemoval.UNCONFIGURED
                except RunnerDeckError as e:
                    if isinstance(e, NotFoundError):
                        # Usually the PAT cannot see the repo; the API fallback would get the
                        # same 404 and could not tell it from "already gone". Stop here then.
                        self._confirm_repo_access(client, record.owner, record.repo, _REMOVE_KEPT)
                    log.warning("config.cmd remove failed for %s: %s", record.name, e)
                    warnings.append(f"config.cmd remove 失败，已改用 GitHub API 删除：{e}")
            if remote is None:
                report(Step.API_DELETE, "通过 GitHub API 删除 runner")
                remote, runner_id = self._delete_remote(client, record, runner_id)
        return remote, runner_id

    @classmethod
    def _delete_remote(
        cls, client: GitHubClient, record: RunnerRecord, runner_id: int | None
    ) -> tuple[RemoteRemoval, int | None]:
        owner, repo = record.owner, record.repo
        if runner_id is None:
            match = match_remote(
                record, cls._confirm_repo_access(client, owner, repo, _REMOVE_KEPT)
            )
            if match is None:
                return RemoteRemoval.ALREADY_GONE, None
            runner_id = match.id
        try:
            deleted = client.delete_runner(owner, repo, runner_id)
        except NotFoundError as e:
            raise _no_repo_access(owner, repo, e, _REMOVE_KEPT) from e
        except GitHubError as e:
            # Real (2026-10-23): 422 "... is currently running a job and cannot be deleted",
            # e.g. a job orphaned by a hard kill keeps the runner busy on GitHub.
            if e.status == 422:
                raise GitHubError(
                    f"GitHub 认为 runner “{record.name}” 还在执行 job"
                    "（常见于 job 执行中被强制结束），"
                    f"暂时不能删除；请在 {record.repo_url}/actions 找到那个仍在运行的 run，"
                    "取消它（不生效时用“Force cancel”）后重试。本地目录和记录都没有改动",
                    status=422,
                    method=e.method,
                    path=e.path,
                ) from e
            raise
        if deleted:
            return RemoteRemoval.API_DELETED, runner_id
        cls._confirm_gone(client, owner, repo, runner_id, kept=_REMOVE_KEPT)
        return RemoteRemoval.ALREADY_GONE, runner_id

    @staticmethod
    def _confirm_repo_access(
        client: GitHubClient, owner: str, repo: str, kept: str
    ) -> list[RemoteRunner]:
        """The repo's runners; a 404 means the PAT cannot see the repo -> NotFoundError.

        GitHub answers 404 for runners that are gone AND for repos the PAT cannot see. A
        successful list on the same repo is what turns a 404 into "gone". `kept` tells the
        user what was left alone.
        """
        try:
            return client.list_runners(owner, repo)
        except NotFoundError as e:
            raise _no_repo_access(owner, repo, e, kept) from e

    @classmethod
    def _confirm_gone(
        cls, client: GitHubClient, owner: str, repo: str, runner_id: int, *, kept: str
    ) -> None:
        """After a 404 for runner `runner_id`: raise unless the repo is visible without it."""
        if any(r.id == runner_id for r in cls._confirm_repo_access(client, owner, repo, kept)):
            raise RunnerDeckError(
                f"GitHub 删除 runner（{owner}/{repo}，id {runner_id}）时返回 404，"
                f"但仓库的 runner 列表里仍有它，请稍后重试。{kept}"
            )

    def _runner_pids(self, runner_dir: Path) -> list[int]:
        try:
            return [p.pid for p in self._find_processes(runner_dir)]
        except (psutil.Error, OSError) as e:
            raise ProcessError(f"无法检查 {runner_dir} 下的 runner 进程：{e}") from e

    # --- import / scan --------------------------------------------------------------------------

    def import_runner(
        self, path: str | os.PathLike[str], *, auto_start: bool = False
    ) -> RunnerRecord:
        """Record an already configured runner directory (e.g. one set up by hand).

        auto_start: for a new record only. Same path already recorded -> that record as it is
        (no network). Otherwise reads `.runner`, maps
        `gitHubUrl` to owner/repo and asks GitHub whether the runner still exists (missing ->
        RunnerDeckError, but only once list_runners proved the repo is visible; a repo the PAT
        cannot see -> NotFoundError). A recorded runner with the same GitHub id whose directory
        no longer exists is treated as moved: its path is updated. If its directory still
        exists, two directories claim one registration -> InvalidInputError.
        """
        runner_dir = Path(os.path.abspath(Path(path).expanduser()))
        existing = self._record_for_path(self._store.load(), runner_dir)
        if existing is not None:
            return existing
        if not runner_dir.is_dir():
            raise InvalidInputError(f"目录不存在：{runner_dir}")
        if not runner_file.looks_like_runner_dir(runner_dir):
            raise InvalidInputError(f"{runner_dir} 不是 runner 目录（缺少 config.cmd 或 run.cmd）")
        info = runner_file.read_runner_file(runner_dir)
        if info is None or not runner_file.is_configured(runner_dir):
            raise InvalidInputError(
                f"{runner_dir} 还没有注册（缺少 .runner 或 .credentials），请用添加功能重新注册"
            )
        owner, repo = runner_file.parse_github_url(info.github_url)
        with closing(self._client_factory()) as client:
            try:
                remote = client.get_runner(owner, repo, info.agent_id)
            except NotFoundError as e:
                raise _no_repo_access(owner, repo, e, _IMPORT_KEPT) from e
            if remote is None:
                # 404: deleted, or the repo is invisible to this PAT. Never advise deleting a
                # directory that may hold a live registration before that is ruled out.
                listed = self._confirm_repo_access(client, owner, repo, _IMPORT_KEPT)
                remote = next((r for r in listed if r.id == info.agent_id), None)
        if remote is None:
            raise RunnerDeckError(
                f"GitHub 上已没有 runner “{info.agent_name}”"
                f"（{owner}/{repo}，id {info.agent_id}），无法导入；请删除该目录后重新添加"
            )
        if remote.name.casefold() != info.agent_name.casefold():
            log.warning("GitHub name %r differs from .runner %r", remote.name, info.agent_name)

        with self._config_lock:
            config = self._store.load()
            existing = self._record_for_path(config, runner_dir)
            if existing is not None:
                return existing
            for record in config.runners:
                if not (_same_repo(record, owner, repo) and record.github_runner_id == remote.id):
                    continue
                if os.path.lexists(record.path):
                    raise InvalidInputError(
                        f"这个 runner 已登记为 “{record.name}”（目录 {record.path}），"
                        "两个目录不能共用同一个注册"
                    )
                moved = record.model_copy(update={"path": str(runner_dir)})
                config.upsert(moved)
                self._store.save(config)
                log.info("runner %s moved: %s -> %s", record.name, record.path, runner_dir)
                return moved
            record = RunnerRecord(
                owner=owner,
                repo=repo,
                name=info.agent_name,
                labels=_custom_labels(remote.labels),
                path=str(runner_dir),
                github_runner_id=remote.id,
                auto_start=auto_start,
            )
            config.upsert(record)
            self._store.save(config)
        log.info("imported runner %s (%s) from %s", record.name, record.full_name, runner_dir)
        return record

    def scan_unregistered(self) -> list[Path]:
        """Configured runner directories directly under runners_root that have no record."""
        return self._unregistered(self._store.load())

    @staticmethod
    def _runners_root(config: AppConfig) -> Path:
        return Path(os.path.abspath(Path(config.settings.runners_root).expanduser()))

    def _unregistered(self, config: AppConfig) -> list[Path]:
        known = {_path_key(r.path) for r in config.runners}
        root = self._runners_root(config)
        return [p for p in runner_file.scan_runner_dirs(root) if _path_key(p) not in known]

    def discover(self) -> Discovery:
        """What lives under runners_root without a record (one process scan).

        unregistered: scan_unregistered(). strays: runner processes whose executable is inside
        a direct subdirectory of the root that has no record (configured: `.runner` and
        `.credentials` exist; service: installed as a Windows service). A failed process scan
        only means no strays (logged); an unreadable root raises RunnerDeckError.
        """
        config = self._store.load()
        root = self._runners_root(config)
        unregistered = self._unregistered(config)
        known = {_path_key(r.path) for r in config.runners}
        index = self._process_index()
        strays: list[StrayRunner] = []
        if index.pids_under(root):
            for child in self._child_dirs(root):
                if _path_key(child) in known or self._holds_record(config, child):
                    continue  # recorded (or holds a recorded runner deeper down): not a stray
                pids = index.pids_under(child)
                if pids:
                    strays.append(
                        StrayRunner(
                            path=child,
                            pids=tuple(pids),
                            configured=runner_file.is_configured(child),
                            service=is_service(child),
                        )
                    )
        return Discovery(root=root, unregistered=tuple(unregistered), strays=tuple(strays))

    @staticmethod
    def _child_dirs(root: Path) -> list[Path]:
        try:
            children = list(root.iterdir())
        except FileNotFoundError:
            return []
        except OSError as e:
            raise RunnerDeckError(f"无法读取目录 {root}：{e.strerror or e}") from e
        dirs: list[Path] = []
        for child in children:
            try:
                if child.is_dir():
                    dirs.append(child)
            except OSError as e:
                log.warning("cannot stat %s: %s", child, e)
        dirs.sort(key=lambda p: (p.name.casefold(), p.name))
        return dirs

    is_service = staticmethod(is_service)

    # --- record-only changes ----------------------------------------------------------------

    def _update_record(self, record_id: str, **changes: object) -> RunnerRecord:
        with self._config_lock:
            config = self._store.load()
            record = self._require_record(config, record_id)
            updated = record.model_copy(update=changes)
            if updated != record:
                config.upsert(updated)
                self._store.save(config)
            return updated

    @staticmethod
    def _require_record(config: AppConfig, record_id: str) -> RunnerRecord:
        record = config.get(record_id)
        if record is None:
            raise RunnerNotFoundError(f"找不到 runner 记录 {record_id[:8]}（可能已被移除）")
        return record

    def set_auto_start(self, record_id: str, enabled: bool) -> RunnerRecord:
        """Turn "start together with RunnerDeck" on or off for one record."""
        return self._update_record(record_id, auto_start=bool(enabled))

    def forget_runner(self, record_id: str) -> RunnerRecord:
        """Delete only the record: no GitHub call, the directory and `.runner` stay untouched.

        For an imported directory the user keeps managing elsewhere; it shows up again in
        scan_unregistered() / discover() and can be imported later. Its stop record is kept
        (it expires by itself) so a re-import right after a hard kill still knows about it.
        """
        with self._config_lock:
            config = self._store.load()
            record = self._require_record(config, record_id)
            config.remove(record.id)
            self._store.save(config)
        log.info(
            "forgot runner %s (%s), directory kept: %s", record.name, record.full_name, record.path
        )
        return record

    # --- processes not owned by a RunnerProcess -----------------------------------------------

    def record_stop(
        self, runner_dir: str | os.PathLike[str], *, forced: bool, at: datetime | None = None
    ) -> None:
        """Remember how `runner_dir` stopped (default time: this manager's clock). Never raises."""
        try:
            self._stop_marks.record(runner_dir, forced=forced, at=at or self._clock())
        except Exception:  # a hint only: never let it break a stop flow
            log.exception("recording the stop of %s failed", runner_dir)

    def kill_external(self, record_id: str) -> ExternalStop:
        """Hard-kill the runner processes in a recorded runner's directory that are not ours
        (a CLI `start` in another terminal, a double-clicked run.cmd).

        Service mode -> InvalidInputError (stop it in services.msc). Something killed -> a
        forced stop record, so the status shows the lingering GitHub session. Check
        ExternalStop.ok: survivors mean the runner is still alive. Raises ProcessError if the
        process scan fails.
        """
        record = self._require_record(self._store.load(), record_id)
        return self._kill_dir(Path(record.path))

    def kill_stray(self, path: str | os.PathLike[str]) -> ExternalStop:
        """Like kill_external, for a stray: `path` must be a direct subdirectory of
        runners_root that has no record (InvalidInputError otherwise)."""
        runner_dir = Path(os.path.abspath(Path(path).expanduser()))
        config = self._store.load()
        root = self._runners_root(config)
        if _path_key(runner_dir.parent) != _path_key(root):
            raise InvalidInputError(
                f"{runner_dir} 不在 runner 根目录 {root} 下，不会结束其中的进程"
            )
        existing = self._record_for_path(config, runner_dir)
        if existing is not None:
            raise InvalidInputError(
                f"{runner_dir} 已登记为 runner “{existing.name}”，请在它的卡片上操作"
            )
        nested = self._holds_record(config, runner_dir)
        if nested is not None:
            # stop_external would also kill that runner, which may be ours and mid-job.
            raise InvalidInputError(
                f"{runner_dir} 下有已登记的 runner “{nested.name}”（{nested.path}），"
                "不会结束其中的进程"
            )
        return self._kill_dir(runner_dir)

    @staticmethod
    def _holds_record(config: AppConfig, directory: Path) -> RunnerRecord | None:
        """A record whose directory lies inside `directory` (not equal to it)."""
        key = _path_key(directory)
        for record in config.runners:
            if _path_key(record.path) != key and winproc.is_under(record.path, directory):
                return record
        return None

    def _kill_dir(self, runner_dir: Path) -> ExternalStop:
        if is_service(runner_dir):
            raise InvalidInputError(_SERVICE_REFUSED)
        try:
            outcome = self._stop_external(runner_dir)
        except (psutil.Error, OSError) as e:
            raise ProcessError(f"无法结束 {runner_dir} 下的 runner 进程：{e}") from e
        if outcome.killed:
            self.record_stop(runner_dir, forced=True)
        return outcome

    @staticmethod
    def _record_for_path(config: AppConfig, runner_dir: Path) -> RunnerRecord | None:
        key = _path_key(runner_dir)
        return next((r for r in config.runners if _path_key(r.path) == key), None)

    # --- status / processes ----------------------------------------------------------------------

    def statuses(
        self, processes: Mapping[str, RunnerProcess] | None = None, *, remote: bool = True
    ) -> list[RunnerStatusRow]:
        """Merged local + GitHub status for every record, in config order.

        processes: RunnerProcess objects owned by the caller, keyed by record id. Records
        without one count as not running (external if runner processes live in the dir).
        At most one process scan per call, only if some record has no running process.
        The last stop comes from the RunnerProcess or from the stop records (record_stop),
        whichever is newer.
        GitHub is asked once per repo; a repo whose list fails (or no client at all, e.g. not
        logged in) shows remote UNKNOWN with `remote_error`, without affecting other repos.
        remote=False: no client is built and GitHub is not asked; remote is UNKNOWN and
        remote_error None for every row.
        """
        config = self._store.load()
        if not config.runners:
            return []
        now = self._clock()
        if remote:
            lists, errors = self._fetch_remote_lists(config.runners)
        else:
            lists, errors = {}, {}
        index = self._process_index()
        rows: list[RunnerStatusRow] = []
        for record in config.runners:
            proc = processes.get(record.id) if processes else None
            local = self._local_snapshot(record, proc, now, index)
            key = _repo_key(record.owner, record.repo)
            remote_runner: RemoteRunner | None = None
            if key in lists:
                remote_runner = match_remote(record, lists[key])
                snapshot = RemoteSnapshot.from_remote(remote_runner, fetched_at=now)
            else:
                snapshot = RemoteSnapshot(RemoteStatus.UNKNOWN)
            rows.append(
                RunnerStatusRow(
                    record=record,
                    display=merge_status(local, snapshot, now=now),
                    local=local,
                    remote=snapshot,
                    remote_runner=remote_runner,
                    remote_error=errors.get(key),
                )
            )
        return rows

    def _fetch_remote_lists(
        self, records: Sequence[RunnerRecord]
    ) -> tuple[dict[tuple[str, str], list[RemoteRunner]], dict[tuple[str, str], str]]:
        groups: dict[tuple[str, str], RunnerRecord] = {}
        for record in records:
            groups.setdefault(_repo_key(record.owner, record.repo), record)
        lists: dict[tuple[str, str], list[RemoteRunner]] = {}
        errors: dict[tuple[str, str], str] = {}
        try:
            client = self._client_factory()
        except RunnerDeckError as e:
            log.warning("no GitHub client for status: %s", e)
            return lists, dict.fromkeys(groups, str(e))
        with closing(client):
            for key, record in groups.items():
                try:
                    lists[key] = client.list_runners(record.owner, record.repo)
                except RunnerDeckError as e:
                    log.warning("listing runners of %s failed: %s", record.full_name, e)
                    errors[key] = str(e)
        return lists, errors

    def _process_index(self) -> _ProcessIndex:
        return _ProcessIndex(self._scan_processes, self._find_processes)

    def _local_snapshot(
        self,
        record: RunnerRecord,
        proc: RunnerProcess | None,
        now: datetime,
        index: _ProcessIndex,
    ) -> LocalSnapshot:
        """Local view of `record`. The last stop: the RunnerProcess's when it has one, the
        stop record otherwise (app restarted, CLI `stop` from another terminal), the newer
        of the two when both exist. A running process ignores stop records."""
        runner_dir = Path(record.path)
        if proc is not None and proc.is_running():
            return LocalSnapshot(
                state=proc.state,
                running=True,
                external=False,
                state_since=proc.state_since,
                last_stop_forced=proc.last_stop_forced,
                stopped_at=proc.stopped_at,
                current_job=proc.current_job,
                last_error=proc.last_error,
            )
        forced, stopped_at = False, None
        if proc is not None:
            forced, stopped_at = proc.last_stop_forced, proc.stopped_at
        mark = self._stop_marks.get(runner_dir, now=now)
        if mark is not None and (stopped_at is None or mark.stopped_at > stopped_at):
            forced, stopped_at = mark.forced, mark.stopped_at
        external = bool(index.pids_under(runner_dir))
        if proc is None:
            return LocalSnapshot(
                state=LocalState.STOPPED,
                running=False,
                external=external,
                state_since=now,
                last_stop_forced=forced,
                stopped_at=stopped_at,
            )
        return LocalSnapshot(
            state=proc.state,
            running=False,
            external=external,
            state_since=proc.state_since,
            last_stop_forced=forced,
            stopped_at=stopped_at,
            current_job=proc.current_job,
            last_error=proc.last_error,
        )

    @staticmethod
    def _sync_proxy_env(record: RunnerRecord, proxy: str | None) -> None:
        """Before a start: put settings.proxy into `.env` where a managed key is missing
        (runners added before M4, imported ones). Values already there are left alone:
        apply_proxy migrates ours, the others are the user's. Best effort, never raises."""
        wanted = _proxy_env(proxy)
        if not wanted:
            return
        runner_dir = Path(record.path)
        try:
            current = installer.read_env_values(runner_dir)
            missing = {k: v for k, v in wanted.items() if k.casefold() not in current}
            if missing:
                installer.write_env_file(runner_dir, missing)
        except RunnerDeckError as e:
            log.warning("could not write the proxy into %s/.env: %s", runner_dir, e)

    def apply_proxy(self, old: str | None, new: str | None) -> ProxyApplyResult:
        """settings.proxy changed from `old` to `new`: rewrite each runner's managed `.env`
        keys that are missing or still hold `old` (deleted when `new` is empty). A key with
        any other value is the user's own and is left alone (reported in `custom`)."""
        before, after = _proxy_env(old) or {}, _proxy_env(new) or {}
        updated: list[str] = []
        custom: list[str] = []
        failed: list[tuple[str, str]] = []
        for record in self._store.load().runners:
            runner_dir = Path(record.path)
            if not runner_dir.is_dir():
                continue
            try:
                current = installer.read_env_values(runner_dir)
                changes: dict[str, str | None] = {}
                kept = False
                for key in PROXY_ENV_KEYS:
                    value = current.get(key.casefold())
                    target = after.get(key)
                    if value == target:
                        continue
                    if value is None or value == before.get(key):
                        changes[key] = target
                    else:
                        kept = True
                if changes:
                    installer.write_env_file(runner_dir, changes)
                    updated.append(record.name)
                if kept:
                    custom.append(record.name)
            except RunnerDeckError as e:
                failed.append((record.name, str(e)))
        log.info(
            "proxy applied: %d updated, %d custom, %d failed", *map(len, (updated, custom, failed))
        )
        return ProxyApplyResult(tuple(updated), tuple(custom), tuple(failed))

    def process_for(
        self,
        record: RunnerRecord,
        *,
        on_line: Callable[[str], None] | None = None,
        on_state: Callable[[LocalState, str | None], None] | None = None,
        on_job_finished: Callable[[str, str], None] | None = None,
        max_lines: int = 2000,
        launch: Launch = Launch.CONSOLE,
    ) -> RunnerProcess:
        """A (not yet started) RunnerProcess for `record`, using the injected popen.

        With `settings.proxy` set, run.cmd gets https_proxy / http_proxy so the runner reaches
        GitHub through it. Only these overrides are passed; RunnerProcess builds the full child
        environment (without secrets such as RUNNERDECK_PAT) itself. Callbacks run on
        background threads (see RunnerProcess). launch: Launch.HELPER for a caller without a
        console (the GUI), see process.Launch.
        """
        proxy = self._store.load().settings.proxy
        self._sync_proxy_env(record, proxy)
        return RunnerProcess(
            Path(record.path),
            env=_proxy_env(proxy),
            on_line=on_line,
            on_state=on_state,
            on_job_finished=on_job_finished,
            popen=self._popen,
            max_lines=max_lines,
            launch=launch,
        )
