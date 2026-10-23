# M1 设计：core 层接口契约

M1 各模块按本文的接口实现，测试按本文的约定写。对 runner 行为的依据是 `docs/spike-notes.md`（§4 输出、§5 停止、§3 注册/移除）。

## 0. 相对 PLAN.md 的调整

PLAN 的 core 列表之外新增 5 个模块，理由如下：

| 模块 | 作用 | 为什么要单独成模块 |
| --- | --- | --- |
| `errors.py` | 异常层级 | CLI 和 M2 的 Controller 统一捕获 `RunnerDeckError` 并直接显示 `str(e)` |
| `redact.py` + `logs.py` | 全局脱敏 + 日志 | “token 一律脱敏”是横切需求，每个看到密钥的模块都先 `register_secret()` |
| `winproc.py` | Windows 进程/控制台工具 | installer 和 process 共用：按行解码、Ctrl+Break、按目录找 runner 进程、杀进程树 |
| `status.py` | 本地 + 远端状态合并（纯函数） | CLI `status` 现在要用，M3 的 Poller 直接复用 |
| `manager.py` | 添加/移除/导入/启停的业务流程（含回滚） | 否则流程会同时写在 `cli.py` 和 M2 的 `controller.py` 里；有了它，Controller 只负责把调用放进线程 |

已完成（地基，不要改接口，有问题提出来）：`errors.py`、`models.py`、`paths.py`、`redact.py`、`logs.py`、`winproc.py`、`core/ruff.toml`（禁止 core 引入 Qt / app / ui）。

## 1. 通用约定

- Python 3.11，`from __future__ import annotations`，类型标注齐全；代码、注释、docstring 用英文，**给用户看的错误信息用中文**。
- 只抛 `runnerdeck.core.errors` 里的异常（必要时继承出新的子类，写在自己的模块里）；第三方异常（httpx、keyring、OSError、subprocess）在模块边界转换掉，`raise ... from e`。
- 日志：`log = logging.getLogger(__name__)`。凡是拿到 PAT、注册/移除 token 的代码，第一件事是 `redact.register_secret(value)`；凡是要放进异常信息或日志的命令输出，先过 `redact.redact()`。
- 子进程、网络、keyring 都做成**可注入**（构造参数或函数参数，默认用真实实现），这样 macOS / Linux 上能用假实现测试。只在 Windows 上才能跑的测试用 `@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")`；CI 的 test job 跑在 windows-latest 上，这些测试会真正执行。
- 不做 `print`；core 层不和终端交互（交互在 `cli.py`）。
- 回调（进度、输出行、状态变化）可能在工作线程里被调用，docstring 写明线程。

## 2. 测试约定

- 测试放 `tests/core/test_<module>.py`，共用夹具放 `tests/core/conftest.py`（只有一个 agent 负责创建它，见 §12）。
- HTTP 用 `respx` mock，不访问网络。keyring 用内存后端（`keyring.set_keyring(...)`），不碰真实凭据管理器。
- 文件系统用 `tmp_path`；设置 `monkeypatch.setenv("RUNNERDECK_HOME", str(tmp_path))` 把所有应用目录重定向。
- 真实样本在 `tests/fixtures/`（stdout、config 输出）；需要 `.runner` 样本时按 §6 字段手写一个最小 JSON，并标注为合成样本。
- 集成测试放 `tests/integration/`，标 `@pytest.mark.integration`，没有 `RUNNERDECK_TEST_PAT` / `RUNNERDECK_TEST_REPO` 时 skip。
- 目标：core 行覆盖率 ≥ 80%（CI 在 Windows 上统计）。

## 3. `store.py`

```python
class ConfigStore:
    def __init__(self, path: Path | None = None) -> None   # 默认 paths.config_file()
    path: Path
    def load(self) -> AppConfig
        # 文件不存在 -> AppConfig()；JSON 坏或校验失败 -> StoreError（信息里带路径，不吞掉）
        # schema_version 比 CONFIG_SCHEMA_VERSION 新 -> StoreError（不认识就不要覆盖）
    def save(self, config: AppConfig) -> None
        # 原子写：同目录 tempfile -> write -> flush -> os.fsync -> os.replace；失败删除临时文件
        # Windows 上目标文件被占用时 os.replace 会 PermissionError：重试 5 次、每次间隔 0.1s，仍失败 -> StoreError
        # 自动创建父目录；JSON UTF-8、indent=2、ensure_ascii=False；datetime 输出 ISO 8601

def resolve_runner(config: AppConfig, key: str) -> RunnerRecord
    # 依次尝试：id 完全匹配 -> id 前缀(>=6 位且唯一) -> name 完全匹配 -> "owner/repo" 完全匹配
    # 不区分大小写匹配 name / owner/repo；0 个 -> RunnerNotFoundError；多个 -> AmbiguousRunnerError（列出候选）
```

## 4. `secrets.py`

```python
KEYRING_SERVICE = "RunnerDeck"
def validate_pat_format(pat: str) -> str
    # strip 后必须以 github_pat_ / ghp_ / gho_ / ghu_ 开头且长度 >= 36，否则 InvalidInputError
    # 信息提示“请粘贴 token 的值，而不是名字”（spike §2：用户真的填错过），绝不回显输入值
def save_pat(login: str, pat: str) -> None
def load_pat(login: str) -> str | None        # 读到后立刻 register_secret
def delete_pat(login: str) -> None            # 不存在时静默
# keyring 的任何异常 -> SecretStoreError；keyring 后端可通过模块级 set_backend()/直接 keyring.set_keyring 在测试中替换
```

## 5. `github_client.py`

```python
API_URL = "https://api.github.com"
class GitHubClient:
    def __init__(self, pat: str, *, proxy: str | None = None, base_url: str = API_URL,
                 timeout: float = 30.0, transport: httpx.BaseTransport | None = None) -> None
        # register_secret(pat)；headers: Authorization: Bearer, Accept: application/vnd.github+json,
        # X-GitHub-Api-Version: 2022-11-28, User-Agent: RunnerDeck/<version>
        # proxy 为 None 时 trust_env=True（读取 HTTPS_PROXY）；transport 仅供测试
    def close(self) -> None; __enter__ / __exit__
    def get_user(self) -> GitHubUser
        # token_kind: 有 X-OAuth-Scopes 头 -> "classic"（scopes 按逗号拆）；否则 PAT 以 github_pat_ 开头 -> "fine-grained"
        # token_expires_at 取 github-authentication-token-expiration 头（格式如 "2026-12-01 00:00:00 +0800"，解析失败则 None）
    def list_repos(self) -> list[Repo]            # /user/repos?affiliation=owner&per_page=100&sort=full_name，按 Link 分页
    def get_repo(self, owner: str, repo: str) -> Repo
    def create_registration_token(self, owner: str, repo: str) -> RunnerToken   # register_secret(token)
    def create_remove_token(self, owner: str, repo: str) -> RunnerToken         # register_secret(token)
    def list_runners(self, owner: str, repo: str) -> list[RemoteRunner]         # {"total_count","runners"}，也按 Link 分页
    def get_runner(self, owner: str, repo: str, runner_id: int) -> RemoteRunner | None   # 404 -> None
    def delete_runner(self, owner: str, repo: str, runner_id: int) -> bool              # 204 -> True，404 -> False
    def get_runner_release(self, version: str | None = None) -> dict[str, Any]
        # /repos/actions/runner/releases/latest 或 /releases/tags/v{version}；原样返回 JSON，解析在 release.py
    def download(self, url: str, dest: Path,
                 on_progress: Callable[[int, int | None], None] | None = None) -> str
        # 用**另一个不带 Authorization 的 httpx.Client**（同样的 proxy / trust_env）下载，follow_redirects=True
        # 流式写入 dest，边写边算 sha256，返回十六进制小写；中途失败删除 dest 并抛 NetworkError
        # on_progress(已下载字节, 总字节或 None)，在调用线程里调用
```

错误映射（所有方法统一处理，信息用中文，带上 `METHOD path -> status`）：

| 情况 | 异常 |
| --- | --- |
| 401 | `AuthError`（“PAT 无效或已过期”） |
| 403/429 且 `x-ratelimit-remaining: 0` 或有 `retry-after` | `RateLimitError(reset_at=...)` |
| 其他 403 | `PermissionDeniedError`（fine-grained 提示需要 Administration: Read and write） |
| 404（除上面说明可返回 None/False 的方法） | `NotFoundError`（提示“仓库不存在或 PAT 没有访问权限”） |
| 其他 4xx/5xx | `GitHubError` |
| `httpx.TransportError`（含超时、代理） | `NetworkError` |

GitHub 的 JSON 里有 `message` 时拼进错误信息。日志只记录方法、路径、状态码、耗时；绝不记录 headers 和 token 响应体。连接错误用 `httpx.HTTPTransport(retries=2)` 自动重试，不重试 4xx/5xx。

## 6. `release.py` 与 `runner_file.py`

```python
# release.py
RUNNER_PLATFORM = "win-x64"
def extract_sha256_from_body(body: str, platform: str = RUNNER_PLATFORM) -> str | None
    # <!-- BEGIN SHA win-x64 -->hex<!-- END SHA win-x64 -->
def parse_release(data: Mapping[str, Any], platform: str = RUNNER_PLATFORM) -> ReleaseInfo
    # 资产名 ^actions-runner-{platform}-(\d+\.\d+\.\d+)\.zip$；version 去掉 tag 的 v
    # sha256 优先用资产 digest（"sha256:..."），没有再用正文标记；两者都有但不一致 -> ReleaseError；都没有 -> ReleaseError
def sha256_file(path: Path) -> str
class ReleaseCache:
    def __init__(self, cache_dir: Path | None = None)        # 默认 paths.cache_dir()
    def cached_path(self, info: ReleaseInfo) -> Path          # cache_dir / info.asset_name
    def is_cached(self, info: ReleaseInfo) -> bool            # 存在且 sha256 一致
    def ensure(self, client: GitHubClient, info: ReleaseInfo,
               on_progress: Callable[[int, int | None], None] | None = None) -> Path
        # 已缓存直接返回；否则下载到 <name>.part，校验 sha，不一致删 .part 抛 ChecksumMismatchError，一致 os.replace
    def prune(self, keep: int = 2) -> list[Path]              # 只保留最新 keep 个 actions-runner-*.zip（按版本号排序），返回删掉的

# runner_file.py
RUNNER_FILE = ".runner"
def read_runner_file(runner_dir: Path) -> RunnerFileInfo | None
    # 不存在 -> None；utf-8-sig 读；JSON 坏或缺字段 -> RunnerFileError；raw 填整个 dict
def parse_github_url(url: str) -> tuple[str, str]
    # https://github.com/{owner}/{repo}[/]  -> (owner, repo)，用 models.validate_owner_repo 校验；不是仓库级 URL -> InvalidInputError
def is_configured(runner_dir: Path) -> bool         # .runner 和 .credentials 都**存在**（绝不读取 .credentials*）
def looks_like_runner_dir(path: Path) -> bool       # config.cmd 和 run.cmd 都存在
def scan_runner_dirs(root: Path) -> list[Path]      # root 下一层子目录里 is_configured 的，按名称排序；root 不存在 -> []
```

`.runner` 已知字段（来自 runner 源码，第 1 轮 spike 未保存原文）：`agentId`、`agentName`、`poolId`、`poolName`、`serverUrl`、`gitHubUrl`、`workFolder`，可能还有 `useV2Flow`、`serverUrlV2`。

## 7. `installer.py`

```python
CONFIG_TIMEOUT_S = 120
@dataclass(frozen=True)
class CommandResult: returncode: int; output: str     # output 已按行解码（winproc.decode_line）并脱敏
RunFn = Callable[..., subprocess.CompletedProcess[bytes]]   # 签名同 subprocess.run，测试注入假实现

def extract_runner(zip_path: Path, target: Path) -> None
    # target 不存在或为空目录才允许；逐个成员检查不越界（winproc.is_under），否则 InstallError；失败时删除已解压内容
def build_config_command(*, url: str, token: str, name: str, labels: Sequence[str],
                         work_folder: str = "_work", replace: bool = True) -> list[str]
    # ["cmd.exe", "/c", "config.cmd", "--unattended", "--url", url, "--token", token, "--name", name,
    #  ("--labels", ",".join(labels) 仅当非空), "--work", work_folder, ("--replace")]
    # name/labels 先过 models.validate_runner_name / normalize_labels（防 cmd 元字符）
def configure(runner_dir: Path, *, url: str, token: str, name: str, labels: Sequence[str],
              run: RunFn = subprocess.run, timeout: float = CONFIG_TIMEOUT_S) -> CommandResult
def unconfigure(runner_dir: Path, *, token: str, run: RunFn = subprocess.run,
                timeout: float = CONFIG_TIMEOUT_S) -> CommandResult
    # ["cmd.exe", "/c", "config.cmd", "remove", "--token", token]
    # 两者：register_secret(token)；cwd=runner_dir，stdin=DEVNULL，stdout=PIPE，stderr=STDOUT，Windows 上
    # creationflags=CREATE_NO_WINDOW；非零退出码或超时 -> InstallError(output=..., returncode=...)
    # 成功判定参照 spike §3 的 √ 行：configure 需出现 "Runner successfully added" 和 "Settings Saved"，
    # unconfigure 需出现 "Runner removed successfully"；退出码 0 但缺这些行也算失败
def write_env_file(runner_dir: Path, updates: Mapping[str, str | None]) -> None
    # 合并写 runner 目录的 .env（KEY=VALUE 每行），None 表示删除该键，保留其他行和顺序；原子写
def remove_runner_dir(runner_dir: Path, *, keep_work: bool = False) -> None
    # 安全检查：必须 looks_like_runner_dir，否则 InstallError（绝不删任意目录）；keep_work=True 时保留 _work 子目录
    # Windows 上只读文件导致 rmtree 失败时清除只读属性重试
```

## 8. `process.py`

依据 spike §4、§5：只有与 runner 共享控制台的 Ctrl+Break 能干净停止；`stdin=DEVNULL` 必须；强杀后 GitHub 会话残留约 3 分钟。

```python
class EventKind(StrEnum): CONNECTED, CONFLICT, RECONNECTED, VERSION, IDLE, BUSY, COMPLETED, STOPPING, EXITED
@dataclass(frozen=True)
class RunnerEvent: kind: EventKind; detail: str | None = None; result: str | None = None
def parse_line(line: str) -> RunnerEvent | None
    # 正则表见 spike-notes §4（与 scripts/spike_common.classify_line 一致，completed 拆成 detail=job, result=结果）

class OutputStateMachine:      # 纯逻辑，无 IO
    state: LocalState; current_job: str | None; runner_version: str | None
    last_job: tuple[str, str] | None     # (job, result)
    def feed(self, event: RunnerEvent) -> bool   # 状态或 job 有变化返回 True
    # 规则：CONNECTED 不改变状态（冲突重试时也会出现）；CONFLICT -> RECONNECTING；RECONNECTED -> STARTING；
    # IDLE -> IDLE 并清 current_job；BUSY -> BUSY + current_job；COMPLETED -> IDLE、清 current_job、记 last_job；
    # STOPPING -> STOPPING；VERSION 只记版本；EXITED 不改变（进程结束由 RunnerProcess 处理）

class StopMethod(StrEnum): CTRL_BREAK, KILL      # GUI 的 helper 方式等 M0 第 2 轮再加
@dataclass(frozen=True)
class StopResult: graceful: bool; method: StopMethod; exit_code: int | None; killed_pids: list[int]
    # graceful=False 表示强杀：调用方要提示“GitHub 端会话约 3 分钟后才过期，期间重启会显示重连中”

PopenFn = Callable[..., subprocess.Popen[bytes]]
class RunnerProcess:
    def __init__(self, runner_dir: Path, *, env: Mapping[str, str] | None = None,
                 on_line: Callable[[str], None] | None = None,
                 on_state: Callable[[LocalState, str | None], None] | None = None,   # (state, current_job)
                 on_job_finished: Callable[[str, str], None] | None = None,          # (job, result)
                 popen: PopenFn = subprocess.Popen, max_lines: int = 2000) -> None
    state / current_job / runner_version / pid / exit_code / last_error / started_at（属性，线程安全读取）
    def output(self) -> list[str]            # 最近 max_lines 行的拷贝
    def start(self) -> None
        # 已在运行 -> ProcessError；目录下已有不属于本对象的 runner 进程（winproc.find_runner_processes）-> ProcessError
        # Popen([winproc.cmd_exe(), "/c", "run.cmd"], cwd=runner_dir, stdin=DEVNULL, stdout=PIPE, stderr=STDOUT,
        #       creationflags=CREATE_NEW_PROCESS_GROUP（**不加** CREATE_NO_WINDOW，否则 Ctrl+Break 无效）, env=合并后的环境)
        # 状态 STARTING；起一个读取线程：逐行 decode_line -> 环形缓冲 -> on_line -> parse_line -> 状态机 -> on_state / on_job_finished
        # 读到 EOF 后 wait()：我们主动 stop 的 -> STOPPED；否则 cmd.exe 退出码非 0，或 run-helper 输出的
        # Listener 退出码已知且非 0 -> ERROR（真实 run.cmd 在 Listener 崩溃后照样 exit /b 0，见 §13）
    def stop(self, timeout: float = 15.0) -> StopResult
        # 未运行 -> 直接返回 graceful=True, exit_code=None
        # 状态 STOPPING；若 winproc.has_console() 则 send_ctrl_break(pid) 并等待最多 timeout 秒
        # 仍未退出（或无控制台）-> winproc.kill_tree(pid)，再清掉目录下残留的 runner 进程；graceful=False
        # 结束后 join 读取线程；最终状态 STOPPED
    def wait(self, timeout: float | None = None) -> int | None
    def is_running(self) -> bool

def running_processes(runner_dir: Path) -> list[psutil.Process]     # = winproc.find_runner_processes
def stop_external(runner_dir: Path) -> ExternalStop        # ExternalStop(killed, survivors, ok)
    # 结束目录下所有 runner 进程及其父 cmd.exe（父进程命令行含 run.cmd / run-helper.cmd 时才杀）；
    # 用于 CLI 从另一个终端停止，必然是强杀；survivors 非空时调用方必须报错
```

Windows 专属测试：在 `tmp_path` 里放一个假的 `run.cmd`（照搬真实 run.cmd + run-helper 的退出码处理：Listener 崩溃后打印 run-helper 的那行并 `exit /b 0`），用当前 Python 跑一个假 Listener 脚本（逐行打印 spike 样本里的输出，`signal.SIGBREAK` 处理器里打印 `Exiting...` 后退出码 0），验证 STARTING -> IDLE、Ctrl+Break 优雅停止（测试进程没有控制台时 skip 这一项，用 `winproc.has_console()` 判断）、强杀路径、意外退出 -> ERROR。

## 9. `status.py`

```python
STALL_AFTER_S = 60              # 运行中但 GitHub 仍 offline 超过它 -> STALLED（PLAN “超过 60 秒标为异常”）
SESSION_LINGER_S = 240          # 强杀后这段时间内 GitHub 仍 online -> SESSION_LINGERING，而不是 EXTERNAL（spike §5：约 3–3.5 分钟）
@dataclass(frozen=True)
class LocalSnapshot:
    state: LocalState; running: bool; external: bool        # external：目录下有 runner 进程但不归本应用管
    state_since: datetime; last_stop_forced: bool = False; stopped_at: datetime | None = None
    current_job: str | None = None; last_error: str | None = None
@dataclass(frozen=True)
class RemoteSnapshot:
    status: RemoteStatus; busy: bool = False; fetched_at: datetime | None = None
@dataclass(frozen=True)
class DisplayStatus:
    state: DisplayState; label: str; detail: str | None = None   # label 中文，例如“空闲”“运行中：build”
def merge_status(local: LocalSnapshot, remote: RemoteSnapshot | None, *, now: datetime) -> DisplayStatus
def match_remote(record: RunnerRecord, remotes: Sequence[RemoteRunner]) -> RemoteRunner | None
    # 先用 github_runner_id 匹配，匹配不到再按 name（不区分大小写）兜底
```

合并规则（PLAN 表格 + spike 修正），按顺序判断：

1. remote 为 MISSING -> MISSING（“已在 GitHub 端删除”），不论本地状态。
2. 本地 STOPPING -> STOPPING（“停止中”）；本地 ERROR -> ERROR（“异常”，detail = last_error）。
3. 本地运行中：
   - 本地 RECONNECTING -> RECONNECTING（“等待旧会话过期”）
   - remote ONLINE 且 busy -> BUSY（job 名取本地 current_job）；ONLINE 且不 busy -> 本地 BUSY 时仍显示 BUSY（远端有延迟），否则 IDLE
   - remote OFFLINE：自 state_since 起不到 STALL_AFTER_S -> CONNECTING，否则 STALLED
   - remote UNKNOWN/None -> 按本地状态（STARTING/IDLE/BUSY）
4. 本地未运行：
   - external=True -> EXTERNAL（“被其他进程运行”）
   - remote ONLINE：last_stop_forced 且 now - stopped_at < SESSION_LINGER_S -> SESSION_LINGERING；否则 EXTERNAL
   - remote OFFLINE -> STOPPED；UNKNOWN/None -> STOPPED（detail 标注远端未知）

## 10. `manager.py`（第二阶段，依赖上面所有模块）

```python
ProgressFn = Callable[[str, str], None]     # (step_id, 中文说明)，在调用线程里调用
ClientFactory = Callable[[], GitHubClient]

class RunnerManager:
    def __init__(self, store: ConfigStore, client_factory: ClientFactory, *,
                 cache: ReleaseCache | None = None, hostname: str | None = None,
                 installer_run: RunFn = subprocess.run, popen: PopenFn = subprocess.Popen) -> None
    def load_config(self) -> AppConfig
    def plan_add(self, owner: str, repo: str, *, name: str | None = None,
                 labels: Sequence[str] | None = None) -> AddPlan
        # 只读：查仓库（拿 private 标志，用于公开仓库安全提醒）、算默认名称/标签/目标目录、
        # 检查同仓库同名 runner（本地记录 + GitHub 列表）是否已存在
    def add_runner(self, plan: AddPlan, *, progress: ProgressFn | None = None) -> RunnerRecord
        # 步骤：release -> 缓存/下载/校验 -> 解压 -> 注册 token -> configure -> 读 .runner -> 写记录
        # 回滚：解压后任何一步失败都删目录；configure 成功但之后失败，先尝试 unconfigure，失败再 API DELETE
    def remove_runner(self, key: str, *, keep_work: bool = False, progress: ProgressFn | None = None) -> RemoveResult
        # 运行中（目录下有 runner 进程）-> ProcessError，由调用方先停；先读 .runner 拿 agentId（remove 会删掉 .runner，spike §3）
        # unconfigure 成功 -> 删目录；失败/目录不存在 -> API DELETE；404 只有在 list_runners 成功（证明 PAT
        # 能看到仓库）后才算“已删除”，否则 NotFoundError 且本地什么都不删；local_only=True 时完全不访问 GitHub
    def import_runner(self, path: Path) -> RunnerRecord
        # read_runner_file -> parse_github_url -> API 确认 runner 仍存在（同样先确认仓库可见，避免把“无权限”
        # 当成“已删除”；确实不存在 -> RunnerDeckError，建议重新注册）
        # 已登记同一 path -> 返回已有记录
    def scan_unregistered(self) -> list[Path]        # runners_root 下已配置但没登记的目录
    def statuses(self, processes: Mapping[str, RunnerProcess] | None = None) -> list[RunnerStatusRow]
        # 每个记录：本地快照（processes 里有就用，否则看目录下是否有外部进程）+ 按仓库分组的 list_runners 结果
        # -> status.merge_status；单个仓库请求失败只把该仓库标 UNKNOWN，不影响其他
    def process_for(self, record: RunnerRecord, **callbacks) -> RunnerProcess
```

`AddPlan`、`RemoveResult`、`RunnerStatusRow` 是 manager 模块内的 frozen dataclass，字段由实现者确定并写 docstring。

## 11. `cli.py`（第三阶段）

`python -m runnerdeck.core.cli <command>`（同时注册 `runnerdeck-cli` 脚本入口）。所有输出过 `redact()`；`RunnerDeckError` -> stderr 打印 `错误：<信息>`，退出码 1；参数错误退出码 2。

| 命令 | 行为 |
| --- | --- |
| `login` | `getpass` 读 PAT（也接受环境变量 `RUNNERDECK_PAT`）-> 格式检查 -> `get_user` -> 存 keyring，`config.account = login`；打印 token 类型、过期时间、所需权限提醒 |
| `logout` | 删 keyring 里的 PAT，清空 account |
| `whoami` | 显示登录名和 token 信息 |
| `repos` | 列出仓库：公开/私有、是否已有本机 runner |
| `add <owner/repo 或 repo>` | `--name` `--labels a,b` `--root` `--yes`；公开仓库给出 fork PR 风险提醒，没有 `--yes` 时要求输入 y 确认；逐步打印进度 |
| `list` | 本地记录 |
| `status` | 合并状态表（本地 + GitHub） |
| `start <key...>` / `start --all` | 前台监管：启动后持续输出带 runner 名前缀的日志和状态变化，Ctrl+C 时对所有 runner 执行优雅停止；有一个停不下来则强杀并提示会话残留 |
| `stop <key>` | 从另一个终端停止（`stop_external`，必然强杀），打印会话残留提醒 |
| `remove <key>` | `--keep-work` `--yes`；运行中先提示用 `stop` |
| `import <path>` | 导入已有 runner 目录 |
| `scan` | 列出 runners_root 下未登记的 runner 目录 |
| `settings [show]` / `settings set <key> <value>` | 支持 runners_root、default_labels、poll_interval_s、proxy |

PAT 来源优先级：`RUNNERDECK_PAT` 环境变量 > keyring（按 `config.account`）。没有 PAT 时提示先 `login`。

## 12. 文件归属（并行实现时互不覆盖）

| 负责 | 文件 |
| --- | --- |
| A | `core/store.py`、`core/secrets.py`、`tests/core/test_store.py`、`tests/core/test_secrets.py`、`tests/core/conftest.py`、`tests/core/test_foundation.py`（地基模块的测试） |
| B | `core/github_client.py`、`tests/core/test_github_client.py`、`tests/integration/test_github_live.py` |
| C | `core/release.py`、`core/runner_file.py`、`tests/core/test_release.py`、`tests/core/test_runner_file.py` |
| D | `core/installer.py`、`tests/core/test_installer.py` |
| E | `core/process.py`、`tests/core/test_process.py`、`tests/core/fake_runner/`（假 run.cmd + 假 Listener） |
| F | `core/status.py`、`tests/core/test_status.py` |
| G（第二阶段） | `core/manager.py`、`tests/core/test_manager.py` |
| H（第三阶段） | `core/cli.py`、`tests/core/test_cli.py` |

`tests/core/conftest.py` 只由 A 创建，内容：`isolated_home`（设置 RUNNERDECK_HOME 到 tmp_path）、`memory_keyring`（内存 keyring 后端）两个夹具。其他人需要共用夹具时在自己的测试文件里定义，不要改 conftest。不要改 `pyproject.toml`、`uv.lock`、地基模块；需要改就在汇报里说明。

## 13. 实现后的修订（以代码为准）

M1 实现和对抗评审（16 条确认问题，见 PROGRESS.md）之后，接口相对上文的变化：

**winproc.py**
- `child_environment(overrides, *, base=None)`：给 config.cmd / run.cmd 的环境 = 本进程环境去掉 `SENSITIVE_ENV_VARS`（`RUNNERDECK_PAT`、`RUNNERDECK_TEST_PAT`、`GH_TOKEN`、`GITHUB_TOKEN` 等）再叠加 overrides。原因：workflow job 继承 run.cmd 的环境，fork PR 可读到 PAT。
- `cmd_exe()`：`%SystemRoot%\System32\cmd.exe` 绝对路径，防止 cwd / PATH 里被放了假的 cmd.exe。
- `kill_tree_report` / `kill_processes_report` 返回 `(gone, alive)`；`kill_tree` / `kill_processes` 只返回确认已结束的 pid。

**github_client.py**：TLS 用 `truststore` 信任 Windows 系统证书库（国内常见的 TLS 代理/加速器把根证书装在系统库里），`SSL_CERT_FILE` 叠加生效，`SSL_CERT_DIR` 在 Windows/macOS 上不生效；不承载 https 流量的 SOCKS 环境代理不再阻止创建客户端。

**installer.py**
- `configure` / `unconfigure` 新增 `env=`（manager 传入代理），子进程环境总是经过 `child_environment`。
- 默认 `run=run_command`：超时时结束整棵进程树（`subprocess.run` 只杀 cmd.exe，孙进程 Listener 占着管道会卡死）。
- 成功判定接受 `REGISTERED_MARKERS`（`Runner successfully added` 或 `--replace` 时的 `Successfully replaced the runner`）；后者来自 runner 源码，待 spike 第 2 轮实测。
- `harden_runner_dir()`：Windows 上用 icacls 关闭继承，只授权当前用户、SYSTEM、Administrators（`D:\` 根下默认继承 Authenticated Users: 修改）。失败只记警告。**runners_root 本身未加固**（其他用户可抢先建同名目录），留待 M3/M4。
- 解压被 Ctrl+C 打断也会清理目录；`remove_runner_dir` 对不存在的目录是空操作，拒绝符号链接。

**process.py**
- ERROR 判定：见 §8 修订。run-helper 的固定输出映射为 Listener 退出码：`terminated error` -> 1、`retryable error` -> 2、`because of updating` -> 3、`re-launch ephemeral` -> 4、`Session Conflict error` -> 5（措辞来自 runner 源码，待实测）；2/3/4 是重启循环，不结束运行。新属性 `listener_exit_code`。
- `stop()` 强杀后仍有存活进程 -> `ProcessError` 列出 pid；`note_forced_kill()` 供 CLI 第二次 Ctrl+C 自己杀进程树前调用，使这次停止记为强杀（`last_stop_forced`，M3 状态合并要用）。
- `stop_external()` 返回 `ExternalStop`。

**status.py**：STALLED 的文案是“连接异常”（与 ERROR 的“异常”区分）；阈值边界：`>= 60s` 为 STALLED，`>= 240s` 不再算会话残留。**已知缺口（推迟到 M3）**：强杀信息只在内存里，换进程（另一个终端 `stop`、重启应用）后无法判断会话残留，GitHub 仍显示 online 时会被标成 EXTERNAL。

**manager.py**：`remove_runner(..., local_only=False)`，`local_only=True` 不访问 GitHub、只删目录和记录（`RemoteRemoval.SKIPPED`），用于仓库已删除或 PAT 已无权限；`RollbackError` 表示回滚未完成；`plan_add(..., root=, version=)`；`process_for` 只传代理覆盖项。

**cli.py**
- 新选项：`remove --local-only`、`add --version x.y.z`、`add` 接受仓库 URL、`-y`、全局 `--debug` / `-V`、`stop` 可接多个 key。
- `start` 的 Ctrl+C：第一次并行地对所有 runner 优雅停止；第二次直接强杀仍在停止中的（Windows 上优雅等待本身不可打断）；第三次退出（130）。只要有我们启动的 runner 可能还活着，退出码就不是 0，并提示 `stop <name>`。
- `stop`：有进程没杀掉 -> 退出码 1 并列出 pid。
- 日志写 `runnerdeck-cli.log`（托盘应用用 `runnerdeck.log`），避免两个进程互相轮转失败。
