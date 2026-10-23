# M2 设计：UI 最小可用 + 打包 + release（契约）

M2 各部分按本文的接口实现，测试按本文的约定写。core 层接口见 `docs/m1-design.md`（含 §13 修订），runner 行为依据 `docs/spike-notes.md`（§5 停止、§10 helper / `.env` 执行策略）。PLAN.md 里的 M2 范围：RunnerController + workers、登录 / 概览 / 添加向导 / 启停 / 移除 / 基础设置、PyInstaller onedir + `--version`、CI 加 UI 冒烟与打包 job、`release.yml`，推 `v0.1.0`。

已确定的三个决策：**关窗 = 确认后优雅停止全部 runner 并退出**（托盘 M3）；**GitHub 端状态手动刷新**（Poller M3）；实现方式沿用 M1 的工作流（本文契约 → 并行实现 → 对抗评审）。

## 0. 相对 PLAN.md 的调整

| 调整 | 理由 |
| --- | --- |
| 新增 `core/console_helper.py` + `process.Launch.HELPER` | GUI 没有控制台，Ctrl+Break 到不了 run.cmd（spike §5）；helper 方案 spike §10 实测 0.91 s 优雅停止 |
| 新增 `core/account.py` | PAT 来源（`RUNNERDECK_PAT` > keyring）、token 过期/权限文案，CLI 和 Controller 共用，不再写在 cli.py 里 |
| `AddPlan.runner_env` + `Step.ENV` | 添加向导的“PowerShell 执行策略”选项（spike §10，`.env` 写 `PSExecutionPolicyPreference=RemoteSigned`） |
| 登录做成对话框而不是导航页 | FluentWindow 的导航项不便按登录状态增删；启动没有 PAT 时弹 `LoginDialog`，设置页“更换 PAT”复用 |
| PLAN CI 表的 `ui-smoke` 并入 `test`（pytest-qt 离屏）+ `build`（exe `--smoke-test`） | 少一个 job，覆盖相同 |
| 打包出三个 exe 共享一个目录 | `RunnerDeck.exe`（窗口）、`runnerdeck-helper.exe`（控制台，必需）、`runnerdeck-cli.exe`（控制台，排障用） |

## 1. 地基（已完成，不要改接口；有问题提出来）

### 1.1 `core/console_helper.py`（只用标准库）

```text
runnerdeck-helper <runner_dir>        # 开发：python -I -m runnerdeck.core.console_helper <dir>
```

- 以 `CREATE_NEW_PROCESS_GROUP`（不加 `CREATE_NO_WINDOW`）启动 `cmd.exe /c run.cmd`，`cwd=runner_dir`，`stdin=DEVNULL`，stdout 继承（runner 输出原样流到父进程的管道，`process.py` 的解析不变）。
- stdin 收到一行 `stop` → 向 run.cmd 的进程组发 `CTRL_BREAK_EVENT`；stdin EOF（GUI 崩溃/关闭管道）→ 同样发 Ctrl+Break，90 s（`ORPHAN_STOP_TIMEOUT_S`）后仍没退出则 `taskkill /T /F`。退出码 = run.cmd 的。自己不打印任何东西。
- 父进程用 `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` 启动它：它因此拥有一个隐藏控制台，run.cmd 继承。

### 1.2 `winproc.helper_command() -> list[str]`

frozen 时 `Path(sys.executable).with_name("runnerdeck-helper.exe")`（不存在 → `ProcessError("找不到 …，安装不完整")`）；开发时 `[python.exe, "-I", "-m", "runnerdeck.core.console_helper"]`（`pythonw.exe` 换成同目录 `python.exe`；`-I` 防止 cwd / PYTHONPATH 植入模块）。`winproc.HELPER_STOP_COMMAND = b"stop"`。

### 1.3 `process.Launch` 与 `RunnerProcess(launch=)`

```python
class Launch(StrEnum): CONSOLE = "console"; HELPER = "helper"
RunnerProcess(runner_dir, *, env=None, on_line=None, on_state=None, on_job_finished=None,
              popen=subprocess.Popen, max_lines=2000, launch=Launch.CONSOLE)
proc.launch -> Launch
```

`HELPER`：`start()` 执行 `[*helper_command(), str(runner_dir)]`，`cwd=None`（**不是** runner 目录：job 可以往那里写文件），`stdin=PIPE`，`creationflags = NEW_PROCESS_GROUP | NO_WINDOW`；`stop()` 往 stdin 写 `b"stop\n"` 代替 `send_ctrl_break`（写失败 → 直接强杀）；`pid` 是 helper 的 pid，`kill_tree` 从它开始杀整棵树；其余（状态机、空闲 15 s / 执行 job 90 s 超时、leftover 清理、ERROR 判定、`StopResult.method` 仍为 `CTRL_BREAK`）不变。`manager.process_for(record, *, launch=Launch.CONSOLE, ...)` 透传。**CLI 只用 CONSOLE**（架构测试检查 cli.py 里没有 `Launch.HELPER`）。

### 1.4 `manager`：`runner_env`

```python
POWERSHELL_POLICY_ENV = {"PSExecutionPolicyPreference": "RemoteSigned"}
manager.plan_add(owner, repo, *, name=None, labels=None, root=None, version=None,
                 runner_env: Mapping[str, str] | None = None) -> AddPlan   # AddPlan.runner_env: tuple[tuple[str, str], ...]
Step.ENV = "env"   # configure 成功后、VERIFY 之前，只在 runner_env 非空时出现
```

### 1.5 `core/account.py`

```python
ENV_PAT = "RUNNERDECK_PAT"; SOURCE_ENV = "环境变量 RUNNERDECK_PAT"; SOURCE_KEYRING = "系统凭据管理器"
TOKEN_KIND_LABELS: Mapping[str, str]              # "fine-grained" -> "fine-grained PAT" ...
class NotLoggedInError(RunnerDeckError)
def resolve_pat(account: str | None, environ, *, login_hint: str) -> tuple[str, str]   # (pat, source)
def format_expiry(expires_at: datetime | None, now) -> tuple[str, bool]                 # (文案, 需要注意)
def permission_hints(user: GitHubUser) -> list[str]
```

### 1.6 测试与守卫

- `tests/conftest.py` 设 `QT_QPA_PLATFORM=offscreen`；`pyproject` `qt_api = "pyqt5"`。
- `tests/core/test_architecture.py`：`ui/**` 不得 import `runnerdeck.core.{manager,github_client,process,installer,release,store,secrets,winproc,cli}`、`httpx`、`subprocess`（只能经 controller；`core.models / status / errors / paths / account / redact / secret_input / logs` 可以直接用）；`app/*` 不得 import `runnerdeck.ui`、`qfluentwidgets`；`console_helper.py` 只能 import 标准库。
- 入口：`pyproject` 已登记 `runnerdeck-helper`（console script）和 `runnerdeck`（gui script → `runnerdeck.__main__:main`）。

## 2. 通用约定

- Python 3.11，`from __future__ import annotations`，类型标注齐全；代码、注释、docstring 英文，**界面文案和错误信息中文**。
- UI 线程绝不做网络 / 子进程 / 大量磁盘 IO；所有 manager 调用经 `app/workers.py` 放到 `QThreadPool`。
- Qt 信号参数用 `object` 承载 Python 对象（`pyqtSignal(object)`），避免 PyQt 的类型注册问题；`str` / `bool` / `int` 直接用。
- 所有要显示的异常都是 `RunnerDeckError` 的 `str(e)`；其他异常 → 记日志 + 显示“内部错误：<type>: <msg>，详情见日志”。
- 日志：`logging.getLogger(__name__)`；app/ui 不直接处理 token。
- 测试用 pytest-qt（`qtbot`），离屏；core 的夹具 `isolated_home` / `memory_keyring` 在 `tests/core/conftest.py`，app/ui 测试需要时在各自 conftest 里复制同名夹具（不要 import tests.core）。

## 3. `app/workers.py`

```python
class TaskSignals(QObject):
    finished = pyqtSignal(object)        # fn 的返回值
    failed = pyqtSignal(str, object)     # (用户可读信息, 异常对象)；RunnerDeckError 用 str(e)，其他加“内部错误：”前缀并 log.exception
    progress = pyqtSignal(str, str)      # (step_id, 中文说明)，对应 manager.ProgressFn

class Task(QRunnable):
    def __init__(self, fn: Callable[..., Any], *args, progress: bool = False, **kwargs)
        # progress=True：以 progress=<回调> 调用 fn，回调在工作线程 emit signals.progress
    signals: TaskSignals

def submit(pool: QThreadPool, fn, *args, on_finished=None, on_failed=None, on_progress=None,
           progress: bool = False, **kwargs) -> Task
    # 连接槽后 pool.start(task)；槽通过 Qt queued connection 在主线程执行
```

`Task.run()` 里 `except BaseException` 都要转成 `failed`，绝不让异常跑到线程池里丢失。

## 4. `app/controller.py`：`RunnerController(QObject)`

界面对 core 的唯一入口。构造：

```python
RunnerController(*, store: ConfigStore | None = None, manager_factory: Callable[[ConfigStore, ClientFactory], RunnerManager] | None = None,
                 environ: Mapping[str, str] = os.environ, pool: QThreadPool | None = None, launch: Launch = Launch.HELPER, parent=None)
```

- 默认 `store = ConfigStore()`，`manager = RunnerManager(store, self._client_factory)`，`_client_factory()` 用 `account.resolve_pat(config.account, environ, login_hint="请先登录")` 建 `GitHubClient(pat, proxy=settings.proxy)`。
- 持有 `self._processes: dict[str, RunnerProcess]`（key = record.id），`self._busy: set[str]`（正在执行操作的 record id）。

**只读属性 / 同步方法（主线程，不阻塞）**

```python
config: AppConfig                       # 最近一次 load 的缓存；每次写操作后刷新并 emit config_changed
logged_in: bool                         # config.account 非空，或 environ 里有 RUNNERDECK_PAT
records: list[RunnerRecord]
local_state(record_id) -> tuple[LocalState, str | None]      # (state, current_job)，无进程 -> (STOPPED, None)
is_running(record_id) -> bool
is_busy(record_id) -> bool
output(record_id) -> list[str]          # RunnerProcess.output()，无进程 -> []
running_ids() -> list[str]
display(record_id) -> DisplayStatus     # 上次 refresh_statuses 的结果；没有则 status.merge_status(local, None)
```

**异步操作（立即返回，结果走信号）**

| 方法 | 工作线程做什么 | 成功信号 | 失败信号 `failed(op, msg)` 的 `op` |
| --- | --- | --- | --- |
| `login(pat: str)` | `secrets.validate_pat_format` → `GitHubClient(pat).get_user()` → `secrets.save_pat` → `config.account` 保存 | `logged_in(GitHubUser)` + `config_changed` | `"login"` |
| `logout()` | `secrets.delete_pat`，`account=None` | `logged_out()` + `config_changed` | `"logout"` |
| `load_account()` | `get_user()`（设置页显示 token 信息） | `account_loaded(GitHubUser, source: str)` | `"account"` |
| `load_repos()` | `client.list_repos()` | `repos_loaded(list[Repo])` | `"repos"` |
| `plan_add(owner, repo, *, name=None, labels=None, ps_policy=True)` | `manager.plan_add(..., runner_env=POWERSHELL_POLICY_ENV if ps_policy else None)` | `plan_ready(AddPlan)` | `"plan_add"` |
| `add_runner(plan, *, start_after=False)` | `manager.add_runner(plan, progress=…)`；`start_after` 时接着 `start(record.id)` | `add_progress(step, text)` 多次 → `runner_added(RunnerRecord)` + `config_changed` | `"add"`（`InstallError` 时 `failed` 的异常对象带 `.output`） |
| `start(record_id)` | `manager.process_for(record, launch=self.launch, on_line/on_state/on_job_finished=桥接)` → `proc.start()` | `runner_state(id, LocalState, job)`（来自进程回调） | `"start"` |
| `stop(record_id)` | `proc.stop()`（默认超时：空闲 15 s / job 90 s） | `runner_stopped(id, StopResult)` + `runner_state` | `"stop"` |
| `start_all()` / `stop_all()` | 对每个未运行 / 运行中的 runner 分别提交 | 同上 | 同上 |
| `remove(record_id, *, keep_work=False)` | 运行中先 `stop`，再 `manager.remove_runner(id, keep_work=…, progress=…)` | `remove_progress(step, text)` → `runner_removed(id, RemoveResult)` + `config_changed` | `"remove"` |
| `refresh_statuses()` | `manager.statuses(self._processes)` | `statuses_updated(list[RunnerStatusRow])` | `"status"`（整体失败才用；单仓库失败在 row.remote_error 里） |
| `save_settings(settings: AppSettings)` | `config.settings = settings; store.save` | `config_changed(AppConfig)` | `"settings"` |
| `open_dir(record_id)` | 主线程 `QDesktopServices.openUrl(QUrl.fromLocalFile(path))` | — | — |
| `shutdown(timeout=None) -> list[str]` | **阻塞**：并行 `stop()` 所有运行中的 runner，返回没停下的 runner 名；关窗时由窗口在进度对话框里调用（可放到线程里并 `processEvents`，但调用方约定它可阻塞） | — | — |

**其他信号**：`busy_changed(record_id: str, busy: bool)`（每个 runner 同时只允许一个 start/stop/remove；add 由 manager 的锁串行）、`runner_line(record_id, line)`、`runner_job_finished(record_id, job, result)`。

**线程桥接**：`RunnerProcess` 的回调在读取线程里调用，Controller 用一个内部 `_Bridge(QObject)`（信号 `line(str, str)`、`state(str, object, object)`、`job(str, str, str)`，queued 到主线程）转发后再 emit 对外信号。回调必须快、不得调用 `start()/stop()`（RunnerProcess 约束）。

**错误处理**：`failed(op, message)` 永远发自主线程；`message` 已过 `redact()`。不弹框，弹框是 UI 的事。

**测试 `tests/app/test_controller.py`**（pytest-qt）：用假 manager（记录调用、可注入异常）和假 RunnerProcess（手动触发 `on_state` 回调）验证：每个操作 emit 对应信号、失败 emit `failed`、busy 互斥（同一 runner 第二次 `start` 被拒绝且 emit `failed`）、`shutdown` 停止全部、`runner_state` 从非主线程回调到达时在主线程 emit。

## 5. `ui/`（qfluentwidgets，中文文案）

ui 只拿 Controller 的信号/方法，不 import manager / github_client / process（守卫见 §1.6）。所有页面构造函数接收 `controller: RunnerController`（测试传假 controller：一个有同名信号/方法的 `QObject`，放在 `tests/ui/conftest.py`）。

| 文件 | 内容 |
| --- | --- |
| `ui/main_window.py` | `MainWindow(FluentWindow)`：导航 概览（`FIF.HOME`）/ 添加 runner（`FIF.ADD`）/ 设置（`FIF.SETTING`，底部）；`showEvent` 首次显示且 `not controller.logged_in` → 弹 `LoginDialog`；`closeEvent`：有运行中 runner → `MessageBox("退出", "有 N 个 runner 正在运行，退出会先停止它们（执行中的 job 会被取消）…")`，确认后调用 `controller.shutdown()`（期间显示 `StateToolTip` 或 `IndeterminateProgressBar`），再 `accept()`；无运行中的直接退出。`statuses_updated` / `runner_state` 等信号由各页自行订阅。监听 `controller.failed` 用 `InfoBar.error` 显示（标题按 op 映射中文：登录失败 / 添加失败 / 启动失败 …）。`setWindowTitle("RunnerDeck")`，图标 `ui/resources/runnerdeck.png`（存在时）。 |
| `ui/pages/dashboard_page.py` | `DashboardPage(QWidget)`，`objectName="dashboard"`。顶部：统计文字（运行中 / 空闲 / 异常 / 已停止 数量）+ `PrimaryPushButton("全部启动")`、`PushButton("全部停止")`、`PushButton("刷新")`（调 `refresh_statuses`，忙时禁用）。中部：`SmoothScrollArea` 里一列 `RunnerCard`；空状态 `BodyLabel("还没有 runner，去“添加 runner”页注册一个")`。订阅 `config_changed`（重建卡片）、`runner_state` / `busy_changed` / `statuses_updated`（更新对应卡片）。 |
| `ui/pages/add_wizard_page.py` | `AddWizardPage(QWidget)`，`objectName="add"`，`QStackedWidget` 三步 + 底部 `上一步 / 下一步`：① `RepoStep`：`SearchLineEdit` 过滤 + `ListWidget`（`full_name`，后缀“私有/公开”“已归档”“fork”，本机已有 runner 的标出），进入页面时 `load_repos()`，加载中显示 `IndeterminateProgressRing`；② `ConfigStep`：`LineEdit` 名称（默认空 = 用 manager 默认 `{主机名}-{repo}`，placeholder 说明）、`LineEdit` 标签（逗号分隔，预填 `config.settings.default_labels`）、`CheckBox("允许 PowerShell 步骤（.env 写入 PSExecutionPolicyPreference=RemoteSigned）")` 默认勾选、`CheckBox("添加后立即启动")` 默认勾选；点“下一步”先 `plan_add`，`plan_ready` 后显示摘要（目录、版本、标签）和 `plan.warnings`（`InfoBar.warning` 或列表）；`plan.needs_confirmation` 时需勾选 `CheckBox("我已了解上述风险")` 才能继续；③ `ProgressStep`：按 `manager.Step` 顺序（release / download / extract / token / configure / env / verify / save / done）的列表，当前步骤前转圈、完成打勾（`FIF.ACCEPT`）、文案来自 `add_progress`；失败：`InfoBar.error` + 展开 `PlainTextEdit` 显示 `exc.output`（`InstallError`）；成功：`InfoBar.success` + 2 s 后跳回概览并重置向导。 |
| `ui/pages/settings_page.py` | `SettingsPage(ScrollArea)`，`objectName="settings"`，`SettingCardGroup`：**账号**（`PrimaryPushSettingCard` 显示登录名 + token 类型/过期（`load_account` 结果，用 `account.format_expiry` / `TOKEN_KIND_LABELS`），按钮“更换 PAT”→ `LoginDialog`；`PushSettingCard("退出登录")` 确认后 `logout()`）；**runner**（`PushSettingCard` runner 根目录 → `QFileDialog.getExistingDirectory`；自定义 `LineEdit` 卡片：默认标签（逗号分隔）、代理（空 = 不设置））；**关于**（版本 `runnerdeck.__version__`、仓库 `HyperlinkCard`、日志目录 `paths.log_dir()` 打开按钮）。改动即 `save_settings`（标签/代理用失焦或回车提交；代理格式错误 `InfoBar.error`）。主题 / 托盘 / 自启 / 轮询间隔留 M3。 |
| `ui/components/runner_card.py` | `RunnerCard(CardWidget)`：左：`StrongBodyLabel(record.name)`、`CaptionLabel(owner/repo)`、`StatusBadge`、`CaptionLabel` 当前 job / detail；右：`PrimaryPushButton("启动")` 或 `PushButton("停止")`（按 `is_running`），busy 时禁用并显示 `IndeterminateProgressRing(16px)`；`TransparentToolButton(FIF.MORE)` → `RoundMenu`：打开目录、查看输出、在 GitHub 打开（`QDesktopServices.openUrl(record.repo_url + "/settings/actions/runners")`）、移除（`MessageBoxBase` 含 `CheckBox("保留 _work 目录")`，运行中提示会先停止）。方法：`update_state(state: LocalState, job)`, `update_display(display: DisplayStatus)`, `set_busy(bool)`。信号：`start_requested(str)`, `stop_requested(str)`, `remove_requested(str, bool)`, `logs_requested(str)`。 |
| `ui/components/status_badge.py` | `StatusBadge(InfoBadge)` + `BADGE_LEVELS: Mapping[DisplayState, InfoLevel]`：IDLE/BUSY → SUCCESS，STARTING/CONNECTING/RECONNECTING/STOPPING/SESSION_LINGERING → WARNING（BUSY 用 INFOAMTION 也可，但和 IDLE 区分开），STALLED/ERROR/MISSING/EXTERNAL → ERROR，STOPPED/UNKNOWN → INFOAMTION（灰）。文案 `status.LABELS` / `status.display_label(state, job)`。 |
| `ui/components/log_dialog.py` | `LogDialog(MessageBoxBase)`：标题 runner 名，`PlainTextEdit`（只读、等宽）初始内容 `controller.output(id)`，订阅 `runner_line` 实时追加（只追加自己的 id），`CheckBox("自动滚动")` 默认勾选；按钮“复制全部”“关闭”。 |
| `ui/components/login_dialog.py` | `LoginDialog(MessageBoxBase)`：`PasswordLineEdit`（placeholder “github_pat_… 或 ghp_…”）、`HyperlinkLabel` 指向 `https://github.com/settings/personal-access-tokens/new`、`BodyLabel` 权限说明（fine-grained：选中仓库 + Administration: Read and write；classic：repo）、错误 `CaptionLabel`。“登录”按钮：先 `secrets.validate_pat_format`（InvalidInputError 的文案直接显示，不回显输入），再 `controller.login(pat)`，等待期间禁用按钮；`logged_in` → `accept()`；`failed("login", msg)` → 显示 msg。`static def ask(controller, parent) -> bool`。 |
| `ui/resources/` | `runnerdeck.png`（256 px）、`runnerdeck.ico`，由 `scripts/make_icon.py` 生成（C 负责）。 |

**测试 `tests/ui/`**（pytest-qt，假 controller）：`test_smoke.py` 主窗口创建、三页可切换（`switchTo`）、关窗无运行 runner 直接接受；`test_dashboard.py` 卡片按 `config_changed` 重建、`runner_state` 改徽章/按钮、点击按钮 emit 到 controller；`test_add_wizard.py` 三步能走（假 controller 的 `plan_add` 立即 emit `plan_ready`，`add_runner` emit 几次 `add_progress` 后 `runner_added`）、公开仓库需勾选风险；`test_login_dialog.py` 格式校验文案、成功 accept；`test_settings.py` 修改根目录 / 代理调用 `save_settings`。

## 6. `__main__.py` 与打包（C）

```python
# src/runnerdeck/__main__.py
def main(argv: Sequence[str] | None = None) -> int
```

- `--version`：打印 `RunnerDeck <__version__>` 退出 0；窗口子系统 exe 没有 stdout，用 `ctypes.windll.kernel32.AttachConsole(-1)` 尽力附加到父控制台再写（失败也退出 0）。
- `--smoke-test`：设 `QT_QPA_PLATFORM=offscreen`，`RUNNERDECK_HOME` 指到临时目录，用**假 controller**（或真 controller 但不登录）创建 `MainWindow`，`switchTo` 三个页面，`QTimer.singleShot(0, app.quit)`，退出 0；任何异常 → stderr + 退出 1。CI 用它验证打包产物里 Qt 插件 / qfluentwidgets 资源齐全。
- 正常启动：单实例（`QLockFile(paths.local_dir() / "runnerdeck.lock")`，已有实例 → `MessageBox` 提示并退出 0，M3 改成激活已有窗口）；`logs.setup_logging(file_name="runnerdeck.log", level=INFO)`；`QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)` 等高 DPI 属性要在 `QApplication` 之前；`setTheme(Theme.AUTO)`；`RunnerController(launch=Launch.HELPER)`；`MainWindow(controller).show()`；`app.exec()` 后返回码。未捕获异常 → `sys.excepthook` 记日志并 `MessageBox`。
- `RunnerDeck.spec`（仓库根）：
  - `a_gui = Analysis(["src/runnerdeck/__main__.py"], pathex=["src"], hiddenimports=[*collect_submodules("qfluentwidgets"), "keyring.backends.Windows", "truststore", "win32ctypes.pywin32"…按需], datas=[*collect_data_files("qfluentwidgets"), ("src/runnerdeck/ui/resources", "runnerdeck/ui/resources")])`
  - `a_helper = Analysis(["src/runnerdeck/core/console_helper.py"], excludes=["PyQt5", "qfluentwidgets", "runnerdeck", "httpx", "pydantic", "keyring", "psutil"])`
  - `a_cli = Analysis(["scripts/entry_cli.py"] 或等价入口，排除 PyQt5/qfluentwidgets)`
  - `EXE(..., name="RunnerDeck", console=False, icon="src/runnerdeck/ui/resources/runnerdeck.ico")`、`EXE(..., name="runnerdeck-helper", console=True)`、`EXE(..., name="runnerdeck-cli", console=True)`，三者 `exclude_binaries=True`，一个 `COLLECT(..., name="RunnerDeck")` → `dist/RunnerDeck/`。
  - 版本：`importlib.metadata.version("runnerdeck")` 在 frozen 包里要能读到 → `datas` 里加 `copy_metadata("runnerdeck")`。
- `scripts/build.ps1`（PowerShell 7 / 5 都能跑）：`uv sync --locked` → `uv run pyinstaller RunnerDeck.spec --noconfirm --clean` → 读版本（`uv run python -c "import runnerdeck;print(runnerdeck.__version__)"`）→ `Compress-Archive dist/RunnerDeck/* → dist/RunnerDeck-<ver>-win-x64.zip`（zip 根下直接是 `RunnerDeck.exe` 等）；失败即 `exit 1`。
- `scripts/make_icon.py`：用 PyQt5 `QPainter` 画 256 px 圆角方块 + “RD” 字样，保存 `ui/resources/runnerdeck.png` 与 `.ico`（Qt 的 ico 写入支持多尺寸，至少 16/32/48/256）。生成的文件提交进仓库。
- `CLAUDE.md` 常用命令补：`uv run python -m runnerdeck --smoke-test`、`uv run runnerdeck-helper`（说明用途）。

## 7. CI / 发布（D）

- `.github/workflows/ci.yml`：`test` job 增加 `env: QT_QPA_PLATFORM: offscreen`，命令改为 `uv run pytest --cov=runnerdeck.core --cov-report=term --cov-fail-under=80`（core 门槛不变，app/ui 覆盖率只报告：`--cov=runnerdeck.app --cov=runnerdeck.ui` 可加但不设门槛；注意 `--cov-fail-under` 作用于总体，所以要么只 `--cov=runnerdeck.core`，要么用 `[tool.coverage.report] fail_under` 的 per-path 方案——选前者）。新增 `build` job（windows-latest，`needs: [lint]` 不需要，和 test 并行）：checkout、setup-uv、`pwsh scripts/build.ps1`、`dist\RunnerDeck\runnerdeck-cli.exe --version`（输出含版本号）、`dist\RunnerDeck\runnerdeck-helper.exe`（无参数应退出码 2 且 stderr 有 usage）、`dist\RunnerDeck\RunnerDeck.exe --smoke-test`（退出码 0；PowerShell 里窗口程序不阻塞，用 `Start-Process -Wait -PassThru` 取 `ExitCode`）、`actions/upload-artifact@v4`（`dist/*.zip`，`retention-days: 7`）。
- `.github/workflows/release.yml`：`on: push: tags: ["v*"]`；顶层 `permissions: contents: read`；job `release`（windows-latest，`permissions: contents: write`）：
  1. 校验 `${GITHUB_REF_NAME#v}` == `pyproject.toml` 的 `version`（`uv version --short` 或 python 读 toml），不一致 `exit 1`。
  2. `uv sync --locked` → `ruff check` + `ruff format --check` → `pytest`（离屏）。
  3. `scripts/build.ps1` → 冒烟同 CI。
  4. 从 `CHANGELOG.md` 截取 `## [X.Y.Z]` 到下一个 `## [` 之间的段落为 release 正文（脚本 `scripts/changelog_section.py <version>`，找不到则失败）。
  5. `gh release create "$TAG" dist/*.zip --title "RunnerDeck $TAG" --notes-file notes.md`，tag 含 `-alpha/-beta/-rc` 加 `--prerelease`；`GH_TOKEN: ${{ github.token }}`。
  - 第三方 action 版本规则同 PLAN（`actions/checkout@v7`、`astral-sh/setup-uv@v10.3.1`、`actions/upload-artifact@v4`）。
- `CHANGELOG.md`：`[Unreleased]` 下补 M2 条目（GUI、console helper、打包、release 流程）；版本号改 0.1.0 由本会话在发布步骤做。
- `README.md`：加“下载与运行”（Release 页 zip → 解压 → `RunnerDeck.exe`；SmartScreen “未知发布者”属正常，附校验说明）、“从源码运行”、“PAT 权限”。
- `docs/PLAN.md` CI 表 `ui-smoke` 行加注记“并入 test + build（M2）”。

## 8. 文件归属（并行实现时互不覆盖）

| 负责 | 文件 |
| --- | --- |
| A（app） | `src/runnerdeck/app/__init__.py`、`app/workers.py`、`app/controller.py`、`tests/app/__init__.py`、`tests/app/conftest.py`、`tests/app/test_workers.py`、`tests/app/test_controller.py` |
| B（ui） | `src/runnerdeck/ui/__init__.py`、`ui/main_window.py`、`ui/pages/*.py`、`ui/components/*.py`、`ui/ruff.toml`（TID251 同 §1.6 的禁令）、`tests/ui/*` |
| C（入口与打包） | `src/runnerdeck/__main__.py`、`RunnerDeck.spec`、`scripts/build.ps1`、`scripts/make_icon.py`、`scripts/entry_cli.py`（如需要）、`src/runnerdeck/ui/resources/runnerdeck.{png,ico}`、`tests/test_main.py`、`CLAUDE.md`（只改常用命令段） |
| D（CI/发布） | `.github/workflows/ci.yml`、`.github/workflows/release.yml`、`scripts/changelog_section.py` + `tests/test_changelog_section.py`、`CHANGELOG.md`、`README.md`、`docs/PLAN.md`（只加注记） |

不要改 `pyproject.toml` / `uv.lock` / `core/*`（需要改就在汇报里说明）。B 对 Controller 的依赖只通过 §4 的名字；A 的 `MainWindow`/页面不存在也要能独立测试；C 的 `--smoke-test` 需要 `MainWindow`，实现时按 §5 的构造签名 `MainWindow(controller)` 写，整合时再跑。删除 `app/.gitkeep`、`ui/*/.gitkeep` 由各自负责人处理。

## 9. 验收

- 本地：`uv run ruff check . && uv run ruff format --check . && uv run pytest`（macOS 离屏）；`uv run python -m runnerdeck --smoke-test` 退出 0。
- Windows（`ssh XM`）：同上 + `pwsh scripts/build.ps1` + `dist\RunnerDeck\RunnerDeck.exe --smoke-test` + `pytest -k "helper"`（真实 helper 进程）。
- 真机 GUI（用户桌面）：从零给两个仓库各添加一个 runner、各跑通一次 workflow、界面全程不卡、关窗优雅停止（GitHub 上 runner 11 s 内 offline、无残留进程）。
- 推 `v0.1.0`：Release 页面出现 `RunnerDeck-0.1.0-win-x64.zip`，解压即可运行。

## 10. 实现后的修订（以代码为准）

（整合与评审后填写。）
