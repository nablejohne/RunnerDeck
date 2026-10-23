"""Business flows on top of the core modules: add / remove / import / scan / status / process.

The CLI (M1) and the Qt controller (M2) both call this module; the controller only moves the
calls onto worker threads. Every public method blocks (network, config.cmd, disk) and reports
progress through an optional `progress(step, text)` callback that runs in the calling thread.

Grounded in docs/spike-notes.md:
- §3: `config.cmd remove` deletes `.runner`, so remove reads `agentId` first and keeps it for the
  `DELETE /actions/runners/{id}` fallback.
- §5: a hard kill leaves the GitHub session alive for minutes; status merging lives in status.py.
Registration and remove tokens live about an hour: they are fetched right before config.cmd
runs and never cached.
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

import psutil

from runnerdeck.core import installer, process, release, runner_file
from runnerdeck.core.errors import (
    InstallError,
    InvalidInputError,
    ProcessError,
    RunnerDeckError,
    RunnerFileError,
)
from runnerdeck.core.github_client import GitHubClient
from runnerdeck.core.installer import RunFn
from runnerdeck.core.models import (
    AppConfig,
    LocalState,
    RemoteRunner,
    RemoteStatus,
    RunnerRecord,
    default_runner_name,
    normalize_labels,
    validate_owner_repo,
    validate_runner_name,
)
from runnerdeck.core.process import PopenFn, RunnerProcess
from runnerdeck.core.redact import redact, register_secret
from runnerdeck.core.release import ReleaseCache
from runnerdeck.core.status import (
    DisplayStatus,
    LocalSnapshot,
    RemoteSnapshot,
    match_remote,
    merge_status,
)
from runnerdeck.core.store import ConfigStore, resolve_runner

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, str], None]  # (step id, Chinese description), calling thread
ClientFactory = Callable[[], GitHubClient]
ProcessFinder = Callable[[Path], Sequence[psutil.Process]]
Clock = Callable[[], datetime]

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
_MB = 1024 * 1024
_DOWNLOAD_UNKNOWN_STEP = 10 * _MB  # progress granularity when the size is unknown


class Step(StrEnum):
    """Progress step ids passed as the first argument of ProgressFn."""

    # add_runner
    RELEASE = "release"
    DOWNLOAD = "download"
    EXTRACT = "extract"
    TOKEN = "token"
    CONFIGURE = "configure"
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
    ALREADY_GONE = "already_gone"  # 404, or no runner with that name in the repo's list


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


def _path_key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


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
    """Environment for run.cmd so the runner itself reaches GitHub through the proxy."""
    value = (proxy or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = f"http://{value}"
    return {"https_proxy": value, "http_proxy": value}


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
    ) -> None:
        self._store = store
        self._client_factory = client_factory
        self._cache = cache if cache is not None else ReleaseCache()
        self._hostname = hostname
        self._installer_run = installer_run
        self._popen = popen
        self._find_processes = find_processes
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
    ) -> AddPlan:
        """Read-only preparation of add_runner.

        Looks the repo up (private flag for the public-repo warning, canonical spelling),
        fills in the default name `{hostname}-{repo}` and `settings.default_labels`, picks the
        target directory under `root` (default `settings.runners_root`) and checks GitHub for a
        runner with the same name. A local record with the same name in the same repo raises
        InvalidInputError (registering again would take over its registration).
        """
        owner, repo = validate_owner_repo(owner.strip(), repo.strip())
        version = _normalize_version(version)
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

    def add_runner(self, plan: AddPlan, *, progress: ProgressFn | None = None) -> RunnerRecord:
        """Download -> verify -> extract -> registration token -> configure -> read .runner ->
        save the record.

        Rollback: once extraction succeeded, any failure deletes the directory. If GitHub got a
        registration (configure succeeded, or it failed after writing `.runner` / printing
        "Runner successfully added"), it is undone first: `config.cmd remove` with a fresh
        remove token, else DELETE by agentId (or by name). The record is written atomically as
        the very last step, so it never exists half-way. A rollback that cannot undo
        everything raises RollbackError (wrapping the original RunnerDeckError).
        """
        report = progress or _no_progress
        with self._install_lock:
            self._check_plan_still_valid(plan)
            with closing(self._client_factory()) as client:
                zip_path = self._prepare_package(client, plan, report)
                report(Step.EXTRACT, f"解压到 {plan.target_dir}")
                installer.extract_runner(zip_path, plan.target_dir)
                record = self._register_extracted(client, plan, report)
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
        self, client: GitHubClient, plan: AddPlan, report: ProgressFn
    ) -> RunnerRecord:
        """Everything after a successful extraction; rolls back on any failure."""
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
                )
            except BaseException as e:
                registered = _registration_happened(runner_dir, e)
                raise
            registered = True

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
            problems.extend(self._unregister_after_failure(client, plan, agent_id, report))
        _report_safely(report, Step.ROLLBACK, f"回滚：删除目录 {plan.target_dir}")
        try:
            installer.remove_runner_dir(plan.target_dir)
        except Exception as e:
            problems.append(f"目录 {plan.target_dir} 未能删除，请手动删除：{e}")
        return [redact(p) for p in problems]

    def _unregister_after_failure(
        self, client: GitHubClient, plan: AddPlan, agent_id: int | None, report: ProgressFn
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
                    runner_dir, token=token.token.get_secret_value(), run=self._installer_run
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
            client.delete_runner(plan.owner, plan.repo, agent_id)
            log.info("rollback: deleted runner %s (id %s) via the API", plan.name, agent_id)
            return []
        except Exception as e:
            return [
                f"GitHub 上可能残留 runner “{plan.name}”，请到 "
                f"{plan.repo_url}/settings/actions/runners 手动删除（{e}）"
            ]

    def _check_plan_still_valid(self, plan: AddPlan) -> None:
        validate_owner_repo(plan.owner, plan.repo)
        validate_runner_name(plan.name)
        if not plan.target_dir.is_absolute():
            raise InvalidInputError(f"目标目录必须是绝对路径：{plan.target_dir}")
        config = self._store.load()
        self._check_local_conflict(config, plan.owner, plan.repo, plan.name)
        self._check_dir_unused(config, plan.target_dir)

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
        self, key: str, *, keep_work: bool = False, progress: ProgressFn | None = None
    ) -> RemoveResult:
        """Unregister and delete a runner, then its record.

        Refuses (ProcessError) while runner processes live in its directory. Reads `.runner`
        before `config.cmd remove` (which deletes it, spike §3); if remove fails or the
        directory is gone, falls back to the API DELETE by agentId (or by name), where 404
        counts as already removed. A GitHub-side failure aborts with nothing deleted locally.
        A directory that cannot be deleted becomes a warning; the record is deleted last.
        """
        report = progress or _no_progress
        record = resolve_runner(self._store.load(), key)
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
        with closing(self._client_factory()) as client:
            if dir_exists and runner_file.is_configured(runner_dir):
                report(Step.UNCONFIGURE, "注销 runner（config.cmd remove）")
                try:
                    token = client.create_remove_token(record.owner, record.repo)
                    installer.unconfigure(
                        runner_dir,
                        token=token.token.get_secret_value(),
                        run=self._installer_run,
                    )
                    remote = RemoteRemoval.UNCONFIGURED
                except RunnerDeckError as e:
                    log.warning("config.cmd remove failed for %s: %s", record.name, e)
                    warnings.append(f"config.cmd remove 失败，已改用 GitHub API 删除：{e}")
            if remote is None:
                report(Step.API_DELETE, "通过 GitHub API 删除 runner")
                remote, runner_id = self._delete_remote(client, record, runner_id)

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
        _report_safely(report, Step.DONE, f"已移除 runner {record.name}")
        log.info("removed runner %s (%s, %s)", record.name, record.full_name, remote)
        return RemoveResult(
            record=record,
            remote=remote,
            runner_id=runner_id if remote is not RemoteRemoval.UNCONFIGURED else None,
            dir_removed=dir_removed,
            warnings=tuple(redact(w) for w in warnings),
        )

    @staticmethod
    def _delete_remote(
        client: GitHubClient, record: RunnerRecord, runner_id: int | None
    ) -> tuple[RemoteRemoval, int | None]:
        if runner_id is None:
            match = match_remote(record, client.list_runners(record.owner, record.repo))
            if match is None:
                return RemoteRemoval.ALREADY_GONE, None
            runner_id = match.id
        if client.delete_runner(record.owner, record.repo, runner_id):
            return RemoteRemoval.API_DELETED, runner_id
        return RemoteRemoval.ALREADY_GONE, runner_id

    def _runner_pids(self, runner_dir: Path) -> list[int]:
        try:
            return [p.pid for p in self._find_processes(runner_dir)]
        except (psutil.Error, OSError) as e:
            raise ProcessError(f"无法检查 {runner_dir} 下的 runner 进程：{e}") from e

    # --- import / scan --------------------------------------------------------------------------

    def import_runner(self, path: str | os.PathLike[str]) -> RunnerRecord:
        """Record an already configured runner directory (e.g. one set up by hand).

        Same path already recorded -> that record (no network). Otherwise reads `.runner`, maps
        `gitHubUrl` to owner/repo and asks GitHub whether the runner still exists (missing ->
        RunnerDeckError). A recorded runner with the same GitHub id whose directory no longer
        exists is treated as moved: its path is updated. If its directory still exists, two
        directories claim one registration -> InvalidInputError.
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
            remote = client.get_runner(owner, repo, info.agent_id)
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
            )
            config.upsert(record)
            self._store.save(config)
        log.info("imported runner %s (%s) from %s", record.name, record.full_name, runner_dir)
        return record

    def scan_unregistered(self) -> list[Path]:
        """Configured runner directories directly under runners_root that have no record."""
        config = self._store.load()
        known = {_path_key(r.path) for r in config.runners}
        root = Path(config.settings.runners_root).expanduser()
        return [p for p in runner_file.scan_runner_dirs(root) if _path_key(p) not in known]

    @staticmethod
    def _record_for_path(config: AppConfig, runner_dir: Path) -> RunnerRecord | None:
        key = _path_key(runner_dir)
        return next((r for r in config.runners if _path_key(r.path) == key), None)

    # --- status / processes ----------------------------------------------------------------------

    def statuses(
        self, processes: Mapping[str, RunnerProcess] | None = None
    ) -> list[RunnerStatusRow]:
        """Merged local + GitHub status for every record, in config order.

        processes: RunnerProcess objects owned by the caller, keyed by record id. Records
        without one count as not running (external if runner processes live in the dir).
        GitHub is asked once per repo; a repo whose list fails (or no client at all, e.g. not
        logged in) shows remote UNKNOWN with `remote_error`, without affecting other repos.
        """
        config = self._store.load()
        if not config.runners:
            return []
        now = self._clock()
        lists, errors = self._fetch_remote_lists(config.runners)
        rows: list[RunnerStatusRow] = []
        for record in config.runners:
            proc = processes.get(record.id) if processes else None
            local = self._local_snapshot(record, proc, now)
            key = _repo_key(record.owner, record.repo)
            remote_runner: RemoteRunner | None = None
            if key in lists:
                remote_runner = match_remote(record, lists[key])
                remote = RemoteSnapshot.from_remote(remote_runner, fetched_at=now)
            else:
                remote = RemoteSnapshot(RemoteStatus.UNKNOWN)
            rows.append(
                RunnerStatusRow(
                    record=record,
                    display=merge_status(local, remote, now=now),
                    local=local,
                    remote=remote,
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

    def _local_snapshot(
        self, record: RunnerRecord, proc: RunnerProcess | None, now: datetime
    ) -> LocalSnapshot:
        if proc is None:
            return LocalSnapshot(
                state=LocalState.STOPPED,
                running=False,
                external=self._has_external(record),
                state_since=now,
            )
        running = proc.is_running()
        return LocalSnapshot(
            state=proc.state,
            running=running,
            external=False if running else self._has_external(record),
            state_since=proc.state_since,
            last_stop_forced=proc.last_stop_forced,
            stopped_at=proc.stopped_at,
            current_job=proc.current_job,
            last_error=proc.last_error,
        )

    def _has_external(self, record: RunnerRecord) -> bool:
        try:
            return bool(self._find_processes(Path(record.path)))
        except (psutil.Error, OSError) as e:
            log.warning("cannot scan processes of %s: %s", record.path, e)
            return False

    def process_for(
        self,
        record: RunnerRecord,
        *,
        on_line: Callable[[str], None] | None = None,
        on_state: Callable[[LocalState, str | None], None] | None = None,
        on_job_finished: Callable[[str, str], None] | None = None,
        max_lines: int = 2000,
    ) -> RunnerProcess:
        """A (not yet started) RunnerProcess for `record`, using the injected popen.

        With `settings.proxy` set, run.cmd gets https_proxy / http_proxy so the runner reaches
        GitHub through it. Callbacks run on background threads (see RunnerProcess).
        """
        proxy = self._store.load().settings.proxy
        return RunnerProcess(
            Path(record.path),
            env=_proxy_env(proxy),
            on_line=on_line,
            on_state=on_state,
            on_job_finished=on_job_finished,
            popen=self._popen,
            max_lines=max_lines,
        )
