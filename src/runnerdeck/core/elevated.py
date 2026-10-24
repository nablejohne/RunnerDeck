"""The elevated half of core/elevate.py: `runnerdeck-cli elevated <request>` (m4-design §2.3).

Runs as administrator after a UAC prompt. Reads the request and deletes it at once, performs
exactly one whitelisted operation, writes `<request>.result.json` and exits 0 (ok) or 1. Every
value is validated before use; config.cmd is only ever reached through core/installer.py.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from runnerdeck.core import diag, installer, runner_file, service
from runnerdeck.core.elevate import REQUEST_VERSION, RESULT_SUFFIX, ElevatedResult
from runnerdeck.core.errors import InstallError, InvalidInputError, RunnerDeckError
from runnerdeck.core.redact import redact, register_secret

log = logging.getLogger(__name__)

# A freshly installed service whose Listener cannot start quits within a second or two
# (spike-notes §12); watch it this long before reporting success.
STAYS_RUNNING_S = 10.0
_SID_RE = re.compile(r"^S-1-\d+(?:-\d+)+$")


class _Context:
    def __init__(
        self,
        *,
        services: service.ServiceOps,
        run: installer.RunFn,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ) -> None:
        self.services = services
        self.run = run
        self.sleep = sleep
        self.clock = clock


def main(
    request_path: str,
    *,
    services: service.ServiceOps | None = None,
    run: installer.RunFn = installer.run_command,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    request = Path(request_path)
    result_path = request.with_name(request.stem + RESULT_SUFFIX)
    try:
        text = request.read_text(encoding="utf-8")
    except OSError as e:
        log.error("cannot read elevated request %s: %s", request, e)
        return 1
    finally:
        try:
            request.unlink(missing_ok=True)
        except OSError as e:
            log.warning("cannot delete %s: %s", request, e)
    ctx = _Context(services=services or service.default_ops(), run=run, sleep=sleep, clock=clock)
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("version") != REQUEST_VERSION:
            raise InvalidInputError("管理员操作的请求格式不对")
        for key in ("token", "remove_token"):
            if isinstance(data.get(key), str):
                register_secret(data[key])
        op = data.get("op")
        handler = _OPS.get(op) if isinstance(op, str) else None
        if handler is None:
            raise InvalidInputError(f"不支持的管理员操作：{op!r}")
        log.info("elevated op %s", op)
        result = handler(ctx, data)
    except RunnerDeckError as e:
        result = ElevatedResult(ok=False, error=redact(str(e)), output=_output_of(e))
    except Exception as e:  # noqa: BLE001 - the caller must always get a result file
        log.exception("elevated op failed")
        result = ElevatedResult(ok=False, error=redact(f"内部错误：{type(e).__name__}: {e}"))
    try:
        result_path.write_text(result.to_json(), encoding="utf-8")
    except OSError as e:
        log.error("cannot write %s: %s", result_path, e)
        return 1
    return 0 if result.ok else 1


# --- validation --------------------------------------------------------------------------


def _str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidInputError(f"缺少参数：{key}")
    return value


def _runner_dir(data: Mapping[str, Any], *, must_exist: bool = True) -> Path:
    path = Path(_str(data, "runner_dir"))
    if not path.is_absolute():
        raise InvalidInputError(f"runner 目录必须是绝对路径：{path}")
    if must_exist and not runner_file.looks_like_runner_dir(path):
        raise InvalidInputError(f"{path} 不是 runner 目录（缺少 config.cmd 或 run.cmd）")
    return path


def _env(data: Mapping[str, Any]) -> dict[str, str] | None:
    env = data.get("env")
    if env is None:
        return None
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise InvalidInputError("env 参数格式不对")
    return dict(installer.validate_env_updates(env))  # type: ignore[arg-type]


def _sid(data: Mapping[str, Any]) -> str | None:
    sid = data.get("grant_sid")
    if sid is None:
        return None
    if not isinstance(sid, str) or not _SID_RE.fullmatch(sid):
        raise InvalidInputError(f"用户 SID 不合法：{sid!r}")
    return sid


def _labels(data: Mapping[str, Any]) -> list[str]:
    labels = data.get("labels", [])
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise InvalidInputError("labels 参数格式不对")
    return labels


def _output_of(e: BaseException) -> str:
    return e.output if isinstance(e, InstallError) else ""


def _agent_id(runner_dir: Path) -> int | None:
    try:
        info = runner_file.read_runner_file(runner_dir)
    except RunnerDeckError:
        return None
    return None if info is None else info.agent_id


# --- operations --------------------------------------------------------------------------


def _install(ctx: _Context, data: Mapping[str, Any], runner_dir: Path) -> ElevatedResult:
    """config.cmd --runasservice, grant, watch the service; undo on failure (remove_token)."""
    env = _env(data)
    remove_token = data.get("remove_token")
    try:
        configured = installer.configure(
            runner_dir,
            url=_str(data, "url"),
            token=_str(data, "token"),
            name=_str(data, "name"),
            labels=_labels(data),
            run=ctx.run,
            env=env,
            run_as_service=True,
        )
    except InstallError as e:
        registered = installer.registration_happened(runner_dir, e)
        return _undo(ctx, runner_dir, remove_token, env, str(e), e.output, registered, None)
    output = configured.output
    name = service.read_service_name(runner_dir)
    if name is None:
        return _undo(
            ctx,
            runner_dir,
            remove_token,
            env,
            "注册成功，但没有生成 .service 文件",
            output,
            True,
            None,
        )
    warnings: list[str] = []
    sid = _sid(data)
    if sid is not None:
        try:
            ctx.services.grant_control(name, sid)
        except RunnerDeckError as e:
            warnings.append(f"没能授予你启停这个服务的权限，之后启停都需要管理员授权：{e}")
    info = _watch(ctx, name)
    if info.state is not service.ServiceState.RUNNING:
        reason = (
            diag.last_terminal_error(runner_dir)
            or f"服务状态 {info.state}，退出码 {info.exit_code}"
        )
        return _undo(
            ctx, runner_dir, remove_token, env, f"服务启动后退出：{reason}", output, True, name
        )
    return ElevatedResult(
        ok=True,
        output=output,
        service_name=name,
        agent_id=_agent_id(runner_dir),
        registered=True,
        service_exists=True,
        warnings=tuple(warnings),
    )


def _watch(ctx: _Context, name: str) -> service.ServiceInfo:
    """RUNNING for STAYS_RUNNING_S, or the first state that is not RUNNING / START_PENDING."""
    deadline = ctx.clock() + STAYS_RUNNING_S
    info = ctx.services.query(name)
    while ctx.clock() < deadline:
        if info.state not in (service.ServiceState.RUNNING, service.ServiceState.START_PENDING):
            return info
        ctx.sleep(1.0)
        info = ctx.services.query(name)
    return info


def _undo(
    ctx: _Context,
    runner_dir: Path,
    remove_token: Any,
    env: Mapping[str, str] | None,
    error: str,
    output: str,
    registered: bool,
    name: str | None,
) -> ElevatedResult:
    """A failed install: config.cmd remove (also deletes the service) when it registered."""
    warnings: list[str] = []
    if registered and isinstance(remove_token, str):
        try:
            installer.unconfigure(runner_dir, token=remove_token, run=ctx.run, env=env)
            registered = False
        except RunnerDeckError as e:
            warnings.append(f"撤销注册失败：{e}")
    exists = False
    if name is not None:
        if registered:  # config.cmd remove did not run or failed: at least drop the service
            try:
                ctx.services.delete(name)
            except RunnerDeckError as e:
                warnings.append(f"删除服务 {name} 失败：{e}")
        exists = ctx.services.query(name).state is not service.ServiceState.MISSING
    return ElevatedResult(
        ok=False,
        error=redact(error),
        output=output,
        service_name=name,
        agent_id=_agent_id(runner_dir) if registered else None,
        registered=registered,
        service_exists=exists,
        warnings=tuple(warnings),
    )


def _op_install_service(ctx: _Context, data: Mapping[str, Any]) -> ElevatedResult:
    return _install(ctx, data, _runner_dir(data))


def _op_convert_to_service(ctx: _Context, data: Mapping[str, Any]) -> ElevatedResult:
    """Unregister the process-mode runner, register it again as a service (spike-notes §12:
    config.cmd refuses a configured directory). If the service install fails, register it
    again in process mode with the same token so the runner is not left unregistered."""
    runner_dir = _runner_dir(data)
    env = _env(data)
    try:
        installer.unconfigure(runner_dir, token=_str(data, "remove_token"), run=ctx.run, env=env)
    except InstallError as e:
        return ElevatedResult(
            ok=False, error=redact(f"注销原来的 runner 失败：{e}"), output=e.output, registered=True
        )
    result = _install(ctx, {**data, "remove_token": data.get("second_remove_token")}, runner_dir)
    if result.ok or result.registered:
        return result
    try:
        installer.configure(
            runner_dir,
            url=_str(data, "url"),
            token=_str(data, "token"),
            name=_str(data, "name"),
            labels=_labels(data),
            run=ctx.run,
            env=env,
        )
    except InstallError as e:
        warning = f"重新按进程模式注册也失败了，runner 现在没有注册：{e}"
        return _replace(result, warnings=(*result.warnings, warning))
    return _replace(
        result,
        registered=True,
        agent_id=_agent_id(runner_dir),
        warnings=(*result.warnings, "已按进程模式重新注册（GitHub 上的 id 变了）"),
    )


def _replace(result: ElevatedResult, **changes: Any) -> ElevatedResult:
    from dataclasses import replace

    return replace(result, **changes)


def _op_remove_service(ctx: _Context, data: Mapping[str, Any]) -> ElevatedResult:
    """config.cmd remove (stops and deletes the service, spike-notes §12); a missing or
    unconfigured directory only gets its service deleted (the caller DELETEs via the API)."""
    name = service.check_service_name(_str(data, "service_name"))
    runner_dir = _runner_dir(data, must_exist=False)
    if runner_file.looks_like_runner_dir(runner_dir) and runner_file.is_configured(runner_dir):
        try:
            removed = installer.unconfigure(
                runner_dir, token=_str(data, "remove_token"), run=ctx.run, env=_env(data)
            )
        except InstallError as e:
            warnings = []
            try:
                ctx.services.delete(name)
            except RunnerDeckError as e2:
                warnings.append(f"删除服务 {name} 失败：{e2}")
            exists = ctx.services.query(name).state is not service.ServiceState.MISSING
            return ElevatedResult(
                ok=False,
                error=redact(f"config.cmd remove 失败：{e}"),
                output=e.output,
                service_name=name,
                registered=True,
                service_exists=exists,
                warnings=tuple(warnings),
            )
        exists = ctx.services.query(name).state is not service.ServiceState.MISSING
        if exists:
            ctx.services.delete(name)
            exists = ctx.services.query(name).state is not service.ServiceState.MISSING
        return ElevatedResult(
            ok=True, output=removed.output, service_name=name, service_exists=exists
        )
    ctx.services.delete(name)
    exists = ctx.services.query(name).state is not service.ServiceState.MISSING
    return ElevatedResult(ok=True, service_name=name, registered=True, service_exists=exists)


def _op_control_service(ctx: _Context, data: Mapping[str, Any]) -> ElevatedResult:
    name = service.check_service_name(_str(data, "service_name"))
    action = _str(data, "action")
    if action == "start":
        ctx.services.start(name)
    elif action == "stop":
        ctx.services.stop(name)
    else:
        raise InvalidInputError(f"不支持的服务操作：{action!r}")
    return ElevatedResult(ok=True, service_name=name, service_exists=True)


def _op_grant_control(ctx: _Context, data: Mapping[str, Any]) -> ElevatedResult:
    name = service.check_service_name(_str(data, "service_name"))
    sid = _sid(data)
    if sid is None:
        raise InvalidInputError("缺少参数：grant_sid")
    ctx.services.grant_control(name, sid)
    return ElevatedResult(ok=True, service_name=name, service_exists=True)


_OPS: dict[str, Callable[[_Context, Mapping[str, Any]], ElevatedResult]] = {
    "install_service": _op_install_service,
    "convert_to_service": _op_convert_to_service,
    "remove_service": _op_remove_service,
    "control_service": _op_control_service,
    "grant_control": _op_grant_control,
}
