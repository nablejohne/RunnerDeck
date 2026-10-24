# M4 设计：增强与发布（契约）

M4 按本文实现，测试按本文约定写。core 接口见 `docs/m1-design.md`（§13 修订），app/ui/打包见 `docs/m2-design.md`（§10），M3 的托盘 / Poller / 状态规则见 `docs/m3-design.md`（§10），runner 行为依据 `docs/spike-notes.md`。PLAN.md 的 M4 范围：服务模式；扫描 workflow 的 `runs-on: self-hosted` 和最近运行记录；代理同时作用于 runner；release.yml 加 Inno Setup 安装包、`SHA256SUMS.txt`、构建来源证明；可选的检查新版本。另外收掉 M3 遗留：取消注销后 runner 不恢复、`runners_root` 本身的 ACL、详情页的 runner 版本、CLAUDE.md 里的“M4 起 pywin32”。

已确定的决策（用户）：

1. 服务模式完整支持：添加向导可选“以服务运行”；只对单条命令用 UAC（`ShellExecute runas`）提权；支持启停、移除、把进程模式转成服务。
2. 安装包按用户安装到 `%LOCALAPPDATA%\Programs\RunnerDeck`，不需要管理员；开机自启仍写 HKCU。
3. 顺序实现，最后 1 个独立 reviewer；不跑多 agent 工作流。
4. 不发 v0.2.0：M3 的改动随 v1.0.0 一起发，CHANGELOG 的 `[Unreleased]` 并入 1.0.0。
5. 安装服务时给当前用户授启停权限（之后启停不弹 UAC）；服务账号只用 NETWORK SERVICE。

标 **【待 spike】** 的条目依赖 §1 的真机结论，spike 后在 §13 记修订，再开始写对应代码。

## 0. 相对 PLAN.md 的调整

| 调整 | 理由 |
| --- | --- |
| 新增 `core/service.py`（pywin32 `win32service`，SCM 可注入） | 服务查询 / 启停只用 SCM API；不解析 `sc.exe` 的输出（中文系统上是本地化文字） |
| 新增 `core/elevate.py` + CLI 隐藏子命令 `elevated <request>` | UAC 只能整进程提权：用 `ShellExecuteExW(runas)` 起一个 `runnerdeck-cli.exe elevated`，它只执行一条白名单操作 |
| 安装服务时给当前用户授予该服务的启停权限（用户已确认，见 §2.3） | 否则每次启停都弹 UAC；授权只在安装时那一次 UAC 里做 |
| 服务账号固定为 runner 默认的 `NT AUTHORITY\NETWORK SERVICE`（用户已确认） | 自定义账号要收集 Windows 密码再传给 config.cmd（命令行可见），收益不抵风险；留到以后 |
| 代理写进 runner 的 `.env`；进程模式仍同时传环境变量 | 服务不继承 RunnerDeck 的环境，也不读当前用户的系统代理（WinINET 设置是按用户的） |
| 新增 `core/workflows.py`（纯函数）+ 依赖 PyYAML | 解析 `runs-on` 和标签匹配；能直接告诉用户“这个 job 永远等不到这台 runner”（M2 验收里遇到过） |
| “最近运行”按仓库列，不按 runner 过滤 | 按 runner 过滤要逐个 run 查 jobs，调用次数翻几倍 |
| 新增 `app/session_end.py`（`QAbstractNativeEventFilter`） | M3 遗留：`WM_ENDSESSION(wParam=0)` = 注销被取消，重启注销时停掉的 runner |
| `runners_root` 只在 RunnerDeck 自己创建时自动加固；已有目录在设置页手动“加固” | M3 的顾虑仍成立（根目录可能是用户自选的任意目录）；手动加固前先确认 |
| 应用内检查更新只提示、不下载 | PLAN 原定 |
| CI 的 `build` job 也出安装包，并在 runner 上静默安装 / 卸载一遍 | 托管 runner 是干净的 Windows，每次推送都验证“可装可卸” |

## 1. 真机 spike（先做，`ssh XM`）

前提：用户在 XM 上启动 OpenSSH 服务（`Start-Service sshd`）。先确认 SSH 会话的令牌：`whoami /groups` 里有 `Mandatory Label\High Mandatory Level` 即为管理员令牌（Windows OpenSSH 对管理员账户的密钥登录通常是这样）。没有就请用户在桌面上用管理员终端跑同一个脚本。

脚本 `D:\tmp\spike_m4.py`（只在 XM 上临时运行，不进仓库；结论和样本进 `spike-notes.md §12` 和 `tests/fixtures/`）。用 sandbox 仓库新注册一个 runner，名字带 `-svc`，标签 `runnerdeck-spike`；不碰 `D:\tmp\actions-runner`。

| # | 要回答的问题 | 做法 |
| --- | --- | --- |
| S1 | `config.cmd --unattended --runasservice` 的输出、退出码、耗时；装完是否立即启动服务；服务名（`.service` 文件内容）、显示名、启动类型、服务账号 | 在 `harden_runner_dir` 过的目录里注册；`win32service.QueryServiceConfig` / `QueryServiceStatusEx` 读回 |
| S2 | 非管理员令牌下 `--runasservice` 的报错与退出码 | `runas /trustlevel:0x20000`（基本用户令牌）再跑一次 |
| S3 | config.cmd 对目录 ACL 做了什么（加了谁、继承否）；`harden_runner_dir` 后再装服务能否正常运行；加固过的根目录下服务能否访问自己的目录 | 前后 `icacls` 对比；根目录先用 `harden` 同样的 DACL 处理一个测试根 |
| S4 | config.cmd 是否改写 / 依赖 `.env` 和 `.path`；服务进程能否读到 `.env`（`PSExecutionPolicyPreference`、`https_proxy`） | 注册前写好 `.env`，job 里打印环境变量 |
| S5 | SCM 启停：停止是否优雅（GitHub 多久变 offline、马上重启有没有 Conflict）、空闲 / 执行 job 时停止的耗时、job 是否 Canceled；`QueryServiceStatusEx` 各状态的持续时间 | `win32service.ControlService(STOP)` / `StartService`，配合 API 轮询 |
| S6 | 给用户 SID 加 `RP WP LC LO RC`（启动 / 停止 / 查询）后，基本用户令牌能否启停 | `SetServiceObjectSecurity` 后用 `runas /trustlevel:0x20000` 起子进程启停 |
| S7 | 服务模式的日志：`_diag/Runner_*.log`、`Worker_*.log` 由谁创建、当前用户能否读；Runner 日志里有没有 `Listening for Jobs` / `Running job` / `completed with result` 对应的行（决定服务模式的状态与通知来源）；RunnerService 写不写事件日志 | 跑一个成功 job、一个失败 job，抓日志片段做样本 |
| S8 | job 以 NETWORK SERVICE 运行：`whoami`、PATH、能否运行 git / PowerShell 步骤；XM 的系统代理（v2rayN）对服务无效时，不写 `https_proxy` 能否连上 GitHub | job 打印；先不写代理跑一次，再写进 `.env` 跑一次 |
| S9 | 移除：管理员 `config.cmd remove --token` 是否先停服务、删服务；输出样本；之后普通用户能否删掉整个目录（`_work` / `_diag` 里是 NETWORK SERVICE 创建的文件） | `sc query` 确认服务消失；普通令牌下 `remove_runner_dir` |
| S10 | 进程模式转服务：同一目录 `config.cmd remove` 后再 `--runasservice` 是否可行，`.env` 是否保留，新 agentId | 用一个进程模式的 sandbox runner |
| S11 | runner 版本从哪读：`bin\Runner.Listener.exe` 的文件版本（`win32api.GetFileVersionInfo`）是否等于 release 版本；`Runner.Listener.exe --version` 的输出 | 两种都读，和 `.runner` / release 对比 |

桌面才能验证的两项放到 §12 验收前单独做（各 5 分钟，用户操作）：

| # | 问题 | 做法 |
| --- | --- | --- |
| D1 | 从无控制台的 GUI 进程 `ShellExecuteExW(runas, SW_HIDE)` 起 `runnerdeck-cli.exe`：UAC 弹出、点“否”返回 1223、点“是”拿到退出码，控制台窗口不闪 | 实现完成后用打包产物直接测（代码路径就是产品代码） |
| D2 | Qt 的原生事件过滤器能否收到 `WM_ENDSESSION(wParam=0)` | 小脚本：一个窗口在 `WM_QUERYENDSESSION` 返回 FALSE 并 `ShutdownBlockReasonCreate`（制造“仍要注销 / 取消”界面），一个 Qt 进程记录收到的消息；用户注销 → 取消 |

## 2. 服务模式

### 2.1 数据

```python
class RunnerRecord:          # 新增字段
    service_name: str | None = None   # 服务模式：.service 的内容；目录丢失时用它删服务
```

`mode` 已有 `RunnerMode.SERVICE`。服务模式的记录 `auto_start` 恒为 False（启动类型由服务自己决定，界面隐藏这个开关）。

### 2.2 `core/service.py`（只有它 import `win32service`）

```python
class ServiceState(StrEnum): RUNNING, STOPPED, START_PENDING, STOP_PENDING, PAUSED, MISSING, UNKNOWN
@dataclass(frozen=True)
class ServiceInfo: name: str; state: ServiceState; pid: int | None; start_type: str | None; account: str | None

class ServiceOps(Protocol):           # 测试注入假实现；非 Windows 默认实现一律 UNKNOWN
    def query(self, name: str) -> ServiceInfo           # 不需要管理员；不存在 -> MISSING
    def start(self, name: str) -> None                  # 拒绝访问 -> ElevationRequiredError
    def stop(self, name: str) -> None                   # 同上；只发 STOP，不等
    def grant_control(self, name: str, sid: str) -> None  # 提权进程里用：DACL 追加 (A;;RPWPLCLORC;;;sid)
    def delete(self, name: str) -> None                 # 提权进程里用：目录已丢失时清孤儿服务
def current_user_sid() -> str
```

`ElevationRequiredError(RunnerDeckError)` 新增在 `errors.py`。等待停止完成由调用方按 `query` 轮询（超时 120 s，执行 job 时的取消耗时见 S5）。

### 2.3 提权：`core/elevate.py` + `core/elevated.py`

非提权一侧（RunnerDeck / CLI）：

```python
@dataclass(frozen=True)
class ElevatedResult: ok: bool; output: str; service_name: str | None; error: str | None
def run_elevated(op: str, payload: dict, *, timeout_s: float = 300) -> ElevatedResult
#   1. 请求写到 %LOCALAPPDATA%\RunnerDeck\elevate\<uuid>.json（原子写；内含注册 / 注销 token）
#   2. ShellExecuteExW(lpVerb="runas", file=<runnerdeck-cli.exe 或 python.exe -m runnerdeck.core.cli>,
#      params="elevated <path>", nShow=SW_HIDE, SEE_MASK_NOCLOSEPROCESS)；
#      用户点“否” -> ElevationCancelledError（1223，中文：“已取消：需要管理员授权”）
#   3. 等进程退出（timeout_s），读 <uuid>.result.json；finally 删请求和结果文件
```

提权一侧 `runnerdeck-cli elevated <path>`（`core/elevated.py`，CLI 帮助里不列出）：读请求后**立即删除**请求文件，只执行白名单操作，结果（输出已脱敏）写到结果文件，退出码 0 / 1。

| op | payload | 做什么 |
| --- | --- | --- |
| `install_service` | `runner_dir, url, token, name, labels, work, grant_sid` | `installer.configure(..., run_as_service=True)` → 读 `.service` → `grant_control` |
| `convert_to_service` | 上面 + `remove_token` | `installer.unconfigure` → `configure(run_as_service=True)` → `grant_control` |
| `remove_service` | `runner_dir, remove_token, service_name` | `installer.unconfigure`（S9：预期 config.cmd 自己停服务、删服务）；目录不在时 `service.delete` |
| `control_service` | `service_name, action` | 未授权的服务（导入的、或用户选了“每次 UAC”）启停 |
| `grant_control` | `service_name, sid` | 导入的服务 runner 补授权（“授权启停…”） |

校验：`runner_dir` 必须是绝对本地路径且含 `config.cmd`；`service_name` 必须匹配 `^actions\.runner\.[^\\/]+$`；未知 op 直接失败。请求文件只在提权那一刻存在，token 本来 1 小时内过期。

**授权启停（用户已确认）**：安装时把当前用户 SID 加到服务的 DACL（启动、停止、查询），之后启停走 SCM，不再弹 UAC。代价：任何以你的身份运行的程序也能启停这个服务（不能改服务配置、不能换可执行文件路径）。导入的、未授权的服务 runner 启停时各弹一次 UAC，或用“授权启停…”补一次授权。

### 2.4 `installer.py`

`build_config_command(..., run_as_service: bool = False)` 追加 `--runasservice`；超时同 configure（120 s，S1 若更慢再调）。服务模式下 `.env` 在 configure **之前**写（服务装完可能立即启动，S1/S4 确认）。

### 2.5 `manager.py`

- `plan_add(..., mode=RunnerMode.SERVICE)`：步骤多一步 `ELEVATE`（“等待管理员授权（UAC）…”）。下载 / 解压 / ACL / `.env` 不提权；拿到注册 token 后 `run_elevated("install_service")`。UAC 取消或失败 → 和进程模式一样回滚（删目录；已注册则 API DELETE）。
- `start_service(record)` / `stop_service(record)`：先 SCM；`ElevationRequiredError` 时由 controller 决定是否走 `control_service`（一次 UAC）。
- `remove_runner(SERVICE)`：remove token → `run_elevated("remove_service")` → 普通权限删目录（S9）→ 删记录。提权失败 / 取消时什么都不删。
- `convert_to_service(record_id)`：进程模式且已停止才可以；remove token + 注册 token → `run_elevated("convert_to_service")` → 记录改成 `mode=SERVICE`、新 `github_runner_id`、`service_name`。**已知风险**：注销成功、装服务失败时 runner 已从 GitHub 注销，记录保持进程模式，状态显示“已在 GitHub 端被删除”，错误信息说明“删除记录后重新添加”。
- `import_runner`：目录有 `.service` → 导入为 SERVICE 记录（读服务名）；之后可“授权启停…”。
- `statuses()` / `_local_snapshot`：SERVICE 记录的本地状态来自 `service.query`：RUNNING → IDLE（是否 busy 由远端 / 日志决定，**【待 spike】S7**），START_PENDING → STARTING，STOP_PENDING → STOPPING，STOPPED → STOPPED，MISSING → ERROR（“服务已不存在”），UNKNOWN → 原样交给合并规则。
- `start_all` / `stop_all`：包含已授权的服务 runner；需要 UAC 的跳过并汇总提示。退出 RunnerDeck、注销、`emergency_stop_all` **不动**服务 runner。
- `kill_external` / `take_over` 对服务 runner 仍拒绝；discover 已把含 `.service` 的目录标成 service（M3）。

### 2.6 状态合并（`status.merge_status`）

服务记录走同一套规则，补两点：本地 STOPPED + 远端 ONLINE 不显示 EXTERNAL，在 90 s 内显示 STOPPED（`DETAIL_OFFLINE_LAG`），之后 STALLED 改为 ERROR“服务已停止但 GitHub 仍在线”；服务记录没有 SESSION_LINGERING（强杀不由我们做）。具体以 S5 的 offline 延迟为准。

### 2.7 服务模式的输出、job 与通知【待 spike】S7

- 若 `_diag/Runner_*.log` 有 job 开始 / 结束行：controller 的 5 s 重算顺带读最新 Runner 日志的新增字节，喂给 `process` 的同一个解析器，得到 busy / 当前 job / job 结果 → 状态和通知与进程模式一致；详情页的“实时输出”对服务 runner 显示这份日志的尾部。
- 若没有：busy 只看 GitHub 的 `busy`，job 结束通知改为“busy → 空闲”时查最近一次 run 的结论（需要 Actions: Read）；“实时输出”页隐藏，只留诊断日志。

### 2.8 app / ui

- 向导第 2 步“运行模式”：进程（默认）/ 服务。选服务时说明：需要一次管理员授权；以 NETWORK SERVICE 运行，开机即运行、不需要登录、退出 RunnerDeck 不影响；job 看不到你的用户目录和用户级 PATH；不会用你的系统代理，需要代理请先在设置里填写（写进 runner 的 `.env`）。
- 卡片 / 详情页：模式徽标“服务”；进程 runner 的菜单多“转为服务…”（确认框说明：会重新注册、GitHub 上的 runner id 会变、要一次 UAC）；未授权的服务 runner 多“授权启停…”。服务 runner 不显示“随 RunnerDeck 启动”、“接管 / 结束进程”。
- 托盘：服务 runner 照常列出启停项；“退出”的确认框只列进程模式里执行中的 job。
- controller 新增：`convert_to_service(record_id)`、`grant_service_control(record_id)`；`start` / `stop` / `remove` / `add_runner` 按模式分派；UAC 取消用 `failed(op, "已取消：需要管理员授权", …)`，不当成错误弹红框（InfoBar 警告）。

### 2.9 CLI

`add --service`、`to-service <key> [-y]`；`start` / `stop` / `status` / `remove` 按记录的模式处理（服务：`start` 只发启动命令后返回，不进前台）；隐藏的 `elevated <request>`。

## 3. 代理写进 `.env`

- 受管理的键：`https_proxy`、`http_proxy`（值即 `_proxy_env` 规范化后的 URL）。`no_proxy` 没有对应设置，不管。
- 添加 runner（两种模式）、每次启动 runner 之前：把当前 `settings.proxy` 同步进 `.env`（内容相同不写）。
- 设置里代理从 old 改成 new：`manager.apply_proxy(old, new)` 对每个记录、每个键：`.env` 里没有或值等于 old → 写 new（new 为空则删键）；值是别的（用户自己写的）→ 不动，并在结果里列出。结果提示“已写入 N 个 runner；正在运行的需要重启后生效”。
- 进程模式仍把代理作为环境变量传给 config.cmd 和 run.cmd（行为不变，`.env` 同步失败时也不受影响）。

## 4. Workflow 扫描与最近运行

- `github_client`：`list_workflow_files(owner, repo)`（`GET /contents/.github/workflows`；404 → 空列表；403 → `PermissionMissingError("Contents: Read")`）、`get_text_file(owner, repo, path)`（base64，> 1 MB 跳过）、`list_runs(owner, repo, per_page=10)`（403 → `PermissionMissingError("Actions: Read")`）。新模型 `WorkflowRun(id, run_number, name, display_title, status, conclusion, event, head_branch, created_at, html_url)`。
- `core/workflows.py`（纯函数，PyYAML `safe_load`）：
  - `scan(path, text) -> list[JobTarget(workflow, job_id, labels: tuple[str, ...] | None, group: str | None, dynamic: bool)]`：`runs-on` 支持字符串、列表、`{group, labels}`；含 `${{` 的标为 dynamic；`uses:` 的复用 workflow 跳过；YAML 解析失败记一条 `parse_error`。
  - `match(target, runner_labels) -> Match`：`OK` / `MISSING(labels)` / `NOT_SELF_HOSTED`（不含 `self-hosted` 的托管 job，不展示）/ `UNKNOWN`（dynamic 或 group）。标签比较不分大小写；runner 标签 = 远端 `labels`（有的话）否则 记录标签 + `self-hosted`、`Windows`、`X64`。
- controller：`load_repo_insights(record_id)`（worker 线程，每仓库缓存 10 分钟，刷新按钮强制重取）→ 信号 `repo_insights_loaded(record_id, RepoInsights)`。每个仓库最多读 30 个 workflow 文件。
- 详情页新增“Workflow”页签：会用到这台 runner 的 job（✓ 匹配 / ✗ 缺少标签 `x`：这个 job 会一直排队 / ? 无法判断）+ 最近 10 次运行（标题、状态、分支、多久前，点击在浏览器打开）。权限不足时显示需要哪个权限和 PAT 设置链接。登录框的权限说明补 Contents: Read、Actions: Read（可选）。

## 5. 取消注销后恢复（M3 遗留）

- `app/session_end.py`：`SessionEndFilter(QAbstractNativeEventFilter)`，`windows_generic_MSG` 里 `message == WM_ENDSESSION and wParam == 0` → 回调 `on_cancelled()`。非 Windows 不安装。
- controller：`emergency_stop_all()` 记下这次停掉的 id；新增 `resume_after_cancelled_logoff()`：对这些 id，等它们真正停下（状态 STOPPED，最多 120 s）后逐个 `start`，然后清空；返回数量。
- `__main__._Lifecycle`：收到取消 → `session_ending = False`（之后的 `aboutToQuit` 恢复正常的阻塞关闭）→ `resume_after_cancelled_logoff()` → 托盘提示“注销已取消，已重新启动 N 个 runner”。
- D2 若发现收不到 `WM_ENDSESSION(0)`：退而在 `commitDataRequest` 后 60 s 进程仍存活即视为取消（在 §13 记录）。

## 6. `runners_root` 的 ACL（M3 遗留）

- `installer.harden_root_dir(path)`：与 `harden_runner_dir` 相同的受保护 DACL（Administrators、SYSTEM、当前用户完全控制，继承到子项）。已加固的 runner 目录本身是受保护 DACL，不受影响；服务 runner 的目录由 config.cmd 另加服务账号（S3 验证根目录加固后服务仍可访问）。
- 添加 runner 时，根目录不存在、由 RunnerDeck 创建 → 自动加固。
- 设置页“runner 根目录”下加“加固权限”按钮（确认框说明效果）；根目录是盘符根、用户主目录或系统目录时拒绝。

## 7. runner 版本（M3 遗留）【待 spike】S11

`runner_file.read_runner_version(runner_dir) -> str | None`（按 S11 的结论读文件版本，按 mtime 缓存）；`RunnerStatusRow` / 详情页头部显示“版本 2.338.1”。读不到显示“—”。顺带改正 CHANGELOG 里提前写了的“runner version”。

## 8. 检查新版本（可选）

- `github_client.latest_app_release()`：`GET /repos/nablejohne/RunnerDeck/releases/latest`（不带 PAT 的独立 client，与下载 runner zip 同样做法）。`core/update_check.py`：`x.y.z` 比较，忽略预发布。
- `AppSettings.check_updates: bool = True`：启动 30 s 后静默查一次；设置页“关于”显示当前版本、检查结果（“有新版本 x.y.z → 下载页”）和“检查更新”按钮。只提示，不下载、不替换。

## 9. 依赖与打包

- `pyproject.toml`：`pywin32>=308; sys_platform == 'win32'`、`pyyaml>=6`；`uv lock`。CLAUDE.md 技术栈改为现状（去掉“M4 起”）。
- `--smoke-test` 额外：`service.query("EventLog")`、`win32api` 读自身文件版本、`yaml.safe_load`（确认 pywin32 的 DLL 和 PyYAML 打进了包）。
- **单实例互斥量**：GUI 启动时创建命名互斥量 `RunnerDeckAppMutex`（ctypes，进程生命周期内持有），供安装 / 卸载程序检测“RunnerDeck 正在运行”。
- `packaging/RunnerDeck.iss`（Inno Setup 6.7.1，windows-2025 镜像自带）：
  - `PrivilegesRequired=lowest`，`DefaultDirName={userpf}\RunnerDeck`（= `%LOCALAPPDATA%\Programs\RunnerDeck`），固定 `AppId`，`AppMutex=RunnerDeckAppMutex`，版本由 `/DAppVersion=` 传入，`OutputBaseFilename=RunnerDeck-{ver}-setup`。
  - 开始菜单快捷方式；可选桌面快捷方式；安装后可选“启动 RunnerDeck”。
  - 卸载：删掉 HKCU Run 的 `RunnerDeck` 值（`ValueType: none; Flags: uninsdeletevalue`）；**不删**用户数据（`%APPDATA%\RunnerDeck`、runner 目录、凭据管理器里的 PAT），也不动服务 runner（它们不依赖 RunnerDeck）；卸载完成页说明这一点。
  - 界面语言：镜像里有 `ChineseSimplified.isl` 就用，否则英文（实现时确认，§13 记录）。
- `scripts/build.ps1`：zip 之后找到 `ISCC.exe`（PATH 或 `${env:ProgramFiles(x86)}\Inno Setup 6`）就编译安装包；`-NoInstaller` 跳过。最后两行输出 zip 和 setup.exe 路径。
- CI `build` job：同时上传 setup.exe；新增安装冒烟：`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER` 安装 → 已安装的 `runnerdeck-cli.exe --version`、`RunnerDeck.exe --smoke-test` → `unins000.exe /VERYSILENT` → 轮询到安装目录、开始菜单快捷方式、HKCU 卸载键都消失（卸载程序会复制自己后立即退出，不能只等进程）。
- `release.yml`：打包后
  1. `SHA256SUMS.txt`（`sha256sum` 格式，覆盖 zip 和 setup.exe）；
  2. `actions/attest-build-provenance@v4`（实现时确认当前推荐用法），`subject-path` 为 zip 和 setup.exe；
  3. `gh release create` 上传 zip、setup.exe、`SHA256SUMS.txt`；
  4. job 权限 `contents: write, id-token: write, attestations: write`。v1.0.0 不是预发布（现有规则）。
- README：安装（安装包 / zip）、SmartScreen“未知发布者”说明、`Get-FileHash` 与 `gh attestation verify <文件> -R nablejohne/RunnerDeck` 校验、服务模式说明、卸载会保留什么、PAT 权限（Administration RW 必需；Contents R、Actions R 可选）。

## 10. 架构与守卫

- `win32service` / `win32security` 只在 `core/service.py`（`installer.harden_*` 维持 icacls）；`ShellExecuteExW` 只在 `core/elevate.py`；`core/elevated.py` 只能经 `installer` 调 config.cmd（不新增直接调用 cmd 的地方）。`tests/core/test_architecture.py` 加这三条。
- ui 仍只经 controller；ui 禁止 import `service` / `elevate` / `elevated` / `workflows`（加进 ui 的 banned 列表）；`models` 新增的数据类型 ui 可直接用。
- core 新功能附单测（SCM、ShellExecute、注册表都注入假实现）；core 覆盖率门槛 80% 不变。

## 11. 实现顺序（顺序实现，每步可跑闭环、本地全绿后提交并推 main）

0. spike S1–S11（`ssh XM`）→ `spike-notes.md §12`、fixtures、本文 §13 修订 → 向用户报告结论，有颠覆性发现再确认。
1. 依赖与小项：pywin32 / PyYAML、互斥量、runner 版本（§7）、根目录加固（§6）、代理 `.env`（§3）。
2. 服务 core：`service` / `elevate` / `elevated`、installer、manager、CLI（§2.2–2.5、2.9）；XM 上用提权的 SSH 会话跑 CLI 全流程（`elevated` 子命令直接调用；UAC 弹窗本身留给 D1）。
3. 服务 app / ui（§2.6–2.8）。
4. Workflow 扫描、最近运行、检查更新（§4、§8）。
5. 取消注销后恢复（§5；先做 D2）。
6. 打包与发布（§9）：Inno 脚本、build.ps1、CI 安装冒烟、release.yml；README / CHANGELOG / CLAUDE.md。
7. 1 个独立 reviewer 审 M4 全部改动 → 逐条核对、修复。
8. CI 绿 → XM 真机 pytest → 桌面验收（§12）→ 版本改 1.0.0、CHANGELOG 合并 → 推 `v1.0.0` tag → 核对 Release → PROGRESS.md + 回流评估。

## 12. 验收

- 本地：`uv run ruff check . && uv run ruff format --check . && uv run pytest`；`--smoke-test` 退出 0。
- CI（windows-latest）：lint、test（core ≥ 80%）、build（三个 exe 冒烟 + 安装包静默装 / 卸）。
- XM（SSH）：真机 pytest；spike 结论落档。
- XM 桌面（用户；job 由我在开发机 `gh workflow run` 触发）：
  1. 向导添加服务 runner：一次 UAC → 服务运行、GitHub 在线 → 跑一个 job 成功（PowerShell 步骤正常；需要代理时经 `.env` 生效）。
  2. 卡片 / 托盘启停服务 runner 不弹 UAC（若选了授权）；执行 job 时停止先确认；状态和通知正确。
  3. 退出 RunnerDeck：服务 runner 仍在线（API 确认）。可选：重启电脑、不登录，API 显示在线。
  4. 进程 runner“转为服务”：一次 UAC，新 id，跑一个 job。
  5. 添加时在 UAC 点“否”：提示已取消，目录和注册都没有留下。
  6. 移除服务 runner：一次 UAC → 服务消失（`sc query`）、GitHub 无此 runner、目录已删。
  7. 详情页：runner 版本、Workflow 页签（故意让一个 job 要求不存在的标签，显示“缺少标签”）、最近运行。
  8. 改代理：`.env` 更新，提示重启生效。
  9. 注销 → 在“仍要注销 / 取消”界面点取消 → 托盘提示并重新启动之前在跑的 runner（D2 的阻塞脚本制造这个界面）。
  10. 安装包：XM 上按用户安装（不弹 UAC）→ 开始菜单启动 → 运行中卸载提示先退出 → 退出后卸载干净（数据保留）；CI 的静默装卸作为干净机器上的证据。
  11. 设置页“检查更新”显示“已是最新版本”（发布后用 1.0.0 安装包再看一次）。
- 推 `v1.0.0`：Release（非预发布）有 zip、setup.exe、`SHA256SUMS.txt`；`gh attestation verify` 对两个文件都通过；`sha256sum -c` 通过。

## 13. 实现后的修订（以代码为准）

（spike 与实现后填写）
