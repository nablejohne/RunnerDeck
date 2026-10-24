"""Command-line front end for debugging and M1 acceptance (docs/m1-design.md §11).

    python -m runnerdeck.core.cli <command> ...

Every business flow lives in manager.py; this module only parses arguments, asks for
confirmation and prints. All output goes through redact(). Exit codes: 0 success, 1 a
RunnerDeckError (printed as "错误：<message>") or a cancelled confirmation, 2 bad arguments,
130 Ctrl+C. In `start`, Ctrl+C is the normal way to stop supervising and exits 0 once every
runner has stopped; `start` never exits 0 while a runner it started may still be alive (it
lists those with their PID and exits 1, or 130 when the user gave up waiting).

Grounded in docs/spike-notes.md:
- §2: users paste the token's name instead of its value, so `login` checks the format first.
- §5: only Ctrl+Break through the shared console stops a runner cleanly. The CLI runs in a
  console, so `start` can stop its runners gracefully; `stop` (another terminal) cannot and
  always hard-kills, after which the GitHub session lingers about 3 minutes.
Both record how each runner stopped (RunnerManager.record_stop, docs/m3-design.md §3.6), so a
later `status` here or in the GUI shows "GitHub 会话未过期" instead of "被其他进程运行".
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
import traceback
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import FrameType
from typing import Any, NoReturn, TextIO
from urllib.parse import urlsplit

import psutil
from pydantic import ValidationError

from runnerdeck import __version__
from runnerdeck.core import (
    account,
    logs,
    paths,
    process,
    runner_file,
    secret_input,
    secrets,
    winproc,
)
from runnerdeck.core.account import format_expiry, permission_hints
from runnerdeck.core.errors import (
    InvalidInputError,
    NotFoundError,
    ProcessError,
    RunnerDeckError,
)
from runnerdeck.core.github_client import GitHubClient
from runnerdeck.core.manager import (
    AddPlan,
    ClientFactory,
    RemoteRemoval,
    RunnerManager,
    RunnerStatusRow,
)
from runnerdeck.core.models import (
    AppConfig,
    AppSettings,
    DisplayState,
    GitHubUser,
    LocalState,
    RemoteStatus,
    RunnerRecord,
    normalize_labels,
    parse_full_name,
)
from runnerdeck.core.process import RunnerProcess, StopResult
from runnerdeck.core.redact import redact, register_secret
from runnerdeck.core.status import display_label
from runnerdeck.core.store import ConfigStore, resolve_runner

log = logging.getLogger(__name__)

PROG = "runnerdeck-cli"
ENV_PAT = account.ENV_PAT

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

# Own log file: the tray app (and later the CLI next to it) must not rotate a file another
# process holds open, which fails on Windows (WinError 32).
LOG_FILE_NAME = "runnerdeck-cli.log"
STOP_POLL_S = 0.1  # `start` stop phase: how often to look for finished stops / another Ctrl+C
# `start` stop phase: how long to wait beyond stop_timeout_s (kill wait, pipe drain, psutil
# scans in RunnerProcess.stop) before listing the runners that did not stop.
STOP_GRACE_S = 30.0
SETTINGS_KEYS = ("runners_root", "default_labels", "poll_interval_s", "proxy")
_PROXY_OFF = frozenset({"", "none", "off", "-"})
_SHOWN_AUTO_LABELS = ("self-hosted", "Windows", "X64")  # added by the runner (AUTO_LABELS)
_BAD_PROXY = "代理地址不合法：只支持 http:// 或 https:// 代理，例如 http://127.0.0.1:7890"

LINGER_HINT = (
    "提示：强制结束后 GitHub 端的旧会话约 3 分钟后才过期；这期间重新启动会先显示"
    "“等待旧会话过期”，runner 会自动重试，不是故障"
)
RECONNECTING_HINT = (
    "GitHub 端的旧会话还没过期（上次是强制结束的），runner 大约每 30 秒重试一次，"
    "通常 3 分钟内会自动恢复"
)
EXTERNAL_EXIT_HINT = (
    "这次退出不是在本终端按 Ctrl+C 停止的：可能是 runner 自己出错退出，"
    "也可能是被外部结束（例如在另一个终端执行了 {prog} stop {key}）"
)

ReadFn = Callable[[str], str]
CliClientFactory = Callable[[str, str | None], GitHubClient]  # (pat, proxy) -> client
ManagerFactory = Callable[[ConfigStore, ClientFactory], RunnerManager]


class UsageError(Exception):
    """Bad combination of arguments that argparse cannot express; exit code 2."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _default_client(pat: str, proxy: str | None) -> GitHubClient:
    return GitHubClient(pat, proxy=proxy)


def _default_manager(store: ConfigStore, client_factory: ClientFactory) -> RunnerManager:
    return RunnerManager(store, client_factory)


@dataclass
class CliDeps:
    """Everything main() touches outside this module; tests replace parts of it.

    stdout / stderr None mean sys.stdout / sys.stderr at call time. read_secret / read_line
    are secret_input.read_secret / input (prompt text is passed already redacted).
    client_factory builds a GitHubClient from (pat, settings.proxy); manager_factory gets the
    store and a zero-argument client factory that resolves the PAT lazily
    (RUNNERDECK_PAT > keyring by config.account).
    kill_tree is what `start` uses on a second Ctrl+C: kill a run.cmd process tree by PID and
    return (pids gone, pids still alive).
    """

    stdout: TextIO | None = None
    stderr: TextIO | None = None
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    read_secret: ReadFn = secret_input.read_secret
    read_line: ReadFn = input
    store_factory: Callable[[], ConfigStore] = ConfigStore
    client_factory: CliClientFactory = _default_client
    manager_factory: ManagerFactory = _default_manager
    stop_external: Callable[[Path], process.ExternalStop] = process.stop_external
    kill_tree: Callable[[int], tuple[list[int], list[int]]] = winproc.kill_tree_report
    setup_logging: Callable[..., Path] = logs.setup_logging
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], datetime] = _utcnow
    poll_interval_s: float = 0.5  # `start`: how often to check whether runners are still alive
    stop_timeout_s: float | None = None  # None: RunnerProcess picks (idle vs. running a job)
    stop_grace_s: float = STOP_GRACE_S


# --- output -------------------------------------------------------------------------------


class Console:
    """Redacting, thread-safe writer. `start` prints from runner reader threads too."""

    def __init__(self, out: TextIO, err: TextIO) -> None:
        self._out = out
        self._err = err
        self._lock = threading.Lock()

    def print(self, text: str = "") -> None:
        self.write(text + "\n")

    def error(self, text: str) -> None:
        self.write(text + "\n", err=True)

    def write(self, text: str, *, err: bool = False) -> None:
        stream = self._err if err else self._out
        text = redact(text)
        with self._lock:
            try:
                stream.write(text)
            except UnicodeEncodeError:
                # A pipe on an English Windows is cp1252: keep going with "?" instead of crashing.
                encoding = getattr(stream, "encoding", None) or "ascii"
                stream.write(text.encode(encoding, "replace").decode(encoding, "replace"))
            stream.flush()


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return " ".join(text.split()) or "-"


def render_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    """Plain-text table; CJK characters count as two columns."""
    cells = [[_cell(c) for c in row] for row in rows]
    widths = [
        max([_width(header), *(_width(row[i]) for row in cells)])
        for i, header in enumerate(headers)
    ]

    def line(values: Sequence[str]) -> str:
        padded = (v + " " * (w - _width(v)) for v, w in zip(values, widths, strict=True))
        return "  ".join(padded).rstrip()

    return [line(headers), line(["-" * w for w in widths]), *(line(row) for row in cells)]


# --- argument parsing ---------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """argparse that prints through Console (redacted, injectable) and in Chinese."""

    def __init__(self, *args: Any, console: Console, **kwargs: Any) -> None:
        kwargs.setdefault("add_help", False)
        super().__init__(*args, **kwargs)
        self._console = console
        self.add_argument("-h", "--help", action="help", help="显示帮助并退出")

    def _print_message(self, message: str, file: Any = None) -> None:
        if message:
            # argparse's own default for file=None is stderr.
            self._console.write(message, err=file is None or file is sys.stderr)

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}：错误：{message}\n")


def build_parser(console: Console) -> argparse.ArgumentParser:
    make = partial(_Parser, console=console)
    parser = make(
        prog=PROG,
        description="RunnerDeck 命令行：管理本机的仓库级 GitHub Actions self-hosted runner",
    )
    parser.add_argument(
        "--debug", action="store_true", help="调试日志同时输出到终端；内部错误时打印完整堆栈"
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}", help="显示版本"
    )
    sub = parser.add_subparsers(dest="command", metavar="<命令>", required=True, parser_class=make)

    def command(name: str, summary: str, handler: Callable[..., int]) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=summary, description=summary)
        p.set_defaults(handler=handler)
        return p

    command("login", f"登录：读取 PAT（或环境变量 {ENV_PAT}），保存到系统凭据管理器", _cmd_login)
    command("logout", "退出登录：删除保存的 PAT", _cmd_logout)
    command("whoami", "显示当前登录的账号和 PAT 信息", _cmd_whoami)
    command("repos", "列出自己的仓库及本机已有的 runner", _cmd_repos)

    p = command("add", "为仓库下载、注册一个新的 runner", _cmd_add)
    p.add_argument("repo", help="owner/repo、仓库 URL，或只写 repo（owner 取当前登录账号）")
    p.add_argument("--name", help="runner 名称（默认 <主机名>-<repo>）")
    p.add_argument("--labels", help="自定义标签，逗号分隔（默认取设置里的 default_labels）")
    p.add_argument("--root", help="runner 目录的上级目录（默认取设置里的 runners_root）")
    p.add_argument("--version", dest="runner_version", help="runner 版本，例如 2.338.0（默认最新）")
    p.add_argument("-y", "--yes", action="store_true", help="不询问，直接确认")

    command("list", "列出本机登记的 runner", _cmd_list)
    command("status", "显示每个 runner 的本地 + GitHub 合并状态", _cmd_status)

    p = command("start", "前台运行 runner，按 Ctrl+C 停止全部", _cmd_start)
    p.add_argument(
        "keys", nargs="*", metavar="key", help="runner 的 id、id 前缀、名称或 owner/repo"
    )
    p.add_argument("--all", action="store_true", help="启动所有登记的 runner")

    p = command("stop", "从另一个终端结束 runner（强制结束）", _cmd_stop)
    p.add_argument(
        "keys", nargs="+", metavar="key", help="runner 的 id、id 前缀、名称或 owner/repo"
    )

    p = command("remove", "在 GitHub 上注销 runner 并删除目录和记录", _cmd_remove)
    p.add_argument("key", help="runner 的 id、id 前缀、名称或 owner/repo")
    p.add_argument("--keep-work", action="store_true", help="保留 _work 目录")
    p.add_argument(
        "--local-only",
        action="store_true",
        help="不访问 GitHub，只删除本地目录和记录（仓库已删除或 PAT 已无权限时使用）",
    )
    p.add_argument("-y", "--yes", action="store_true", help="不询问，直接确认")

    p = command("import", "登记一个已经配置好的 runner 目录", _cmd_import)
    p.add_argument("path", help="runner 目录")

    p = command(
        "forget", "取消登记：只删除 RunnerDeck 的记录，目录和 GitHub 上的注册都保留", _cmd_forget
    )
    p.add_argument("key", help="runner 的 id、id 前缀、名称或 owner/repo")
    p.add_argument("-y", "--yes", action="store_true", help="不询问，直接确认")

    command("scan", "列出 runners_root 下还没登记的 runner 目录", _cmd_scan)

    p = command("settings", "查看或修改设置", _cmd_settings_show)
    settings_sub = p.add_subparsers(dest="settings_command", metavar="<操作>", parser_class=make)
    s = settings_sub.add_parser("show", help="显示设置", description="显示设置")
    s.set_defaults(handler=_cmd_settings_show)
    s = settings_sub.add_parser("set", help="修改一项设置", description="修改一项设置")
    s.add_argument("key", choices=SETTINGS_KEYS, help="、".join(SETTINGS_KEYS))
    s.add_argument("value", help="新值；proxy 写 none 表示清除，default_labels 写空串表示清空")
    s.set_defaults(handler=_cmd_settings_set)
    return parser


# --- context ------------------------------------------------------------------------------


class _Context:
    """Per-invocation state: store, lazily resolved PAT, lazily built manager."""

    def __init__(self, deps: CliDeps, console: Console) -> None:
        self.deps = deps
        self.console = console
        self.store = deps.store_factory()
        self._pat: tuple[str, str] | None = None
        self._manager: RunnerManager | None = None

    def config(self) -> AppConfig:
        return self.store.load()

    def pat(self) -> tuple[str, str]:
        """(PAT, where it came from). RUNNERDECK_PAT wins over the stored one."""
        if self._pat is None:
            self._pat = account.resolve_pat(
                self.config().account,
                self.deps.environ,
                login_hint=f"请先运行 {PROG} login（或设置环境变量 {ENV_PAT}）",
            )
        return self._pat

    def client(self) -> GitHubClient:
        pat, _ = self.pat()
        return self.deps.client_factory(pat, self.config().settings.proxy)

    @property
    def manager(self) -> RunnerManager:
        if self._manager is None:
            self._manager = self.deps.manager_factory(self.store, self.client)
        return self._manager

    def confirm(self, question: str) -> bool:
        try:
            answer = self.deps.read_line(redact(question))
        except EOFError:
            self.console.print()
            self.console.print("没有读到确认（非交互运行时请加 --yes）")
            return False
        return answer.strip().casefold() in ("y", "yes")


# --- helpers ------------------------------------------------------------------------------


def _short_key(config: AppConfig, record: RunnerRecord) -> str:
    """The name if it identifies the runner unambiguously, else the 8-char id prefix."""
    folded = record.name.casefold()
    if sum(r.name.casefold() == folded for r in config.runners) == 1:
        return record.name
    return record.id[:8]


def _progress(console: Console) -> Callable[[str, str], None]:
    def report(step: str, text: str) -> None:
        console.print(f"  > {text}")

    return report


def _unique(records: Sequence[RunnerRecord]) -> list[RunnerRecord]:
    seen: set[str] = set()
    out: list[RunnerRecord] = []
    for record in records:
        if record.id not in seen:
            seen.add(record.id)
            out.append(record)
    return out


_format_expiry = format_expiry  # kept under the old names for tests / callers


def _print_token_info(ctx: _Context, user: GitHubUser) -> None:
    console = ctx.console
    kinds = account.TOKEN_KIND_LABELS
    console.print(f"token 类型：{kinds.get(user.token_kind, user.token_kind)}")
    if user.token_kind == "classic":
        console.print(f"权限范围：{', '.join(user.scopes) if user.scopes else '无'}")
    text, attention = _format_expiry(user.token_expires_at, ctx.deps.clock())
    console.print(f"{'注意：' if attention else ''}过期时间：{text}")
    for hint in permission_hints(user):
        console.print(f"权限要求：{hint}")


def _parse_repo_target(ctx: _Context, target: str) -> tuple[str, str]:
    target = target.strip()
    if target.casefold().startswith(("https://", "http://")):
        return runner_file.parse_github_url(target)
    if "/" in target:
        return parse_full_name(target)
    account = ctx.config().account
    if account:
        owner = account
    else:  # RUNNERDECK_PAT without a login: ask GitHub who we are
        with closing(ctx.client()) as client:
            owner = client.get_user().login
    return parse_full_name(f"{owner}/{target}")


def _labels_text(labels: Sequence[str]) -> str:
    return ", ".join([*_SHOWN_AUTO_LABELS, *labels])


def _mask_proxy(proxy: str) -> str:
    try:
        parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    except ValueError:
        return proxy
    if parts.password:
        return proxy.replace(f":{parts.password}@", ":***@")
    return proxy


def _remote_text(row: RunnerStatusRow) -> str:
    status = row.remote.status
    if status is RemoteStatus.ONLINE:
        return "在线（忙）" if row.remote.busy else "在线"
    return {
        RemoteStatus.OFFLINE: "离线",
        RemoteStatus.MISSING: "不存在",
        RemoteStatus.UNKNOWN: "未知",
    }[status]


# --- commands: account --------------------------------------------------------------------


def _cmd_login(ctx: _Context, args: argparse.Namespace) -> int:
    console = ctx.console
    raw = ctx.deps.environ.get(ENV_PAT, "")
    from_env = bool(raw.strip())
    if from_env:
        console.print(f"使用环境变量 {ENV_PAT} 中的 PAT")
    else:
        try:
            raw = ctx.deps.read_secret(
                redact("请粘贴 GitHub PAT 的值（输入不会显示；Ctrl+V 或鼠标右键粘贴）：")
            )
        except EOFError:
            raise InvalidInputError("没有读到 PAT（输入已结束）") from None
    register_secret(raw.strip())
    try:
        pat = secrets.validate_pat_format(raw)
    except InvalidInputError as e:
        if from_env:
            raise InvalidInputError(f"环境变量 {ENV_PAT}：{e}") from None
        raise

    config = ctx.config()
    console.print("正在向 GitHub 验证 PAT …")
    with closing(ctx.deps.client_factory(pat, config.settings.proxy)) as client:
        user = client.get_user()
    secrets.save_pat(user.login, pat)
    previous = config.account
    config.account = user.login
    ctx.store.save(config)
    log.info("logged in as %s (%s token)", user.login, user.token_kind)
    console.print(f"已登录：{user.login}（PAT 已保存到系统凭据管理器）")
    if previous and previous.casefold() != user.login.casefold():
        try:
            secrets.delete_pat(previous)
        except RunnerDeckError as e:
            console.error(f"警告：无法删除之前账号 {previous} 保存的 PAT：{e}")
        else:
            console.print(f"已删除之前账号 {previous} 保存的 PAT")
    _print_token_info(ctx, user)
    return EXIT_OK


def _cmd_logout(ctx: _Context, args: argparse.Namespace) -> int:
    console = ctx.console
    config = ctx.config()
    if not config.account:
        console.print("当前没有登录")
    else:
        account = config.account
        secrets.delete_pat(account)
        config.account = None
        ctx.store.save(config)
        log.info("logged out %s", account)
        console.print(f"已退出登录 {account}，系统凭据管理器中的 PAT 已删除")
    if ctx.deps.environ.get(ENV_PAT, "").strip():
        console.print(f"注意：环境变量 {ENV_PAT} 仍然设置着，之后的命令会继续使用它")
    return EXIT_OK


def _cmd_whoami(ctx: _Context, args: argparse.Namespace) -> int:
    console = ctx.console
    _, source = ctx.pat()
    with closing(ctx.client()) as client:
        user = client.get_user()
    console.print(f"登录名：{user.login}")
    console.print(f"PAT 来源：{source}")
    account = ctx.config().account
    if account and account.casefold() != user.login.casefold():
        console.print(f"注意：配置里登录的账号是 {account}，但当前 PAT 属于 {user.login}")
    _print_token_info(ctx, user)
    return EXIT_OK


def _cmd_repos(ctx: _Context, args: argparse.Namespace) -> int:
    console = ctx.console
    with closing(ctx.client()) as client:
        repos = client.list_repos()
    if not repos:
        console.print("这个账号下没有自己的仓库")
        return EXIT_OK
    config = ctx.config()
    rows = []
    for repo in repos:
        tags = ["私有" if repo.private else "公开"]
        if repo.archived:
            tags.append("已归档")
        if repo.fork:
            tags.append("fork")
        folded = repo.full_name.casefold()
        local = [r.name for r in config.runners if r.full_name.casefold() == folded]
        rows.append((repo.full_name, "，".join(tags), ", ".join(local) or "-"))
    for line in render_table(("仓库", "可见性", "本机 runner"), rows):
        console.print(line)
    console.print(f"共 {len(repos)} 个仓库")
    return EXIT_OK


# --- commands: runners --------------------------------------------------------------------


def _print_plan(console: Console, plan: AddPlan) -> None:
    visibility = "私有" if plan.private else "公开"
    console.print(f"仓库：{plan.full_name}（{visibility}）")
    console.print(f"runner 名称：{plan.name}")
    console.print(f"标签：{_labels_text(plan.labels)}")
    console.print(f"目录：{plan.target_dir}")
    console.print(f"runner 版本：{plan.version or '最新版'}")


def _cmd_add(ctx: _Context, args: argparse.Namespace) -> int:
    console = ctx.console
    owner, repo = _parse_repo_target(ctx, args.repo)
    labels = None if args.labels is None else args.labels.split(",")
    console.print(f"查询仓库 {owner}/{repo} …")
    plan = ctx.manager.plan_add(
        owner, repo, name=args.name, labels=labels, root=args.root, version=args.runner_version
    )
    _print_plan(console, plan)
    for warning in plan.warnings:
        console.print(f"注意：{warning}")
    confirmed = args.yes or not plan.needs_confirmation
    if not confirmed and not ctx.confirm("确认添加？输入 y 继续，其他任意输入取消："):
        console.print("已取消，没有做任何改动")
        return EXIT_ERROR
    record = ctx.manager.add_runner(plan, progress=_progress(console))
    key = _short_key(ctx.config(), record)
    console.print(
        f"完成：runner {record.name} 已注册到 {record.full_name}"
        f"（GitHub id {record.github_runner_id}，本地 id {record.id[:8]}）"
    )
    console.print(f"前台运行它：{PROG} start {key}")
    return EXIT_OK


def _cmd_list(ctx: _Context, args: argparse.Namespace) -> int:
    console = ctx.console
    config = ctx.config()
    if not config.runners:
        console.print(f"还没有登记任何 runner（用 {PROG} add 添加，或 {PROG} import 导入已有目录）")
        return EXIT_OK
    rows = [
        (r.id[:8], r.name, r.full_name, ", ".join(r.labels) or "-", r.path) for r in config.runners
    ]
    for line in render_table(("ID", "名称", "仓库", "自定义标签", "目录"), rows):
        console.print(line)
    return EXIT_OK


def _cmd_status(ctx: _Context, args: argparse.Namespace) -> int:
    console = ctx.console
    rows = ctx.manager.statuses()
    if not rows:
        console.print("还没有登记任何 runner")
        return EXIT_OK
    table = [
        (
            row.record.id[:8],
            row.record.name,
            row.record.full_name,
            row.display.label,
            _remote_text(row),
            row.display.detail,
        )
        for row in rows
    ]
    for line in render_table(("ID", "名称", "仓库", "状态", "GitHub", "说明"), table):
        console.print(line)
    # One line per distinct error; "not logged in" is the same for every repo.
    errors: dict[str, list[str]] = {}
    for row in rows:
        if row.remote_error is not None:
            repos = errors.setdefault(row.remote_error, [])
            if row.record.full_name not in repos:
                repos.append(row.record.full_name)
    for message, repos in errors.items():
        console.print(f"注意：无法获取 GitHub 状态（{'、'.join(repos)}）：{message}")
    return EXIT_OK


def _cmd_remove(ctx: _Context, args: argparse.Namespace) -> int:
    console = ctx.console
    config = ctx.config()
    record = resolve_runner(config, args.key)
    key = _short_key(config, record)
    console.print(f"将移除 runner {record.name}（{record.full_name}，目录 {record.path}）")
    console.print(
        (
            "不会访问 GitHub（--local-only），只删除本地目录"
            if args.local_only
            else "会在 GitHub 上注销它，删除本地目录"
        )
        + ("（保留 _work）" if args.keep_work else "")
        + "，并删除记录"
    )
    if not args.yes and not ctx.confirm("确认移除？输入 y 继续，其他任意输入取消："):
        console.print("已取消，没有做任何改动")
        return EXIT_ERROR
    try:
        result = ctx.manager.remove_runner(
            record.id,
            keep_work=args.keep_work,
            local_only=args.local_only,
            progress=_progress(console),
        )
    except ProcessError as e:
        console.error(f"错误：{e}")
        console.error(f"提示：runner 在另一个终端运行时，可以先执行 {PROG} stop {key}")
        return EXIT_ERROR
    except NotFoundError as e:
        console.error(f"错误：{e}")
        console.error(
            f"提示：如果仓库已被删除或不再需要这个 runner，可以用 {PROG} remove {key} --local-only"
            " 只删除本地目录和记录"
        )
        return EXIT_ERROR
    for warning in result.warnings:
        console.print(f"注意：{warning}")
    if result.remote is RemoteRemoval.UNCONFIGURED:
        console.print("GitHub：已通过 config.cmd remove 注销")
    elif result.remote is RemoteRemoval.SKIPPED:
        console.print("GitHub：未处理（--local-only）")
    elif result.remote is RemoteRemoval.API_DELETED:
        console.print(
            f"GitHub：已通过 API 删除（runner id {result.runner_id}；"
            "GitHub 对已不存在的 id 也返回成功）"
        )
    else:
        console.print("GitHub：上面已经没有这个 runner")
    if result.dir_removed:
        console.print("目录：已删除" + ("（保留了 _work）" if args.keep_work else ""))
    console.print(f"已移除 runner {record.name}")
    return EXIT_OK


def _cmd_import(ctx: _Context, args: argparse.Namespace) -> int:
    console = ctx.console
    before = {r.id: r.path for r in ctx.config().runners}
    record = ctx.manager.import_runner(args.path)
    old_path = before.get(record.id)
    if old_path is None:
        console.print(
            f"已导入 runner {record.name}（{record.full_name}，GitHub id "
            f"{record.github_runner_id}，本地 id {record.id[:8]}），目录 {record.path}"
        )
    elif os.path.normcase(old_path) == os.path.normcase(record.path):
        console.print(f"这个目录已经登记为 runner {record.name}（本地 id {record.id[:8]}）")
    else:
        console.print(f"runner {record.name} 的目录已更新：{old_path} -> {record.path}")
    return EXIT_OK


def _cmd_forget(ctx: _Context, args: argparse.Namespace) -> int:
    console = ctx.console
    config = ctx.config()
    record = resolve_runner(config, args.key)
    console.print(f"将取消登记 runner {record.name}（{record.full_name}，目录 {record.path}）")
    console.print(
        "只删除 RunnerDeck 的记录：不访问 GitHub，目录和其中的 .runner 原样保留，"
        "GitHub 上的注册也保留"
    )
    if not args.yes and not ctx.confirm("确认取消登记？输入 y 继续，其他任意输入取消："):
        console.print("已取消，没有做任何改动")
        return EXIT_ERROR
    ctx.manager.forget_runner(record.id)
    console.print(
        f"已取消登记 runner {record.name}；之后可以用 {PROG} import {record.path} 重新登记"
    )
    return EXIT_OK


def _cmd_scan(ctx: _Context, args: argparse.Namespace) -> int:
    console = ctx.console
    found = ctx.manager.scan_unregistered()
    root = ctx.config().settings.runners_root
    if not found:
        console.print(f"{root} 下没有未登记的 runner 目录")
        return EXIT_OK
    console.print(f"{root} 下有 {len(found)} 个未登记的 runner 目录：")
    for path in found:
        console.print(f"  {path}")
    console.print(f"用 {PROG} import <目录> 登记")
    return EXIT_OK


def _cmd_stop(ctx: _Context, args: argparse.Namespace) -> int:
    console = ctx.console
    config = ctx.config()
    records = _unique([resolve_runner(config, key) for key in args.keys])
    code = EXIT_OK
    forced = False
    for record in records:
        name = record.name
        try:
            outcome = ctx.deps.stop_external(Path(record.path))
        except (psutil.Error, OSError, RunnerDeckError) as e:
            console.error(f"错误：无法结束 runner {name}：{e}")
            code = EXIT_ERROR
            continue
        killed = ", ".join(map(str, outcome.killed))
        if outcome.killed:
            # Always a hard kill: the next `status` (here or in the GUI) must know the GitHub
            # session lingers, rather than calling an online runner "被其他进程运行".
            ctx.manager.record_stop(Path(record.path), forced=True)
        if outcome.ok and not outcome.killed:
            console.print(f"runner {name} 没有在运行")
        elif outcome.ok:
            forced = True
            console.print(f"已强制结束 runner {name}（PID {killed}）")
        else:
            if outcome.killed:
                console.print(f"已结束 runner {name} 的部分进程（PID {killed}）")
            survivors = ", ".join(map(str, outcome.survivors))
            console.error(f"错误：无法结束 runner {name} 的进程（PID {survivors}），它仍在运行")
            console.error(
                "提示：它可能是用管理员权限或其他账号启动的，请在同样权限的终端里重新执行 "
                f"{PROG} stop {_short_key(config, record)}"
            )
            code = EXIT_ERROR
    if forced:
        console.print(LINGER_HINT)
    return code


# --- start: foreground supervision ---------------------------------------------------------


@contextmanager
def _sigint_counted(handler: Callable[[int, FrameType | None], None]) -> Iterator[None]:
    """While inside, Ctrl+C (SIGINT) calls `handler` instead of raising KeyboardInterrupt.

    Only the main thread can install a handler; Ctrl+C never reaches other threads anyway.
    CPython runs the handler on the main thread the next time that thread executes bytecode:
    at once in time.sleep(), but on Windows only after a WaitForSingleObject (Popen.wait) or a
    lock wait has returned.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    try:
        previous = signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):  # e.g. an embedding interpreter without signal support
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, signal.default_int_handler if previous is None else previous)


@dataclass
class _Run:
    """One runner under `start` supervision. The stop phase's threads fill in the results."""

    record: RunnerRecord
    key: str  # what to type after `runnerdeck-cli stop`
    proc: RunnerProcess
    pid: int | None = None  # run.cmd's cmd.exe, remembered for the final report
    stopper: threading.Thread | None = None  # runs proc.stop()
    result: StopResult | None = None
    stop_error: str | None = None
    forcer: threading.Thread | None = None  # runs deps.kill_tree() after a 2nd Ctrl+C
    force_killed: list[int] = field(default_factory=list)
    force_survivors: list[int] = field(default_factory=list)
    force_error: str | None = None

    def busy(self) -> bool:
        return any(t is not None and t.is_alive() for t in (self.stopper, self.forcer))

    def wait(self, timeout: float) -> None:
        for thread in (self.stopper, self.forcer):
            if thread is not None and thread.is_alive():
                thread.join(timeout)
                return


class _Supervisor:
    """Runs several RunnerProcess objects in the foreground until they exit or Ctrl+C.

    run.cmd runs in its own process group, which ignores the console's Ctrl+C, so only this
    object can stop the runners, and it must not lose one (docs/m1-design.md §11):
    - While supervising, SIGINT only counts presses (_sigint_counted) instead of raising
      KeyboardInterrupt at an arbitrary point, such as after Popen spawned run.cmd but before
      the runner was registered here, which would leave it running unsupervised.
    - 1st Ctrl+C: start no more runners; stop all running ones at the same time, each on its
      own thread (Ctrl+Break, then up to stop_timeout_s each, in parallel).
    - 2nd Ctrl+C: kill every runner still stopping (deps.kill_tree on run.cmd's PID). The
      graceful wait itself cannot be cut short: on Windows RunnerProcess.stop() waits in
      WaitForSingleObject, which Ctrl+C does not interrupt. So the main thread never blocks in
      it, and killing the process is what ends that wait.
    - 3rd Ctrl+C: stop waiting.
    Without further presses the stop phase still ends after stop_timeout_s + stop_grace_s.
    Every runner that may still be alive at the end is listed with its PID and a
    `runnerdeck-cli stop <key>` hint; the exit code is then 1 (130 after a 3rd Ctrl+C), never 0.
    """

    def __init__(
        self, ctx: _Context, records: Sequence[RunnerRecord], config: AppConfig | None = None
    ) -> None:
        self.ctx = ctx
        self.console = ctx.console
        self.records = records
        self.config = config
        self.runs: list[_Run] = []
        self.skipped: list[RunnerRecord] = []
        self.failed = False
        self._hinted: set[str] = set()
        self._presses = 0

    def _on_sigint(self, signum: int, frame: FrameType | None) -> None:
        # Runs on the main thread between two bytecodes, possibly while that thread holds a
        # lock (Console, RunnerProcess, threading.Event): only count, never print or wait here.
        self._presses += 1

    def _key(self, record: RunnerRecord) -> str:
        return _short_key(self.config, record) if self.config is not None else record.name

    def _on_line(self, name: str) -> Callable[[str], None]:
        def on_line(line: str) -> None:
            self.console.print(f"[{name}] {line}")

        return on_line

    def _on_state(self, name: str) -> Callable[[LocalState, str | None], None]:
        def on_state(state: LocalState, job: str | None) -> None:
            label = display_label(DisplayState(state.value), job)
            self.console.print(f"[{name}] 状态：{label}")
            if state is LocalState.RECONNECTING and name not in self._hinted:
                self._hinted.add(name)
                self.console.print(f"[{name}] {RECONNECTING_HINT}")

        return on_state

    def run(self) -> int:
        gave_up = False
        with _sigint_counted(self._on_sigint):
            try:
                if self._supervise():
                    self._announce_stop()
                    ended = [run for run in self.runs if not run.proc.is_running()]
                    self._report_exits(ended)
                    targets = [run for run in self.runs if run not in ended]
                    gave_up = self._stop_all(targets, base=1)
                else:
                    self._report_exits(self.runs)
            except Exception:
                # Never leave runners behind on an unexpected failure.
                self._stop_all([run for run in self.runs if run.proc.is_running()], base=0)
                self._report_alive()
                raise
            alive = self._report_alive()
        if alive:
            return EXIT_INTERRUPTED if gave_up else EXIT_ERROR
        return EXIT_ERROR if self.failed else EXIT_OK

    def _supervise(self) -> bool:
        """Start the runners, then wait while any is running. True if Ctrl+C ended that."""
        deps = self.ctx.deps
        try:
            self._start_all()
            if self.runs and not self._presses:
                self.console.print("按 Ctrl+C 停止所有 runner")
            while not self._presses and any(run.proc.is_running() for run in self.runs):
                deps.sleep(deps.poll_interval_s)
        except KeyboardInterrupt:  # only without our SIGINT handler (off the main thread)
            self._presses += 1
        return self._presses > 0

    def _start_all(self) -> None:
        manager = self.ctx.manager
        for index, record in enumerate(self.records):
            if self._presses:
                self.skipped = list(self.records[index:])
                return
            name = record.name
            try:
                proc = manager.process_for(
                    record, on_line=self._on_line(name), on_state=self._on_state(name)
                )
            except RunnerDeckError as e:
                self.console.error(f"错误：[{name}] 无法启动：{e}")
                self.failed = True
                continue
            run = _Run(record, self._key(record), proc)
            # Registered before start(): if start() is cut short after run.cmd was spawned,
            # the stop phase still finds the runner (is_running() is already True by then).
            self.runs.append(run)
            try:
                proc.start()
            except BaseException as e:
                if not proc.is_running():
                    self.runs.remove(run)
                if not isinstance(e, RunnerDeckError):
                    raise
                self.console.error(f"错误：[{name}] 无法启动：{e}")
                self.failed = True
                continue
            run.pid = proc.pid
            self.console.print(f"[{name}] 已启动（PID {proc.pid}），目录 {record.path}")

    def _announce_stop(self) -> None:
        self.console.print()
        self.console.print(
            f"收到 Ctrl+C，正在停止所有 runner（{self._stop_wait_text()}；"
            "再按一次 Ctrl+C 立即强制结束）…"
        )
        if self.skipped:
            self.console.print(f"没有启动：{'、'.join(r.name for r in self.skipped)}")

    def _stop_wait_text(self) -> str:
        timeout = self.ctx.deps.stop_timeout_s
        if timeout is not None:
            return f"每个最多等 {timeout:g} 秒"
        return (
            f"空闲的最多等 {process.DEFAULT_STOP_TIMEOUT_S:g} 秒，"
            f"正在执行 job 的会先取消 job，最多等 {process.BUSY_STOP_TIMEOUT_S:g} 秒"
        )

    def _stop_all(self, targets: Sequence[_Run], *, base: int) -> bool:
        """Stop `targets` at the same time. True if the user gave up waiting (3rd Ctrl+C).

        base: Ctrl+C presses already used up (1 after the press that started stopping).
        """
        deps = self.ctx.deps
        for run in targets:
            run.stopper = threading.Thread(
                target=self._stop_one,
                args=(run, deps.stop_timeout_s),
                name=f"stop-{run.record.name}",
                daemon=True,  # a runner we gave up on must not keep the CLI from exiting
            )
            run.stopper.start()
        timeout = deps.stop_timeout_s
        limit = (process.BUSY_STOP_TIMEOUT_S if timeout is None else timeout) + deps.stop_grace_s
        deadline = time.monotonic() + limit
        forced = gave_up = False
        while busy := [run for run in targets if run.busy()]:
            if not forced and self._presses > base:
                forced = True
                self.console.print(
                    "再次收到 Ctrl+C：不再等待 Ctrl+Break，立即强制结束还没停下的 runner"
                    "（再按一次 Ctrl+C 就不再等待）"
                )
                for run in busy:
                    self._force(run)
            elif forced and self._presses > base + 1:
                gave_up = True
                self.console.error("又收到 Ctrl+C：不再等待 runner 停止")
                break
            elif time.monotonic() > deadline:
                self.console.error(f"错误：等了 {limit:g} 秒，仍有 runner 没有停下")
                break
            try:
                busy[0].wait(STOP_POLL_S)
            except KeyboardInterrupt:  # only without our SIGINT handler (off the main thread)
                self._presses += 1
        self._record_stops(targets)
        self._report_stops(targets)
        return gave_up

    def _record_stops(self, targets: Sequence[_Run]) -> None:
        """Stop records for every runner this stop phase ended (graceful or not)."""
        for run in targets:
            if run.result is not None:
                forced = not run.result.graceful or bool(run.force_killed)
            elif not run.proc.is_running():  # ended, but stop() raised (survivors)
                forced = True
            else:
                continue  # may still be running: listed by _report_alive
            self.ctx.manager.record_stop(Path(run.record.path), forced=forced)

    def _stop_one(self, run: _Run, timeout: float | None) -> None:
        try:
            run.result = run.proc.stop(timeout)
        except RunnerDeckError as e:
            run.stop_error = str(e)
        except Exception as e:  # a bug, on a daemon thread: report it rather than lose it
            log.exception("stopping %s failed", run.record.name)
            run.stop_error = f"内部错误：{type(e).__name__}: {e}"

    def _force(self, run: _Run) -> None:
        pid = run.proc.pid
        if pid is None or run.stopper is None or not run.stopper.is_alive():
            return
        run.forcer = threading.Thread(
            target=self._force_one, args=(run, pid), name=f"kill-{run.record.name}", daemon=True
        )
        run.forcer.start()

    def _force_one(self, run: _Run, pid: int) -> None:
        try:
            run.proc.note_forced_kill()  # so its stop() reports a hard kill, not a graceful one
            run.force_killed, run.force_survivors = self.ctx.deps.kill_tree(pid)
        except Exception as e:  # psutil.Error / OSError, or a bug: report it rather than lose it
            log.warning("killing %s (pid %s) failed: %s", run.record.name, pid, e)
            run.force_error = str(e) or type(e).__name__

    def _report_stops(self, targets: Sequence[_Run]) -> None:
        forced_any = False
        for run in targets:
            name = run.record.name
            if run.force_error is not None:
                self.console.error(f"错误：[{name}] 无法强制结束：{run.force_error}")
                self.failed = True
            elif run.force_survivors:
                pids = ", ".join(map(str, run.force_survivors))
                self.console.error(f"错误：[{name}] 强制结束失败，这些进程还在：PID {pids}")
                self.failed = True
            if run.stop_error is not None:
                self.console.error(f"错误：[{name}] {run.stop_error}")
                if not run.proc.is_running():  # the run ended, but some processes survived
                    self.console.error(f"提示：请执行 {PROG} stop {run.key} 结束剩下的进程")
                self.failed = True
                continue
            if run.result is None:
                continue  # still stopping: listed by _report_alive
            killed = list(dict.fromkeys([*run.force_killed, *run.result.killed_pids]))
            pids = ", ".join(map(str, killed)) or "-"
            if run.force_killed:
                forced_any = True
                self.console.print(f"[{name}] 已强制结束（PID {pids}）")
            elif not run.result.graceful:
                forced_any = True
                self.console.print(f"[{name}] 没能正常停止，已强制结束（PID {pids}）")
            else:
                self.console.print(f"[{name}] 已停止")
        if forced_any:
            self.console.print(LINGER_HINT)

    def _report_exits(self, runs: Sequence[_Run]) -> None:
        """Runners that ended on their own, not stopped by this supervisor."""
        for run in runs:
            name = run.record.name
            error = run.proc.last_error
            if error:
                self.console.error(f"错误：[{name}] {error}")
                hint = EXTERNAL_EXIT_HINT.format(prog=PROG, key=run.key)
                self.console.error(f"[{name}] {hint}")
                self.failed = True
            else:
                self.console.print(f"[{name}] 已退出（退出码 {run.proc.exit_code}）")

    def _report_alive(self) -> bool:
        """List every runner that may still be running. True if there is one."""
        alive = [run for run in self.runs if run.proc.is_running()]
        for run in alive:
            pid = run.proc.pid or run.pid
            self.console.error(
                f"错误：[{run.record.name}] 没能确认已停止，可能仍在运行（PID {pid or '未知'}）；"
                f"请执行 {PROG} stop {run.key} 结束它"
            )
        return bool(alive)


def _cmd_start(ctx: _Context, args: argparse.Namespace) -> int:
    config = ctx.config()
    if args.all and args.keys:
        raise UsageError("--all 不能和 runner 名称同时使用")
    if args.all:
        records = list(config.runners)
        if not records:
            raise RunnerDeckError(f"还没有登记任何 runner（用 {PROG} add 添加）")
    elif args.keys:
        records = _unique([resolve_runner(config, key) for key in args.keys])
    else:
        raise UsageError("请指定要启动的 runner，或使用 --all")
    return _Supervisor(ctx, records, config).run()


# --- settings -----------------------------------------------------------------------------


def _cmd_settings_show(ctx: _Context, args: argparse.Namespace) -> int:
    console = ctx.console
    settings = ctx.config().settings
    proxy = _mask_proxy(settings.proxy) if settings.proxy else "未设置（使用环境变量或系统代理）"
    labels = ", ".join(settings.default_labels) or "无"
    for key, value in (
        ("runners_root", settings.runners_root),
        ("default_labels", labels),
        ("poll_interval_s", str(settings.poll_interval_s)),
        ("proxy", proxy),
    ):
        console.print(f"{key:<16} {value}")
    console.print(f"配置文件：{ctx.store.path}")
    console.print(f"日志目录：{paths.log_dir()}")
    return EXIT_OK


def _parse_setting(key: str, raw: str) -> object:
    value = raw.strip()
    if key == "runners_root":
        if not value:
            raise InvalidInputError("runners_root 不能为空")
        return os.path.abspath(os.path.expanduser(value))
    if key == "default_labels":
        return normalize_labels(value)
    if key == "poll_interval_s":
        try:
            return int(value)
        except ValueError:
            raise InvalidInputError("poll_interval_s 必须是整数（秒）") from None
    # proxy
    if value.casefold() in _PROXY_OFF:
        return None
    normalized = value if "://" in value else f"http://{value}"
    try:
        parts = urlsplit(normalized)
        _ = parts.port  # raises ValueError for a port that is not a number in range
    except ValueError:
        raise InvalidInputError(_BAD_PROXY) from None
    if parts.password:
        register_secret(parts.password)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InvalidInputError(_BAD_PROXY)
    return normalized


def _cmd_settings_set(ctx: _Context, args: argparse.Namespace) -> int:
    console = ctx.console
    value = _parse_setting(args.key, args.value)
    config = ctx.config()
    try:
        settings = AppSettings.model_validate({**config.settings.model_dump(), args.key: value})
    except ValidationError as e:
        reason = "；".join(err["msg"] for err in e.errors(include_url=False, include_input=False))
        raise InvalidInputError(f"{args.key} 的值不合法：{reason}") from None
    config.settings = settings
    ctx.store.save(config)
    shown: object = value
    if args.key == "default_labels":
        shown = ", ".join(settings.default_labels) or "无"
    elif args.key == "proxy":
        shown = _mask_proxy(settings.proxy) if settings.proxy else "未设置"
    console.print(f"已设置 {args.key} = {shown}")
    return EXIT_OK


# --- entry point --------------------------------------------------------------------------


def _init_logging(deps: CliDeps, console: Console, debug: bool) -> Path | None:
    try:
        return deps.setup_logging(
            level=logging.DEBUG if debug else logging.INFO, stream=debug, file_name=LOG_FILE_NAME
        )
    except OSError as e:
        console.error(f"警告：无法写入日志文件（{e}），继续运行")
        return None


def main(argv: Sequence[str] | None = None, deps: CliDeps | None = None) -> int:
    """Run one CLI command; returns the process exit code."""
    deps = deps or CliDeps()
    console = Console(deps.stdout or sys.stdout, deps.stderr or sys.stderr)
    parser = build_parser(console)
    try:
        args = parser.parse_args(None if argv is None else list(argv))
    except SystemExit as e:  # --help, --version, argument errors
        return e.code if isinstance(e.code, int) else (EXIT_OK if e.code is None else EXIT_USAGE)

    log_file = _init_logging(deps, console, args.debug)
    log.info("cli command: %s", args.command)
    try:
        ctx = _Context(deps, console)
        return int(args.handler(ctx, args))
    except KeyboardInterrupt:
        console.error("\n已中断")
        return EXIT_INTERRUPTED
    except UsageError as e:
        console.error(f"{PROG}：错误：{e}")
        return EXIT_USAGE
    except RunnerDeckError as e:
        log.info("command %s failed: %s", args.command, e)
        console.error(f"错误：{e}")
        return EXIT_ERROR
    except Exception as e:
        log.exception("command %s crashed", args.command)
        where = f"，详情见日志 {log_file}" if log_file is not None else ""
        console.error(f"内部错误：{type(e).__name__}: {e}{where}")
        if args.debug:
            console.error(traceback.format_exc())
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
