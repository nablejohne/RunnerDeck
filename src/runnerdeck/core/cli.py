"""Command-line front end for debugging and M1 acceptance (docs/m1-design.md §11).

    python -m runnerdeck.core.cli <command> ...

Every business flow lives in manager.py; this module only parses arguments, asks for
confirmation and prints. All output goes through redact(). Exit codes: 0 success, 1 a
RunnerDeckError (printed as "错误：<message>") or a cancelled confirmation, 2 bad arguments,
130 Ctrl+C (except in `start`, where Ctrl+C is the normal way to stop supervising).

Grounded in docs/spike-notes.md:
- §2: users paste the token's name instead of its value, so `login` checks the format first.
- §5: only Ctrl+Break through the shared console stops a runner cleanly. The CLI runs in a
  console, so `start` can stop its runners gracefully; `stop` (another terminal) cannot and
  always hard-kills, after which the GitHub session lingers about 3 minutes.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
import threading
import time
import traceback
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, NoReturn, TextIO
from urllib.parse import urlsplit

import psutil
from pydantic import ValidationError

from runnerdeck import __version__
from runnerdeck.core import logs, paths, process, runner_file, secrets
from runnerdeck.core.errors import InvalidInputError, ProcessError, RunnerDeckError
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
from runnerdeck.core.process import RunnerProcess
from runnerdeck.core.redact import redact, register_secret
from runnerdeck.core.status import display_label
from runnerdeck.core.store import ConfigStore, resolve_runner

log = logging.getLogger(__name__)

PROG = "runnerdeck-cli"
ENV_PAT = "RUNNERDECK_PAT"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

EXPIRY_WARN_DAYS = 7  # warn when the PAT expires sooner than this
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

ReadFn = Callable[[str], str]
CliClientFactory = Callable[[str, str | None], GitHubClient]  # (pat, proxy) -> client
ManagerFactory = Callable[[ConfigStore, ClientFactory], RunnerManager]


class NotLoggedInError(RunnerDeckError):
    """No PAT: neither RUNNERDECK_PAT nor a stored PAT for config.account."""


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

    stdout / stderr None mean sys.stdout / sys.stderr at call time. read_secret / read_line are
    getpass.getpass / input (prompt text is passed already redacted). client_factory builds a
    GitHubClient from (pat, settings.proxy); manager_factory gets the store and a zero-argument
    client factory that resolves the PAT lazily (RUNNERDECK_PAT > keyring by config.account).
    """

    stdout: TextIO | None = None
    stderr: TextIO | None = None
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    read_secret: ReadFn = getpass.getpass
    read_line: ReadFn = input
    store_factory: Callable[[], ConfigStore] = ConfigStore
    client_factory: CliClientFactory = _default_client
    manager_factory: ManagerFactory = _default_manager
    stop_external: Callable[[Path], list[int]] = process.stop_external
    setup_logging: Callable[..., Path] = logs.setup_logging
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], datetime] = _utcnow
    poll_interval_s: float = 0.5  # `start`: how often to check whether runners are still alive
    stop_timeout_s: float = process.DEFAULT_STOP_TIMEOUT_S


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
    p.add_argument("-y", "--yes", action="store_true", help="不询问，直接确认")

    p = command("import", "登记一个已经配置好的 runner 目录", _cmd_import)
    p.add_argument("path", help="runner 目录")

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
            env_value = self.deps.environ.get(ENV_PAT, "").strip()
            if env_value:
                register_secret(env_value)
                try:
                    value = secrets.validate_pat_format(env_value)
                except InvalidInputError as e:
                    raise InvalidInputError(f"环境变量 {ENV_PAT}：{e}") from None
                self._pat = (value, f"环境变量 {ENV_PAT}")
            else:
                account = self.config().account
                if not account:
                    raise NotLoggedInError(
                        f"还没有登录：请先运行 {PROG} login（或设置环境变量 {ENV_PAT}）"
                    )
                stored = secrets.load_pat(account)
                if stored is None:
                    raise NotLoggedInError(
                        f"系统凭据管理器里没有 {account} 的 PAT，请重新运行 {PROG} login"
                    )
                self._pat = (stored, "系统凭据管理器")
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


def _format_expiry(expires_at: datetime | None, now: datetime) -> tuple[str, bool]:
    """(text, needs attention)."""
    if expires_at is None:
        return "GitHub 没有返回过期时间（可能设置为永不过期）", False
    stamp = expires_at.isoformat(sep=" ", timespec="minutes")
    remaining = expires_at - now
    if remaining.total_seconds() <= 0:
        return f"{stamp}（已过期，请在 GitHub 重新生成）", True
    days = remaining.days
    if days < EXPIRY_WARN_DAYS:
        left = f"{days} 天" if days else "不到 1 天"
        return f"{stamp}（即将过期：还剩 {left}，请及时在 GitHub 重新生成）", True
    return f"{stamp}（还剩 {days} 天）", False


def permission_hints(user: GitHubUser) -> list[str]:
    """What the PAT needs for RunnerDeck, phrased for its kind (PLAN “PAT 权限”)."""
    if user.token_kind == "fine-grained":
        return [
            "fine-grained PAT 需要：Repository access 里选中要添加 runner 的仓库；"
            "Repository permissions 里 Administration: Read and write（必需），"
            "Metadata: Read（自动包含）"
        ]
    if user.token_kind == "classic":
        scopes = user.scopes or []
        if "repo" in scopes:
            return [
                "classic PAT 已有 repo 权限，可以管理所有仓库的 runner；"
                "更推荐只授权选定仓库的 fine-grained PAT"
            ]
        shown = ", ".join(scopes) if scopes else "无"
        return [
            f"classic PAT 缺少 repo 权限（当前：{shown}），注册和移除 runner 会失败；"
            "请在 GitHub 上为这个 token 勾选 repo"
        ]
    return [
        "无法判断 token 类型。需要的权限：fine-grained PAT 为 Administration: Read and write"
        " + Metadata: Read；classic PAT 为 repo"
    ]


def _print_token_info(ctx: _Context, user: GitHubUser) -> None:
    console = ctx.console
    kinds = {"fine-grained": "fine-grained PAT", "classic": "classic PAT", "unknown": "未知"}
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
            raw = ctx.deps.read_secret(redact("请粘贴 GitHub PAT 的值（输入不会显示）："))
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
        "会在 GitHub 上注销它，删除本地目录"
        + ("（保留 _work）" if args.keep_work else "")
        + "，并删除记录"
    )
    if not args.yes and not ctx.confirm("确认移除？输入 y 继续，其他任意输入取消："):
        console.print("已取消，没有做任何改动")
        return EXIT_ERROR
    try:
        result = ctx.manager.remove_runner(
            record.id, keep_work=args.keep_work, progress=_progress(console)
        )
    except ProcessError as e:
        console.error(f"错误：{e}")
        console.error(f"提示：runner 在另一个终端运行时，可以先执行 {PROG} stop {key}")
        return EXIT_ERROR
    for warning in result.warnings:
        console.print(f"注意：{warning}")
    if result.remote is RemoteRemoval.UNCONFIGURED:
        console.print("GitHub：已通过 config.cmd remove 注销")
    elif result.remote is RemoteRemoval.API_DELETED:
        console.print(f"GitHub：已通过 API 删除（runner id {result.runner_id}）")
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
        try:
            pids = ctx.deps.stop_external(Path(record.path))
        except (psutil.Error, OSError) as e:
            console.error(f"错误：无法结束 runner {record.name}：{e}")
            code = EXIT_ERROR
            continue
        if not pids:
            console.print(f"runner {record.name} 没有在运行")
            continue
        forced = True
        console.print(f"已强制结束 runner {record.name}（PID {', '.join(map(str, pids))}）")
    if forced:
        console.print(LINGER_HINT)
    return code


# --- start: foreground supervision ---------------------------------------------------------


class _Supervisor:
    """Runs several RunnerProcess objects in the foreground until they exit or Ctrl+C."""

    def __init__(self, ctx: _Context, records: Sequence[RunnerRecord]) -> None:
        self.ctx = ctx
        self.console = ctx.console
        self.records = records
        self.running: list[tuple[RunnerRecord, RunnerProcess]] = []
        self.failed = False
        self._hinted: set[str] = set()

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
        try:
            self._start_all()
            if not self.running:
                return EXIT_ERROR
            self.console.print("按 Ctrl+C 停止所有 runner")
            while any(proc.is_running() for _, proc in self.running):
                self.ctx.deps.sleep(self.ctx.deps.poll_interval_s)
        except KeyboardInterrupt:
            self.console.print()
            self.console.print("收到 Ctrl+C，正在停止所有 runner …")
            self._stop_all()
        except Exception:
            self._stop_all()  # never leave runners behind on an unexpected failure
            raise
        else:
            self._report_exits()
        return EXIT_ERROR if self.failed else EXIT_OK

    def _start_all(self) -> None:
        manager = self.ctx.manager
        for record in self.records:
            name = record.name
            try:
                proc = manager.process_for(
                    record, on_line=self._on_line(name), on_state=self._on_state(name)
                )
                proc.start()
            except RunnerDeckError as e:
                self.console.error(f"错误：[{name}] 无法启动：{e}")
                self.failed = True
                continue
            self.running.append((record, proc))
            self.console.print(f"[{name}] 已启动（PID {proc.pid}），目录 {record.path}")

    def _stop_all(self) -> None:
        force = False
        forced_any = False
        for record, proc in self.running:
            if not proc.is_running():
                continue
            name = record.name
            while True:
                try:
                    result = proc.stop(0.0 if force else self.ctx.deps.stop_timeout_s)
                    break
                except KeyboardInterrupt:
                    if force:
                        raise
                    force = True
                    self.console.print("再次收到 Ctrl+C：不再等待，强制结束剩余的 runner")
                except RunnerDeckError as e:
                    self.console.error(f"错误：[{name}] {e}")
                    self.failed = True
                    result = None
                    break
            if result is None:
                continue
            if result.graceful:
                self.console.print(f"[{name}] 已停止")
            else:
                forced_any = True
                pids = ", ".join(map(str, result.killed_pids)) or "-"
                self.console.print(f"[{name}] 没能正常停止，已强制结束（PID {pids}）")
        if forced_any:
            self.console.print(LINGER_HINT)

    def _report_exits(self) -> None:
        for record, proc in self.running:
            if proc.last_error:
                self.console.error(f"错误：[{record.name}] {proc.last_error}")
                self.failed = True
            else:
                self.console.print(f"[{record.name}] 已退出（退出码 {proc.exit_code}）")


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
    return _Supervisor(ctx, records).run()


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
        return deps.setup_logging(level=logging.DEBUG if debug else logging.INFO, stream=debug)
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
