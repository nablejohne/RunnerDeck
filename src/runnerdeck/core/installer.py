"""Unpack, register, unregister and delete a runner directory (config.cmd lives only here).

Spike §3: config.cmd prints an ASCII banner followed by `√ ...` lines; success is judged by
those lines, not by the exit code alone. Output is decoded per line (UTF-8 first, console
code page second) and redacted before it reaches an exception, a log or the caller.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runnerdeck.core import winproc
from runnerdeck.core.errors import InstallError, InvalidInputError
from runnerdeck.core.models import normalize_labels, validate_runner_name
from runnerdeck.core.redact import redact, register_secret
from runnerdeck.core.runner_file import looks_like_runner_dir

log = logging.getLogger(__name__)

CONFIG_TIMEOUT_S = 120
CONFIG_CMD = "config.cmd"
RUN_CMD = "run.cmd"
ENV_FILE = ".env"
WORK_DIR = "_work"

# Lines config.cmd prints on success (tests/fixtures/config/*.txt); each must appear somewhere.
# Each entry is satisfied by any one of its alternatives. With --replace and an existing
# runner of the same name, actions/runner prints "Successfully replaced the runner" instead
# (from the runner source; not yet captured in a spike).
# Server-side registration happened (also used by manager to decide whether to roll back).
REGISTERED_MARKERS: tuple[str, ...] = (
    "Runner successfully added",
    "Successfully replaced the runner",
)
CONFIGURE_SUCCESS_LINES: tuple[str | tuple[str, ...], ...] = (REGISTERED_MARKERS, "Settings Saved")
UNCONFIGURE_SUCCESS_LINES: tuple[str | tuple[str, ...], ...] = ("Runner removed successfully",)

# After killing the process tree on timeout, how long to wait for the output pipe to close.
_DRAIN_TIMEOUT_S = 5.0
_MESSAGE_LINE_MAX = 200

# Everything placed on the `cmd.exe /c config.cmd ...` line must be free of cmd.exe
# metacharacters (& | < > ^ % " spaces ...). Name and labels are checked in models.py.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]{8,256}$")
_URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/[A-Za-z0-9._-]+)+/?$")
_WORK_FOLDER_RE = re.compile(r"^[A-Za-z0-9._:\\/-]{1,200}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")

RunFn = Callable[..., subprocess.CompletedProcess[bytes]]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str  # decoded per line (winproc.decode_line) and redacted


# --- running config.cmd ----------------------------------------------------------------


def run_command(
    args: Sequence[str], *, timeout: float | None = None, **popen_kwargs: Any
) -> subprocess.CompletedProcess[bytes]:
    """Drop-in for subprocess.run(args, timeout=..., **popen_kwargs) without `check`/`input`.

    Unlike subprocess.run, a timeout kills the whole process tree before collecting output:
    config.cmd's Runner.Listener.exe is a grandchild that inherits the stdout pipe, so killing
    only cmd.exe would leave communicate() blocked until the Listener exits on its own.
    """
    proc = subprocess.Popen(list(args), **popen_kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        partial: bytes | None = e.output  # POSIX fills this; Windows leaves it None
    except BaseException:
        _kill_tree(proc)
        raise
    else:
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    _kill_tree(proc)
    try:
        out, err = proc.communicate(timeout=_DRAIN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        out, err = partial or b"", None  # something we could not kill still holds the pipe
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_DRAIN_TIMEOUT_S)
    raise subprocess.TimeoutExpired(proc.args, timeout or 0.0, output=out, stderr=err)


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(Exception):
        winproc.kill_tree(proc.pid)
    with contextlib.suppress(OSError):
        proc.kill()


def _creationflags() -> int:
    return winproc.CREATE_NO_WINDOW if winproc.IS_WINDOWS else 0


def _check_token(token: str) -> None:
    if not _TOKEN_RE.fullmatch(token):
        # Never echo the value: it is a credential.
        raise InvalidInputError("runner token 格式不合法（只能包含字母、数字、. _ -，长度 8–256）")


def build_config_command(
    *,
    url: str,
    token: str,
    name: str,
    labels: Sequence[str],
    work_folder: str = WORK_DIR,
    replace: bool = True,
) -> list[str]:
    """Argument list for `config.cmd --unattended`; every value is validated against cmd.exe
    metacharacters first (InvalidInputError)."""
    register_secret(token)
    _check_token(token)
    if not _URL_RE.fullmatch(url):
        raise InvalidInputError(f"仓库 URL 不合法：{url!r}")
    if not _WORK_FOLDER_RE.fullmatch(work_folder):
        raise InvalidInputError(f"工作目录名不合法：{work_folder!r}")
    name = validate_runner_name(name)
    label_list = normalize_labels(labels if isinstance(labels, str) else list(labels))

    args = ["cmd.exe", "/c", CONFIG_CMD, "--unattended", "--url", url, "--token", token]
    args += ["--name", name]
    if label_list:
        args += ["--labels", ",".join(label_list)]
    args += ["--work", work_folder]
    if replace:
        args.append("--replace")
    return args


def build_remove_command(*, token: str) -> list[str]:
    register_secret(token)
    _check_token(token)
    return ["cmd.exe", "/c", CONFIG_CMD, "remove", "--token", token]


def configure(
    runner_dir: Path,
    *,
    url: str,
    token: str,
    name: str,
    labels: Sequence[str],
    run: RunFn = run_command,
    timeout: float = CONFIG_TIMEOUT_S,
) -> CommandResult:
    """Register the runner in `runner_dir` with GitHub (blocking, up to `timeout` seconds)."""
    register_secret(token)
    args = build_config_command(url=url, token=token, name=name, labels=labels)
    return _run_config(
        runner_dir,
        args,
        action="runner 注册",
        success_lines=CONFIGURE_SUCCESS_LINES,
        run=run,
        timeout=timeout,
    )


def unconfigure(
    runner_dir: Path, *, token: str, run: RunFn = run_command, timeout: float = CONFIG_TIMEOUT_S
) -> CommandResult:
    """`config.cmd remove`: unregister on GitHub and delete .runner / .credentials (spike §3).

    Read .runner (agentId) before calling this; it is gone afterwards.
    """
    register_secret(token)
    args = build_remove_command(token=token)
    return _run_config(
        runner_dir,
        args,
        action="runner 注销",
        success_lines=UNCONFIGURE_SUCCESS_LINES,
        run=run,
        timeout=timeout,
    )


def _run_config(
    runner_dir: Path,
    args: list[str],
    *,
    action: str,
    success_lines: Sequence[str | tuple[str, ...]],
    run: RunFn,
    timeout: float,
) -> CommandResult:
    if not (runner_dir / CONFIG_CMD).is_file():
        raise InstallError(f"{action}失败：{runner_dir} 下没有 {CONFIG_CMD}")
    log.info("%s: %s (cwd=%s)", action, redact(subprocess.list2cmdline(args)), runner_dir)
    started = time.monotonic()
    try:
        proc = run(
            args,
            cwd=runner_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            creationflags=_creationflags(),
        )
    except subprocess.TimeoutExpired as e:
        output = _decode_output(e.output)
        log.warning("%s timed out after %gs; output:\n%s", action, timeout, output)
        # `from None`: TimeoutExpired's message embeds the command line, i.e. the token.
        raise InstallError(
            f"{action}超时（超过 {timeout:g} 秒），已结束 config.cmd", output=output
        ) from None
    except OSError as e:
        raise InstallError(f"{action}失败：无法启动 config.cmd（{redact(str(e))}）") from e

    output = _decode_output(proc.stdout)
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        log.warning(
            "%s failed, rc=%s in %.1fs; output:\n%s", action, proc.returncode, elapsed, output
        )
        last = _last_line(output)
        raise InstallError(
            f"{action}失败（config.cmd 退出码 {proc.returncode}）" + (f"：{last}" if last else ""),
            output=output,
            returncode=proc.returncode,
        )
    lines = output.splitlines()
    alternatives = [(m,) if isinstance(m, str) else m for m in success_lines]
    missing = [
        alts[0] for alts in alternatives if not any(a in line for a in alts for line in lines)
    ]
    if missing:
        log.warning("%s: rc=0 but no %s; output:\n%s", action, missing, output)
        quoted = "、".join(f"“{m}”" for m in missing)
        raise InstallError(
            f"{action}未完成：config.cmd 退出码为 0，但输出中没有 {quoted}",
            output=output,
            returncode=proc.returncode,
        )
    log.info("%s ok in %.1fs", action, elapsed)
    return CommandResult(returncode=proc.returncode, output=output)


def _decode_output(raw: bytes | None) -> str:
    if not raw:
        return ""
    # config.cmd ends lines with \r\r\n; decode_line strips any trailing \r / \n run.
    lines = [redact(winproc.decode_line(chunk)) for chunk in raw.split(b"\n")]
    return "\n".join(lines).strip("\n")


def _last_line(output: str) -> str:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line:
            return line[:_MESSAGE_LINE_MAX]
    return ""


# --- unpacking ---------------------------------------------------------------------------


def extract_runner(zip_path: Path, target: Path) -> None:
    """Unpack the runner zip into `target`, which must be missing or an empty directory.

    Every member is checked before anything is written (no absolute paths, drive letters or
    `..`); on any failure whatever was extracted is removed again.
    """
    if target.exists():
        if not target.is_dir():
            raise InstallError(f"解压目标不是目录：{target}")
        if any(target.iterdir()):
            raise InstallError(f"解压目标目录不为空：{target}")
        created = False
    else:
        created = True

    try:
        zf = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as e:
        raise InstallError(f"无法打开 runner 压缩包 {zip_path}：{e}") from e
    with zf:
        for info in zf.infolist():
            _check_member(info.filename, target)
        started = time.monotonic()
        try:
            target.mkdir(parents=True, exist_ok=True)
            zf.extractall(target)
            missing = [n for n in (CONFIG_CMD, RUN_CMD) if not (target / n).is_file()]
            if missing:
                raise InstallError(f"压缩包内容不是 runner：解压后缺少 {'、'.join(missing)}")
        except Exception as e:
            _discard_extracted(target, created=created)
            if isinstance(e, InstallError):
                raise
            raise InstallError(f"解压 runner 失败：{e}") from e
    log.info("extracted %s to %s in %.1fs", zip_path.name, target, time.monotonic() - started)


def _check_member(name: str, target: Path) -> None:
    parts = re.split(r"[\\/]", name)
    if (
        name.startswith(("/", "\\"))
        or _DRIVE_RE.match(name)
        or ".." in parts
        or not winproc.is_under(target / name, target)
    ):
        raise InstallError(f"runner 压缩包包含不安全的路径：{name!r}")


def _discard_extracted(target: Path, *, created: bool) -> None:
    try:
        if created:
            if target.exists():
                _rmtree(target)
            return
        for child in target.iterdir():
            _remove_path(child, root=target)
    except OSError as e:
        log.warning("could not clean up %s after failed extraction: %s", target, e)


# --- .env --------------------------------------------------------------------------------


def write_env_file(runner_dir: Path, updates: Mapping[str, str | None]) -> None:
    """Merge `updates` into `<runner_dir>/.env` (KEY=VALUE lines read by Runner.Listener).

    None deletes a key. Keys match case-insensitively (Windows environment semantics); the
    first matching line is rewritten in place, later duplicates of an updated key are dropped,
    every other line keeps its position, new keys are appended. Written atomically.
    """
    for key, value in updates.items():
        if not _ENV_KEY_RE.fullmatch(key):
            raise InvalidInputError(f"环境变量名不合法：{key!r}")
        if value is not None and ("\n" in value or "\r" in value):
            raise InvalidInputError(f"环境变量 {key} 的值不能包含换行")
    if not updates:
        return
    if not runner_dir.is_dir():
        raise InstallError(f"runner 目录不存在：{runner_dir}")

    path = runner_dir / ENV_FILE
    exists = path.exists()
    if not exists and all(v is None for v in updates.values()):
        return
    try:
        existing = path.read_bytes().decode("utf-8-sig") if exists else ""
    except (OSError, UnicodeDecodeError) as e:
        raise InstallError(f"无法读取 {path}：{e}") from e

    newline = "\r\n" if "\r\n" in existing else "\n"
    old_lines = _LINE_SPLIT_RE.split(existing)
    if old_lines and old_lines[-1] == "":
        old_lines.pop()

    pending = {key.casefold(): (key, value) for key, value in updates.items()}
    handled: set[str] = set()
    lines: list[str] = []
    for line in old_lines:
        key, sep, _ = line.partition("=")
        folded = key.casefold()
        if not (sep and key and folded in pending):
            lines.append(line)
            continue
        if folded in handled:
            continue
        handled.add(folded)
        new_key, value = pending[folded]
        if value is not None:
            lines.append(f"{new_key}={value}")
    for folded, (new_key, value) in pending.items():
        if folded not in handled and value is not None:
            lines.append(f"{new_key}={value}")

    content = newline.join(lines) + (newline if lines else "")
    _atomic_write_text(path, content)
    log.info("updated %s: %s", path, sorted(updates))  # keys only, values may be secrets


def _atomic_write_text(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise InstallError(f"无法写入 {path}：{e}") from e


# --- deleting ----------------------------------------------------------------------------


def remove_runner_dir(runner_dir: Path, *, keep_work: bool = False) -> None:
    """Delete a runner directory (or, with keep_work, everything in it except `_work`).

    Refuses anything that is not a runner directory (config.cmd + run.cmd) or is a symlink.
    A missing directory is a no-op. Read-only files are made writable and retried.
    """
    if not os.path.lexists(runner_dir):
        log.info("runner dir %s already gone", runner_dir)
        return
    if runner_dir.is_symlink():
        raise InstallError(f"拒绝删除 {runner_dir}：它是符号链接")
    if not runner_dir.is_dir() or not looks_like_runner_dir(runner_dir):
        raise InstallError(
            f"拒绝删除 {runner_dir}：它不像 runner 目录（缺少 {CONFIG_CMD} 或 {RUN_CMD}）"
        )
    try:
        if keep_work:
            for child in runner_dir.iterdir():
                if child.name.casefold() != WORK_DIR:
                    _remove_path(child, root=runner_dir)
        else:
            _rmtree(runner_dir)
    except OSError as e:
        raise InstallError(
            f"删除 runner 目录失败：{e}（可能有程序正在使用其中的文件，请先停止 runner）"
        ) from e
    log.info("removed %s%s", runner_dir, " (kept _work)" if keep_work else "")


def _remove_path(path: Path, *, root: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        _rmtree(path)
    else:
        try:
            os.unlink(path)
        except PermissionError:
            _make_writable(str(path), root)
            os.unlink(path)


def _rmtree(root: Path) -> None:
    def on_error(func: Callable[[str], Any], path: str, exc_info: Any) -> None:
        exc = exc_info[1]
        if isinstance(exc, FileNotFoundError):
            return
        if not isinstance(exc, PermissionError):
            raise exc
        _make_writable(path, root)
        func(path)

    shutil.rmtree(root, onerror=on_error)


def _make_writable(path: str, root: Path) -> None:
    """Clear the read-only attribute (Windows) / add owner write permission (POSIX).

    On POSIX deleting needs a writable parent, so the parent is fixed too while it is still
    inside the tree being removed.
    """
    _add_owner_write(path)
    parent = os.path.dirname(path)
    if winproc.is_under(parent, root):
        _add_owner_write(parent)


def _add_owner_write(path: str) -> None:
    with contextlib.suppress(OSError):
        mode = os.lstat(path).st_mode
        extra = stat.S_IWUSR | stat.S_IRUSR | (stat.S_IXUSR if stat.S_ISDIR(mode) else 0)
        os.chmod(path, stat.S_IMODE(mode) | extra)
