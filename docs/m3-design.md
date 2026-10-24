# M3 设计：日常可用（契约）

M3 各部分按本文的接口实现，测试按本文的约定写。core 接口见 `docs/m1-design.md`（§13 修订），app/ui/打包见 `docs/m2-design.md`（§10 修订），runner 行为依据 `docs/spike-notes.md`（§5 停止、§10 第 2 轮）。PLAN.md 的 M3 范围：托盘、关闭到托盘、开机自启（HKCU Run）、启动时自动拉起 runner；Poller 与状态合并规则、详情页日志查看；导入已有目录、发现残留进程；job 完成 / 失败通知；发布 `v0.2.0`。另外收掉 M1 推迟的缺口：强杀信息只在内存里（m1-design §13 status.py）。

已确定的决策：实现方式沿用 M1/M2 的工作流（本文契约 → 4 条车道并行实现 → 对抗评审）；job 通知默认“仅失败”；验收时导入 `D:\tmp\actions-runner`（用户自己的 runner）并由 RunnerDeck 启动跑一次 job，验完“取消登记”（只删记录，目录和注册原样保留）。

## 0. 相对 PLAN.md 的调整

| 调整 | 理由 |
| --- | --- |
| 托盘放 `ui/tray.py`（PLAN 写在 `app/`） | 菜单用 qfluentwidgets 的 `SystemTrayMenu`，app 层禁止 import qfluentwidgets |
| 开机自启放 `core/autostart.py`（PLAN 写在 `app/`） | 纯 winreg、无 Qt，注册表可注入，单测计入 core 覆盖率门槛 |
| 新增 `core/stop_marks.py` | M1 缺口：停止记录（时间 + 是否强杀）要跨进程保存，GUI 和 CLI 共用 |
| 新增 `core/diag.py` | 列出 / 读取 runner 的 `_diag` 日志（尾部截断、脱敏） |
| 新增 `app/single_instance.py` | 第二个实例激活已有窗口（m2-design §6 约定 M3 做） |
| 详情页取代 `LogDialog` | 实时输出和 `_diag` 日志共用一个日志控件；卡片“查看输出”改为“详情” |
| 托盘菜单每个 runner 一个“启动 / 停止”项，不用子菜单 | RoundMenu 的子菜单用 `show()` 打开，不触发 `aboutToShow`，内容无法按状态刷新 |
| 新增“取消登记”（`forget`） | 导入的是用户自己的目录：要能只删记录、不注销不删目录（用户确认的验收流程） |
| Poller 全部仓库失败时固定降频到 5 分钟，不做指数退避 | 够用；本地进程扫描不降频 |
| 状态合并规则补两条（§2） | 优雅停止后 GitHub 11–70 s 才显示离线（spike §10）；轮询结果可能早于本地状态变化 |
| `runners_root` 本身的 ACL 加固仍推迟到 M4 | 根目录可能是用户自选的任意目录，改 ACL 风险大于收益 |

## 1. 地基（本会话先写，车道不改接口；有问题提出来）

### 1.1 `core/models.py`

```python
class AppSettings:  # 新增
    notify_jobs: Literal["all", "failed", "off"] = "failed"

# 运行时数据（不持久化），frozen dataclass，ui 可以直接 import models 拿类型
@dataclass(frozen=True)
class DiagLog:
    path: Path; kind: Literal["runner", "worker", "other"]; modified: datetime; size: int
    name: str  (property) = path.name
@dataclass(frozen=True)
class AutostartState:
    supported: bool            # 非 Windows（且未注入注册表）为 False
    registered: bool           # Run 键下有 RunnerDeck 值
    command: str | None        # 现有值
    expected: str | None       # 本次运行应写入的命令（autostart.launch_command()）
    disabled_by_user: bool     # StartupApproved\Run 标记为禁用（任务管理器“启动”页）
    matches: bool  (property) = registered and command 与 expected 相同（忽略大小写）
@dataclass(frozen=True)
class StrayRunner:
    path: Path; pids: tuple[int, ...]; configured: bool; service: bool
@dataclass(frozen=True)
class Discovery:
    root: Path; unregistered: tuple[Path, ...] = (); strays: tuple[StrayRunner, ...] = ()
    empty: bool  (property)
```

### 1.2 `core/status.py`（规则见 §2）

```python
OFFLINE_LAG_S = 90
DETAIL_OFFLINE_LAG = "已优雅停止，GitHub 端还显示在线（通常 1 分钟内更新）"
class Bucket(StrEnum): RUNNING, IDLE, TROUBLE, STOPPED
def bucket(state: DisplayState) -> Bucket
    # IDLE -> IDLE；BUSY/STARTING/CONNECTING/RECONNECTING/STOPPING -> RUNNING；
    # STALLED/ERROR/MISSING/EXTERNAL -> TROUBLE；STOPPED/UNKNOWN/SESSION_LINGERING -> STOPPED
class TrayLevel(StrEnum): STOPPED, ONLINE, BUSY, ERROR      # 灰 / 绿 / 蓝 / 红
def tray_level(states: Iterable[DisplayState]) -> TrayLevel
    # 任一 TROUBLE -> ERROR；否则任一 BUSY -> BUSY；否则任一 RUNNING/IDLE -> ONLINE；否则 STOPPED
JOB_FAILURE_RESULTS = frozenset({"failed", "abandoned"})
def is_job_failure(result: str) -> bool        # 不区分大小写；Canceled / Succeeded* / Skipped 不算
```

概览计数（`ui/pages/dashboard_page.summarize`）改用 `bucket()`，和托盘颜色同一套分组。

### 1.3 测试与守卫

- `tests/ui/conftest.py` 的 `FakeController` 补齐 §4 的全部新信号 / 方法（含 M2 漏掉的 `refreshing_changed`）；`set_state` 之类的辅助同时 emit `display_changed`。
- `tests/core/test_architecture.py` 的 `BANNED_IN_UI` 和 `ui/ruff.toml` 增加 `runnerdeck.core.autostart`、`runnerdeck.core.stop_marks`、`runnerdeck.core.diag`、`winreg`、`psutil`。
- 整合时新增 `tests/ui/test_fake_controller.py`：`FakeController` 的每个公开信号和方法在 `RunnerController` 上都存在（防止假对象和真对象走样）。

## 2. 状态合并规则（`status.merge_status`，按顺序）

1. remote MISSING -> MISSING。
2. 本地 STOPPING -> STOPPING；本地 ERROR 且目录下**没有**外部进程 -> ERROR（detail = last_error）；有外部进程时落到第 4 条显示 EXTERNAL（评审修订：否则“异常”成死胡同，接管 / 结束永远不出现）。
3. 本地运行中：
   - 本地 RECONNECTING -> RECONNECTING。
   - **新**：`remote.fetched_at` 早于 `local.state_since`（远端快照早于这次本地状态变化）-> 按本地状态原样显示（STARTING / IDLE / BUSY），**detail 为空**（远端已知，只是旧）。
   - remote ONLINE 且 busy，或本地 BUSY -> BUSY；ONLINE -> IDLE。
   - remote OFFLINE：`state_since` 起不到 60 s -> CONNECTING，否则 STALLED。
   - remote UNKNOWN / None -> 本地状态原样，detail = `DETAIL_REMOTE_UNKNOWN`。
4. 本地未运行：
   - external -> EXTERNAL。
   - remote ONLINE：强杀且 `stopped_at` 起不到 240 s -> SESSION_LINGERING；**新**：优雅停止且不到 `OFFLINE_LAG_S`（90 s）-> STOPPED（detail = `DETAIL_OFFLINE_LAG`）；否则 EXTERNAL。两个窗口都按**快照的 `fetched_at`** 距 `stopped_at` 计算（评审修订：轮询间隔可达 300 s，停止后 3 s 拿到的 online 快照会一直是最新的，按 now 算 90 s 后就会误报 EXTERNAL）；会话残留的剩余秒数仍按 now 倒数，最少显示 1 秒。
   - remote OFFLINE -> STOPPED；UNKNOWN / None -> STOPPED（detail 标注远端未知）。

`last_stop_forced` / `stopped_at` 的来源：有本进程的 RunnerProcess 时用它的；没有（应用重启后、CLI 从另一个终端 `stop`）时用 stop_marks（§3.1）；两者都有取较新的。

## 3. 车道 A：core

### 3.1 `core/stop_marks.py`

```python
@dataclass(frozen=True)
class StopMark: stopped_at: datetime; forced: bool
class StopMarks:
    def __init__(self, directory: Path | None = None)      # 默认 paths.local_dir() / "stops"
    def record(self, runner_dir: Path, *, forced: bool, at: datetime | None = None) -> None
    def get(self, runner_dir: Path, *, now: datetime) -> StopMark | None
    def clear(self, runner_dir: Path) -> None
```

- 每个 runner 一个文件：`sha256(manager._path_key 同样的规范化路径)[:16].json`，内容 `{"path", "stopped_at"(ISO), "forced"}`。GUI 和 CLI 各写各的 runner，不共享一个 JSON（避免两个进程读改写互相覆盖）。
- 原子写（临时文件 + `os.replace`，`PermissionError` 重试同 store）。
- `get`：文件缺失 / 损坏 / `path` 不符 -> None；早于 `SESSION_LINGER_S` -> None 并尽力删除。
- 停止记录只是提示信息：所有方法遇到 `OSError` 只记警告，**绝不抛异常**。

### 3.2 `core/diag.py`

```python
DIAG_DIR = "_diag"; MAX_TAIL_BYTES = 1_048_576
def list_diag_logs(runner_dir: Path) -> list[DiagLog]     # _diag/*.log，按修改时间新到旧；目录不存在 -> []
    # kind：Runner_* -> "runner"，Worker_* -> "worker"，其他 -> "other"
def read_tail(path: Path, *, max_bytes: int = MAX_TAIL_BYTES) -> str
    # 只读末尾 max_bytes，截断时丢掉第一行残片并在开头加一行“…（只显示最后 N MB）”；
    # UTF-8（errors="replace"），整段过 redact()；OSError -> RunnerDeckError("无法读取 …：…")
```

runner 运行中日志文件是否可读要实测（§9 spike）；读失败给出文案，不崩。

### 3.3 `core/autostart.py`

```python
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "RunnerDeck"; AUTOSTART_FLAG = "--autostart"
class AutostartError(RunnerDeckError)
class Registry(Protocol):          # 只操作 HKCU；测试注入内存实现，绝不碰开发机真实注册表
    def get(self, key: str, name: str) -> str | bytes | None
    def set_string(self, key: str, name: str, value: str) -> None
    def delete(self, key: str, name: str) -> None              # 不存在时静默
class WinRegistry(Registry)        # winreg；非 Windows 构造时抛 AutostartError("开机自启只支持 Windows")
def launch_command(*, frozen: bool | None = None, executable: str | None = None) -> str
    # frozen：'"<dir>\RunnerDeck.exe" --autostart'；开发：'"<dir>\pythonw.exe" -m runnerdeck --autostart'
    # （python.exe 换同目录 pythonw.exe）；路径一律加引号
def state(registry: Registry | None = None) -> AutostartState
    # StartupApproved 只读：值的第一个字节为奇数（如 0x03）= 被用户禁用
def enable(registry=None) -> AutostartState; def disable(registry=None) -> AutostartState
def reconcile(wanted: bool, registry=None) -> AutostartState
    # wanted 且（未注册或命令不符，例如用户把解压目录挪了）-> 重写；不 wanted 且已注册 -> 删除
    # registry=None 时用 WinRegistry()；不支持的平台 reconcile 直接返回 supported=False，不抛
# OSError -> AutostartError（中文，带键名）
```

### 3.4 `process.py`

```python
RunnerProcess.signal_stop(self) -> bool
    # 不等待、不强杀：HELPER 往 stdin 写 stop，CONSOLE 发 Ctrl+Break；标记为“我们主动停的”，
    # 之后的退出记 STOPPED 而不是 ERROR。没在运行 -> False。供 Windows 注销 / 关机时调用。
```

### 3.5 `manager.py`

```python
RunnerManager.__init__(..., stop_marks: StopMarks | None = None, scan_processes: ... = <一次扫描全部 runner 进程>)
add_runner(plan, *, auto_start: bool = False, progress=None) -> RunnerRecord
import_runner(path, *, auto_start: bool = False) -> RunnerRecord   # 同路径已登记 -> 原样返回
set_auto_start(record_id: str, enabled: bool) -> RunnerRecord
forget_runner(record_id: str) -> RunnerRecord     # 只删记录：不访问 GitHub、不动目录、不动 .runner
statuses(processes=None, *, remote: bool = True) -> list[RunnerStatusRow]
    # 每次调用只做一次进程扫描（psutil.process_iter 一遍，按目录分组），不再每个记录扫一遍；
    # remote=False：不建 client、不访问 GitHub，remote = UNKNOWN 且 remote_error = None
discover() -> Discovery
    # unregistered = scan_unregistered()；strays = runner 进程的 exe 在 runners_root 下、
    # 但所在 runner 目录（root 的直接子目录）没有登记；configured = is_configured；service 见下
is_service(runner_dir: Path) -> bool              # runner 目录里有 .service 文件（只 stat）
kill_external(record_id: str) -> ExternalStop
    # 服务模式 -> InvalidInputError("这个 runner 以 Windows 服务方式运行，请在“服务”里停止（服务模式 M4 支持）")
    # process.stop_external；结束了进程就 record_stop(forced=True)
kill_stray(path: str | os.PathLike[str]) -> ExternalStop
    # path 必须是 runners_root 的直接子目录、没有登记；服务同上拒绝
record_stop(runner_dir: Path, *, forced: bool, at: datetime | None = None) -> None   # 写 stop_marks，不抛
remove_runner(...)：成功后 clear 该目录的停止记录
_local_snapshot：没有 proc -> 用 stop_marks；proc 未运行 -> proc 与 stop_marks 取较新
```

`find_processes` 注入点保留（兼容现有测试），新的一次性扫描注入点由实现者命名并写 docstring；非 Windows 测试绝不扫描真实进程。

### 3.6 `cli.py`

- `stop`：结束了进程 -> `manager.record_stop(dir, forced=True)`；`start` 的 Ctrl+C 停止后对每个 runner `record_stop(forced=not result.graceful)`。
- 新命令 `forget <key>`（`-y`）：取消登记；`status` 自动用上 stop_marks（经 manager）。

### 3.7 测试

`tests/core/test_stop_marks.py`、`test_diag.py`、`test_autostart.py`（内存注册表）、`test_manager.py` / `test_process.py` / `test_cli.py` 补对应用例；`test_status.py` 的规则用例由地基负责。core 覆盖率门槛 80% 不变。

## 4. 车道 B：app

### 4.1 `app/controller.py` 新增

**信号**

| 信号 | 参数 |
| --- | --- |
| `display_changed` | `(record_id: str, DisplayStatus)`，合并后的显示状态变化时（本地状态、轮询结果、定时重算、配置变化） |
| `discovery_updated` | `(Discovery)` |
| `runner_imported` | `(RunnerRecord)`，随后 `config_changed` |
| `runner_forgotten` | `(record_id: str)`，随后 `config_changed` |
| `external_stopped` | `(key: str, ExternalStop)`；key 是记录 id，或未登记目录的路径字符串 |
| `diag_logs_loaded` | `(record_id: str, list[DiagLog])` |
| `diag_text_loaded` | `(record_id: str, path: str, text: str)` |
| `autostart_state_loaded` | `(AutostartState)` |

**方法**（同步方法在主线程、不阻塞；其余进工作线程，结果走信号）

```python
display(record_id) -> DisplayStatus; all_displays() -> dict[str, DisplayStatus]
remote_busy(record_id) -> bool            # 最近一次远端快照说 busy（接管 / 结束前的僵尸 job 提醒）
refresh_statuses(*, remote: bool = True, quiet: bool = False)   # quiet：失败只记日志，不 emit failed
start_auto_runners()      # 工作线程 statuses(remote=False) -> 主线程启动 auto_start 且未运行、无外部进程的记录；
                          # 跳过的 -> failed("autostart", "…目录下已有其他进程在运行，未自动启动：<名字>")
set_auto_start(record_id, enabled)        # -> config_changed；failed("auto_start")
add_runner(plan, *, start_after=False, auto_start=False)
import_runner(path: str, *, auto_start=False)   # failed("import")
forget(record_id)          # 本进程在运行或 busy -> 同步拒绝；failed("forget")
discover()                 # failed("discover")
kill_external(record_id)   # -> external_stopped；之后 poller.kick；failed("kill")
take_over(record_id)       # kill_external 成功后在主线程 start(record_id)；failed("take_over")
kill_stray(path: str)      # failed("kill")
load_diag_logs(record_id); load_diag_text(record_id, path)   # path 必须在该记录的 _diag 下；failed("diag")
autostart_supported: bool  (property)
load_autostart_state(); set_launch_on_login(enabled)    # 改注册表 + 保存 settings.launch_on_login；failed("launch_on_login")
reconcile_autostart()      # 启动时调用：autostart.reconcile(settings.launch_on_login)
emergency_stop_all() -> int    # 主线程、不阻塞：对每个运行中的 runner signal_stop()，返回个数（注销 / 关机）
```

- **实时合并**：controller 缓存每个记录最近一次的 `row.local`（external 等需要扫描 / 读盘的字段）和 `row.remote`；任何时候用“缓存的 local + 本进程 RunnerProcess 的实时字段（state、running、state_since、current_job、last_error、较新的停止信息）+ 缓存的 remote”在主线程调 `merge_status`（纯函数）。`statuses_updated` emit 前用实时合并结果替换 `row.display`（`dataclasses.replace`），避免轮询发起后才发生的状态变化被旧结果覆盖。`kill_external` / `take_over` 成功后清掉该记录缓存的 external。
- **定时重算**：有记录时一个 5 s 的 `QTimer` 重算全部显示（CONNECTING -> STALLED、会话残留倒计时、90 s 宽限都只随时间变化），只对变化的记录 emit `display_changed`。
- **远端缓存**：`remote=False` 的刷新不覆盖远端缓存；某仓库请求失败 -> 该仓库的远端缓存置 UNKNOWN。
- **停止记录**：`stop()` / `shutdown()` 在工作线程里 `proc.stop()` 之后 `manager.record_stop(dir, forced=not result.graceful)`。
- `save_settings` 发现 `launch_on_login` 变化时同样在工作线程里改注册表。

### 4.2 `app/poller.py`

```python
class StatusPoller(QObject):
    SLOW_INTERVAL_S = 300; KICK_DELAY_MS = 3000
    def __init__(self, controller: RunnerController, *, clock=time.monotonic, parent=None)
    def start(self) -> None     # 立即轮询一次，之后每 settings.poll_interval_s 秒
    def stop(self) -> None
    def kick(self) -> None      # 去抖：KICK_DELAY_MS 后做一次远端轮询
    interval_s: int; slow: bool  (properties)
```

- 每次都 `refresh_statuses(quiet=True)`；未登录 -> `remote=False`；`slow` 时只有距上次远端轮询超过 `SLOW_INTERVAL_S` 才 `remote=True`，其余 `remote=False`（本地进程扫描不降频）。
- 一次远端轮询里所有行都有 `remote_error` -> `slow = True`；任何一个仓库成功 -> `False`；`logged_in` / `config_changed` 时复位。没有记录时不轮询。`controller.refreshing` 为真时跳过本次。
- 自己订阅：`runner_state` 进入 IDLE / STOPPED / ERROR、`external_stopped`、`runner_added`、`runner_imported` -> `kick()`；`config_changed` -> 更新间隔。

### 4.3 测试

`tests/app/test_controller.py` 补：实时合并（轮询发起后状态变化不被覆盖、优雅停止后 90 s 内显示已停止）、定时重算、`start_auto_runners` 跳过外部进程、take_over 顺序、quiet、停止记录写入、diag 路径校验、`emergency_stop_all`。`tests/app/test_poller.py`：间隔、kick 去抖、降频与复位（假时钟 + `qtbot.wait` 或直接调用超时槽）。`tests/app/conftest.py` 的 FakeManager / FakeProcess 补对应方法。

## 5. 车道 C：ui（qfluentwidgets，中文文案）

| 文件 | 内容 |
| --- | --- |
| `ui/tray.py` | `TrayIcon(QSystemTrayIcon)`：`TrayIcon(controller, window, parent=None)`；图标 = 应用图标 + 右下角状态色点（`status.tray_level`，灰 `#8a8a8a` / 绿 `#2ea043` / 蓝 `#1f6feb` / 红 `#d1242f`，QPainter 动态画）；tooltip `RunnerDeck` + 计数（≤ 127 字符截断）。左键单击 / 双击 -> `window.show_and_raise()`。菜单 `SystemTrayMenu`，在 `aboutToShow` 里整体重建：打开主界面 / 全部启动 / 全部停止 / 每个 runner 一项（`启动 名字` 或 `停止 名字（状态）`，busy 时禁用）/ 退出（`window.quit_app()`）。通知 `notify(title, message, icon)`（标题 ≤ 63、正文 ≤ 255 截断）：`runner_job_finished` 按 `settings.notify_jobs`（all 全发；failed 只发 `is_job_failure`；off 不发）；runner 进入 ERROR（`notify_jobs != "off"`）；窗口隐藏时 `discovery_updated` 出现新的未登记目录 / 残留进程。 |
| `ui/main_window.py` | `MainWindow(controller, parent=None, *, prompt_login=True, tray: bool | None = None)`：`tray=None` 时按 `QSystemTrayIcon.isSystemTrayAvailable()`；`self.tray: TrayIcon | None`。新方法 `show_and_raise()`（`showNormal` + `raise_` + `activateWindow`）、`quit_app()`、`open_detail(record_id)`、`on_session_end()`（`controller.emergency_stop_all()`）。**任何模态框之前先 `show_and_raise()`**（MessageBox 是盖在父窗口上的遮罩，窗口隐藏时会看不见又阻塞）。`closeEvent`：`QApplication.instance().isSavingSession()` 或正在退出 -> accept；`close_to_tray` 且有托盘 -> `hide()` + `ignore()`，本次运行第一次时托盘提示“RunnerDeck 仍在后台运行”；否则走 `quit_app()`。`quit_app()`：有运行中的 runner -> 确认（数量 + 正在执行 job 的名字）-> `StateToolTip` + `controller.shutdown()` -> `tray.hide()` -> `QApplication.quit()`。`failed` / 强杀提示：窗口可见用 InfoBar，隐藏或最小化时改用托盘通知。`FAILED_TITLES` 补 §4 新 op（导入失败 / 取消登记失败 / 扫描失败 / 结束进程失败 / 接管失败 / 读取日志失败 / 自动启动 / 自启设置失败 / 保存失败）。`config_changed` 时主题变了就 `ui.theme.apply_theme`。详情页 `self.detailPage` 用 `self.stackedWidget.addWidget` 加入（不加导航项），`open_detail` 里 `show_record` 后 `switchTo`。 |
| `ui/theme.py` | `THEMES = {"auto": Theme.AUTO, "light": Theme.LIGHT, "dark": Theme.DARK}`；`apply_theme(name: str) -> None` |
| `ui/pages/runner_detail_page.py` | `RunnerDetailPage(QWidget)`，`objectName="detail"`。`show_record(record_id: str | None)`（None / 记录已删 -> 空状态文案，不崩）。头部：返回按钮（回概览）、名字、`StatusBadge`、仓库、目录、标签、GitHub id、runner 版本（如有）；操作：启动 / 停止（同卡片规则，含 BUSY 确认、EXTERNAL 的接管）、`SwitchButton("随 RunnerDeck 启动")`、打开目录、在 GitHub 打开、更多菜单（移除 / 取消登记）。`SegmentedWidget`：**实时输出**（`LogView`：初始 `controller.output(id)`，订阅 `runner_line` 只追加本 id，自动滚动、复制全部）| **诊断日志**（`ComboBox` 列出 `diag_logs_loaded`，选中即 `load_diag_text`；刷新、打开 `_diag` 目录按钮；读失败显示文案）。 |
| `ui/components/log_view.py` | `LogView(QWidget)`：只读等宽 `PlainTextEdit` + 自动滚动 + 复制全部；`set_lines(list[str])`、`append_line(str)`、`set_text(str)`。删除 `log_dialog.py` 及其测试，改测 `LogView`。 |
| `ui/components/runner_card.py` | 徽章和 detail **只**来自 `update_display`（`display_changed`）；`update_state` 只决定按钮。显示 EXTERNAL 时主按钮变“接管”，菜单多“结束进程”（确认框：会强制结束，GitHub 会话约 3 分钟后过期；`remote_busy` 时加“正在执行 job，强杀会留下僵尸 job”）。停止前若本地 BUSY -> 确认“正在执行 job <名字>，停止会取消它”。点击卡片 -> `detail_requested`。菜单：详情、打开目录、在 GitHub 打开、随 RunnerDeck 启动（可勾选）、移除、取消登记（确认框说明目录和 GitHub 注册都保留）。新信号：`detail_requested(str)`、`take_over_requested(str)`、`kill_requested(str)`、`auto_start_toggled(str, bool)`、`forget_requested(str)`（`logs_requested` 删除）。 |
| `ui/pages/dashboard_page.py` | 计数改用 `status.bucket`；订阅 `display_changed`；头部加 `PushButton(FIF.FOLDER_ADD, "导入")` -> `QFileDialog.getExistingDirectory` -> `import_runner(path)`；顶部发现横幅（`discovery_updated` 非空时显示）：“在 runner 根目录发现 N 个未登记的 runner 目录” + 逐个“导入”、“发现 M 个不属于 RunnerDeck 的 runner 进程” + “结束”（确认后 `kill_stray`）；“全部停止”若有 BUSY 先确认。 |
| `ui/pages/add_wizard_page.py` | 第 2 步加 `CheckBox("随 RunnerDeck 自动启动")` 默认勾选，不作废 plan；`add_runner(plan, start_after=…, auto_start=…)`。 |
| `ui/pages/settings_page.py` | 新组“启动与托盘”：开机自启（`SwitchSettingCard`；内容来自 `autostart_state_loaded`：已开启 / “已在任务管理器中禁用，请在任务管理器的‘启动’页启用” / 不支持时禁用并写“只支持 Windows”；切换 -> `set_launch_on_login`）、启动时最小化到托盘、关闭窗口时最小化到托盘（`save_settings`）、job 通知（ComboBox：全部 / 仅失败 / 关闭）。新组“个性化”：主题（跟随系统 / 浅色 / 深色）。runner 组加轮询间隔（ComboBox：15 / 30 / 60 / 120 / 300 秒）。ComboBox 卡片按 `LineEditSettingCard` 的方式自定义（不依赖 qconfig）。 |

**测试 `tests/ui/`**：托盘菜单重建与动作、颜色等级、通知过滤（all / failed / off）、截断；关窗到托盘 / 退出流程（`tray=True` 注入，离屏下 `isSystemTrayAvailable` 恒为 False）、隐藏时 failed 走托盘；详情页空状态、实时输出只追加本 id、诊断日志加载；卡片 EXTERNAL 按钮、BUSY 停止确认、徽章只随 `display_changed` 变；导入、发现横幅；设置页新卡片调用；向导 auto_start。现有 `test_runner_state_updates_badge_and_button` 按新规则改写。

## 6. 车道 D：入口、打包与文档

### 6.1 `app/single_instance.py`

```python
SHOW_MESSAGE = "show"
def server_name(base: Path | None = None) -> str
    # "RunnerDeck-" + sha256(normcase(paths.local_dir()) + 用户名)[:16]：Windows 上本地套接字是全机命名管道，
    # 多用户同时登录（快速切换用户 / RDP）不能撞名；RUNNERDECK_HOME 不同也互不干扰
class SingleInstance(QObject):
    message_received = pyqtSignal(str)
    def __init__(self, *, lock_dir: Path | None = None, name: str | None = None, parent=None)
    def acquire(self) -> bool       # QLockFile.tryLock(0)；成功后 QLocalServer.removeServer + listen（UserAccessOption）
    def send_to_primary(self, message: str = SHOW_MESSAGE, timeout_ms: int = 2000) -> bool
        # 重试连接到超时（对方可能拿了锁还没 listen）；Windows 上先 AllowSetForegroundWindow(ASFW_ANY)
    def release(self) -> None
```

### 6.2 `__main__.py`

- 新参数 `--autostart`（开机自启写进 Run 键的就是它）：启动后只在托盘，不显示窗口。普通启动：`settings.start_minimized` 且有托盘 -> 也只在托盘；否则显示窗口。没有托盘时一律显示窗口。
- 单实例：`acquire()` 失败 -> `send_to_primary()` 后退出 0（`--autostart` 时不发 show，直接退出）；主实例收到 show -> `window.show_and_raise()`。可测试的逻辑放在函数里（`_run_gui` 本身测试不跑）。
- 在 `QApplication` 创建前 `QGuiApplication.setFallbackSessionManagementEnabled(False)`；`app.commitDataRequest.connect(window.on_session_end, Qt.DirectConnection)`；有托盘时 `app.setQuitOnLastWindowClosed(False)`；`aboutToQuit` 兜底：还有运行中的 runner 就 `controller.shutdown()`。
- 顺序：日志 -> QApplication -> 单实例 -> controller -> `apply_theme(settings.theme)` -> window（含托盘）-> `StatusPoller(controller).start()` -> `controller.reconcile_autostart()` -> `controller.discover()` -> `controller.start_auto_runners()` -> 按上面规则 show 或不 show -> `exec`。
- `--smoke-test`：额外 `switchTo(window.detailPage)`（无记录，验证空状态），`MainWindow(..., tray=True)` 构造托盘；仍不走 `exec()`。
- `RunnerDeck.spec`：hiddenimports 加 `PyQt5.QtNetwork`、`runnerdeck.app.poller`、`runnerdeck.app.single_instance`、`runnerdeck.ui.tray`。

### 6.3 文档

`CHANGELOG.md` 的 `[Unreleased]` 写 M3 条目；`README.md` 补托盘 / 开机自启 / 导入 / 通知；`CLAUDE.md` 常用命令补 `--autostart`；`docs/PLAN.md` 目录结构处加注记（tray 在 ui/、autostart 在 core/）。

### 6.4 测试

`tests/test_main.py`：`--autostart` 解析、单实例分支（注入假 SingleInstance）、smoke 含详情页；`tests/app/test_single_instance.py`：同进程两个实例（临时 home）——第二个 acquire 失败、send 成功、主实例收到 show；名字随 home 变化。

## 7. 通用约定

同 m2-design §2：Python 3.11、`from __future__ import annotations`、类型齐全；代码 / 注释英文，界面文案和错误信息中文；UI 线程不做网络、子进程、注册表写入以外的磁盘 IO（停止记录、诊断日志都在工作线程）；信号参数用 `object` 承载 Python 对象。所有可显示异常是 `RunnerDeckError` 的 `str(e)`。

## 8. 文件归属（并行实现时互不覆盖）

| 负责 | 文件 |
| --- | --- |
| 地基（本会话，先于车道） | `docs/m3-design.md`、`core/models.py`、`core/status.py`、`tests/core/test_status.py`、`tests/core/test_models_runtime.py`、`tests/ui/conftest.py`（FakeController）、`tests/core/test_architecture.py`、`ui/ruff.toml` |
| A（core） | `core/stop_marks.py`、`core/diag.py`、`core/autostart.py`、`core/process.py`、`core/manager.py`、`core/cli.py`、`core/winproc.py`（如需要）、`tests/core/*`（除地基文件） |
| B（app） | `app/controller.py`、`app/poller.py`、`tests/app/conftest.py`、`tests/app/test_controller.py`、`tests/app/test_poller.py` |
| C（ui） | `ui/tray.py`、`ui/theme.py`、`ui/main_window.py`、`ui/pages/*`、`ui/components/*`、`tests/ui/*`（除 conftest.py） |
| D（入口 / 打包 / 文档） | `app/single_instance.py`、`tests/app/test_single_instance.py`、`src/runnerdeck/__main__.py`、`tests/test_main.py`、`RunnerDeck.spec`、`CHANGELOG.md`、`README.md`、`CLAUDE.md`（只改常用命令段）、`docs/PLAN.md`（只加注记） |

不要改 `pyproject.toml` / `uv.lock` / 地基文件（需要就在汇报里说明）。B 用 FakeManager 测试，不依赖 A 完成；C 只依赖 FakeController；D 的 smoke 需要 C 的 `detailPage` 和 `MainWindow(tray=)`，整合时再跑。

## 9. 验收

- 本地：`uv run ruff check . && uv run ruff format --check . && uv run pytest`；`uv run python -m runnerdeck --smoke-test` 退出 0。
- GitHub CI（windows-latest）：test（core 覆盖率 ≥ 80%）+ build（三个 exe 冒烟）绿。
- Windows 真机，`ssh XM`：真机 `pytest`；**spike**：CLI 启动 sandbox runner 时用 Python 读 `_diag/Runner_*.log`（运行中能否读）；CLI `stop`（强杀）后 `status` 显示“GitHub 会话未过期”而不是“被其他进程运行”。
- 真机桌面（用户）：
  1. 托盘：图标颜色随状态变、菜单启停、左键打开、关窗到托盘、托盘“退出”优雅停止全部；再次双击 exe 激活已有窗口。
  2. 开机自启：打开开关 -> 重启电脑 -> 不做任何操作，`auto_start` 的 runner 自动在线（同时观察网络 / 代理晚于 RunnerDeck 就绪时 runner 能否自己连上）。
  3. 导入 `D:\tmp\actions-runner`（PAT 需能访问它注册的仓库）-> 由 RunnerDeck 启动 -> 跑一次 job -> 停止 -> 取消登记；目录和 GitHub 注册原样保留。
  4. 通知：sandbox 跑一个会失败的 job（给 `spike.yml` 加一个失败开关）-> 托盘弹“失败”；成功的 job 默认不弹。
  5. 详情页：实时输出、诊断日志可看。
  6. 接管：另开终端用 CLI `start` 一个 runner -> 界面显示“被其他进程运行” -> 接管 -> 显示“等待旧会话过期”后恢复空闲。
  7. 注销：runner 运行中注销 Windows -> 不阻塞注销；重新登录后记录 runner 是否显示“等待旧会话过期”（结论写进 spike-notes）。
- 推 `v0.2.0`：Release 页出现 `RunnerDeck-0.2.0-win-x64.zip`（预发布）。

## 10. 实现后的修订（以代码为准）

**地基**
- controller 取全部显示状态的方法叫 `all_displays()`（`displays` 在 FakeController 里已是字典属性）。
- 新增 `tests/ui/test_fake_controller.py`：FakeController 的信号、方法（含关键字参数）、属性在 RunnerController 上都存在。

**core**
- `RunnerManager.__init__` 多三个注入点：`stop_marks`、`scan_processes`（`ProcessScanner`，真实实现 `process.scan_runner_processes()` 返回 `RunnerProcessInfo(pid, exe)`）、`stop_external`。只注入了 `find_processes` 的旧测试回退为按目录调用它，绝不扫描真实进程。`remove_runner` 的“是否在运行”仍按目录检查。
- `stop_marks.path_key()` 是唯一的路径规范化（`manager._path_key` 是它的别名）；`get` 把时间在未来超过 240 s 的记录也当作过期；损坏 / 路径不符的文件忽略但不删。`record_stop(at=None)` 用 manager 注入的时钟。
- `kill_stray` 也写强杀记录；`forget_runner` 不清停止记录（240 s 后自然过期）；`set_auto_start` / `forget_runner` / `kill_external` 找不到 id -> `RunnerNotFoundError`。
- `diag.list_diag_logs`：`_diag` 存在但读不了 -> `RunnerDeckError`；只列顶层 `*.log`。`read_tail` 去 BOM、`\r\n` -> `\n`。
- `autostart`：不支持的平台 `state()` 返回 `supported=False`，`enable/disable` 不注入注册表时抛 `AutostartError`；`disabled_by_user` 即使 Run 值不存在也照实读；frozen 时命令用 `sys.executable` 原样加引号。
- `signal_stop()`：送达失败返回 False 且不改状态；送达后之后的 `stop()` 不再重复发送，等超时后强杀。
- CLI `start` 停止阶段：`forced = not graceful or 被强杀`；可能仍存活的 runner 不写记录。新命令 `forget <key> [-y]`。

**app**
- `RunnerController` 多注入点 `autostart`（AutostartOps）、`diag`（DiagOps）、`clock`；默认实现在每次调用时才 import core 模块。
- `start_auto_runners` 跳过的 runner 合成一条 `failed("autostart", …、…)`。
- 本进程在运行的记录，`kill_external` / `take_over` 同步拒绝（`stop_external` 只放过祖先进程，会杀掉自己的子进程）。结束成功后缓存立即标成强杀（显示“GitHub 会话未过期”）；有存活进程时 take_over 不启动并 `failed("take_over")`。
- `import_runner` / `forget` 先重读配置，再发 `runner_imported` / `runner_forgotten`，然后 `config_changed`；之后都重新 `discover()`（`kill_stray` 成功后也是），发现横幅随之刷新。
- 自启相关失败的 op 统一为 `"launch_on_login"`；`set_launch_on_login` 先写注册表、成功后才存设置；`save_settings` 只在开关变化且支持时改注册表。
- `display()` 总是现算；变化检测用单独的“已发出”缓存。`StatusPoller` 默认挂在 controller 下（不必保存引用）；慢速模式下 `kick()` 仍查 GitHub；刷新进行中到期的 kick 会重新排队。
- `emergency_stop_all` 不写停止记录（会话结束时不做磁盘 IO）：注销后重新登录，GitHub 仍在线的那 ~70 s 可能显示“被其他进程运行”（验收第 7 项观察）。

**ui**
- 详情页头部没有 runner 版本（记录和 controller 都没有这个字段）。
- `show_and_raise()` 对之前最大化的窗口用 `showMaximized()`；`quit_app()` 返回 bool。
- `RunnerCard(record, parent=None, *, remote_busy=None)`；确认框辅助函数（`confirm_stop` / `confirm_kill` / `confirm_forget` / `ask_remove`）在 `runner_card.py`，详情页和托盘复用。接管也先确认；托盘停止执行 job 的 runner 同样确认。
- 托盘里显示 EXTERNAL 的 runner 一项是“接管 名字…”，打开详情页（在那里确认接管），不直接启动。
- 只有错误和警告在窗口隐藏时转托盘通知；成功提示不转。诊断日志在切到“诊断日志”页或点刷新时才加载。开关卡片自带“开 / 关”文案（没装 Qt 翻译时 qfluentwidgets 显示 On/Off）。
- `tests/ui/conftest.py` 新增自动夹具 `app_quits`：拦截 `QApplication.quit()`（在 `exec()` 之外调用会置位 Qt 的 quitNow，之后所有 qtbot 等待都超时）。

**入口**
- `server_name` = sha256(normcase(abspath(base)) + "\0" + 用户名)[:16]。`SingleInstance` 额外有 `name` / `lock_file` / `is_primary`；锁文件名移到 `single_instance.LOCK_FILE_NAME`。锁目录不可写等非“被占用”错误：记警告后照常启动（无锁、无服务）。
- `_run_gui` 拆出可测试的 `_run_app(app, *, autostart, factories)`；`main()` 多 `instance_factory` / `poller_factory`。
- `commitDataRequest` 接 `_Lifecycle.on_commit_data`（DirectConnection），它记下“会话结束中”再调 `window.on_session_end()`，异常只记日志；此后 `aboutToQuit` 不再阻塞调用 `shutdown()`（否则会拖住注销）。
- 第二个实例发 show 失败（主实例拿着锁但不应答）时弹“已经在运行”的提示后退出 0（`--autostart` 时静默退出）。
- `--smoke-test` 也真实获取 / 释放一次单实例锁和本地服务（验证 QtNetwork 打进了包）。

**独立评审（1 个 reviewer，9 条，已修 8 条）**
- 概览页 `config_changed` 时不再整体重建卡片：记录还在的卡片原地更新（`RunnerCard.set_record`）。原来卡片的确认框开着时被 `deleteLater`，确认后从已销毁对象 emit -> RuntimeError（reviewer 离屏复现）。卡片的确认框返回后再用 `sip.isdeleted` 检查一次（自己的记录被删时丢弃回答）。
- `discover` 不把“里面嵌着已登记 runner”的根目录子目录当残留；`kill_stray` 遇到这种目录拒绝（否则会强杀 RunnerDeck 自己正在跑 job 的 runner）。
- 状态规则 2、4 的修订见 §2。
- `__main__`：没有可见窗口时用原生 `QMessageBox`（qfluentwidgets 的 MessageBox 是盖在父窗口上的遮罩，父为 None 时崩溃）：第二实例“已经在运行”提示、excepthook。
- `save_settings` 只写与缓存相比真正改动的字段，叠加到重新读出的设置上；基于过期副本的保存不会把刚打开的开机自启关回去，也不碰注册表。
- 登录框不叠在另一个模态框上（有 `activeModalWidget` 时 500 ms 后再试）。
- `StopMarks.get` 不再删除过期文件（读后删可能删掉 CLI 刚写的新记录；下次 `record` 覆盖即可）。
- **未修（已知限制）**：注销被用户取消（“仍要注销 / 取消”选了取消）时，`commitDataRequest` 已经让 runner 停止，不会自动重启也没有提示；要靠原生 `WM_ENDSESSION(wParam=0)` 过滤器，留到 M4。真机验收第 7 项一并观察。
