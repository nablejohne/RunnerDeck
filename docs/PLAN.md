# RunnerDeck：Windows 自托管 Runner 管理器开发方案

Oct 22, 2026 · @Jhone

## 概述与目标

RunnerDeck 是一个 Windows 托盘应用：登录一次 PAT，就能把这台笔记本一键注册成多个个人仓库的 self-hosted runner，并在一个界面里统一启停、看状态、看日志、移除。

个人账号下的 runner 只能注册到单个仓库，所以每个仓库需要一个独立目录、独立注册的 runner 实例。RunnerDeck 把这套重复操作（下载、拿 token、config、启动、移除）全部自动化，并补上官方没有的状态面板。

**MVP 范围**

- 用 PAT 登录，列出自己的仓库
- 添加向导：选仓库 → 填名称和标签 → 自动下载、注册、启动
- 概览页：每个 runner 的本地状态 + GitHub 端在线/忙碌状态、当前 job
- 启动 / 停止 / 移除 / 打开目录 / 查看日志
- 托盘常驻、开机自启、启动时自动拉起 runner
- 导入已手动配置好的 runner 目录（例如现在的 `D:\tmp\actions-runner`）

**非目标（当前不做）**

- 组织级、企业级 runner 与 runner group 管理（数据模型预留 scope 字段）
- 自动扩缩容、容器隔离、ephemeral runner
- macOS / Linux 支持

## 技术选型

核心原则：界面用 PyQt5 + QFluentWidgets，业务逻辑放在不依赖 Qt 的纯 Python 层，方便测试和以后换界面。

| 用途 | 选择 | 理由 |
| --- | --- | --- |
| 语言 | Python 3.11 | PyQt5 5.15 支持良好，兼顾性能与库兼容 |
| GUI | PyQt5 5.15 + PyQt-Fluent-Widgets（`import qfluentwidgets`） | Fluent 风格的导航窗口、卡片、设置页、托盘菜单现成可用 |
| HTTP | httpx（同步，放在工作线程里调用） | 接口清晰，代理设置简单，可用 respx 做 mock |
| 数据模型 | pydantic v2 | 配置与 API 响应校验 |
| PAT 存储 | keyring | 写入 Windows 凭据管理器，不落盘明文 |
| 进程管理 | subprocess + psutil | core 层不依赖 QProcess；psutil 用于找进程树、孤儿进程 |
| Windows 服务（M4） | pywin32 | 查询、启停 runner 服务 |
| 日志 | 标准库 logging + RotatingFileHandler | 无额外依赖 |
| 测试 | pytest + respx（+ pytest-qt 可选） | core 层全覆盖，UI 只做冒烟 |
| 依赖管理 | uv + pyproject.toml | 安装快，锁文件可复现 |
| 代码检查 | ruff | 格式化 + lint 一体 |
| 打包 | PyInstaller（onedir），可选 Inno Setup 出安装包 | 对 Qt 资源支持成熟 |

许可提示：PyQt-Fluent-Widgets 的 PyQt5 免费版为 GPLv3，个人开源项目直接用即可，本项目也建议采用 GPLv3。

## 核心原理

整个应用只依赖两样东西：GitHub REST API（拿 token、查状态）和 runner 自带的 `config.cmd` / `run.cmd`（注册、运行、注销）。

**用到的 GitHub REST API**

| 接口 | 用途 | 阶段 |
| --- | --- | --- |
| `GET /user` | 校验 PAT，取登录名 | M1 |
| `GET /user/repos?affiliation=owner&per_page=100` | 仓库列表，按 Link 头分页；`private` 字段用于安全提醒 | M1 |
| `POST /repos/{owner}/{repo}/actions/runners/registration-token` | 注册 token，约 1 小时有效，用时即取 | M1 |
| `POST /repos/{owner}/{repo}/actions/runners/remove-token` | 注销 token | M1 |
| `GET /repos/{owner}/{repo}/actions/runners` | runner 列表：`id`、`status`（online/offline）、`busy`、`labels` | M1 |
| `DELETE /repos/{owner}/{repo}/actions/runners/{runner_id}` | 本地目录已丢失时强制注销 | M1 |
| `GET /repos/actions/runner/releases/latest` | 最新版本、`actions-runner-win-x64-*.zip` 下载地址；release 正文中带各平台 SHA256 | M1 |
| `GET /repos/{owner}/{repo}/contents/.github/workflows` | 扫描哪些 workflow 用了 `runs-on: self-hosted` | M4 |
| `GET /repos/{owner}/{repo}/actions/runs?per_page=5` | 最近运行记录 | M4 |

PAT 权限：fine-grained token 选定仓库，**Administration: Read and write**（必需）、Metadata: Read（自动）、Contents: Read 和 Actions: Read（M4 功能用）。classic token 勾 `repo` 即可。

**runner 命令（在每个 runner 目录下执行）**

```powershell
# 无人值守注册（不会出现交互提问）
config.cmd --unattended --url https://github.com/{owner}/{repo} --token {reg_token} --name {name} --labels {labels} --work _work --replace

# 前台运行（进程模式）；run.cmd 内部会在 runner 自更新后自动重启 Listener
run.cmd

# 注销
config.cmd remove --token {remove_token}

# 服务模式（M4，需管理员）：config 时加 --runasservice
# 服务名形如 actions.runner.{owner}-{repo}.{name}
```

**runner 目录里有用的文件**

- `.runner`：注册成功后生成的 JSON（带 UTF-8 BOM），含 `agentId`、`agentName`、`gitHubUrl`、`workFolder` 等。用来取 GitHub 端 runner id，也用来导入已有目录。
- `.credentials`、`.credentials_rsaparams`：runner 的凭据，应用只判断其是否存在，绝不读写内容。
- `_diag\Runner_*.log`、`_diag\Worker_*.log`：runner 自身日志，详情页可直接查看。
- `.env`：runner 启动时会读取的环境变量文件，代理（`https_proxy`、`no_proxy`）写在这里即可对 runner 生效。

**状态关键字（M0 需用真实输出确认）**：stdout 中出现 `Listening for Jobs` 表示空闲，`Running job: <name>` 表示开始执行，`Job <name> completed with result: <result>` 表示结束。

## 架构与目录结构

三层结构，依赖只能自上而下：`ui` → `app` → `core`。`core` 不得 import PyQt5，所有网络和子进程调用都在 `core` 里完成，由 `app` 放到后台线程执行，再用 Qt 信号把结果交回界面。

&#91;embedded content: RunnerDeck 分层架构 · 3 层 + 3 类外部资源\]

界面只通过 Controller 发起操作、接收信号；对 GitHub、runner 目录和本地存储的所有读写都集中在 core 层。

```text
runnerdeck/
├─ pyproject.toml           # 版本号唯一来源（[project].version）
├─ uv.lock
├─ CLAUDE.md
├─ CHANGELOG.md             # Keep a Changelog 格式，发布时从这里取说明
├─ .github/
│  ├─ workflows/
│  │  ├─ ci.yml             # push / PR：lint + 架构守卫 + 测试（+ M2 起打包冒烟）
│  │  └─ release.yml        # 推 v* tag：打包、出安装包、发 GitHub Release
│  └─ dependabot.yml        # github-actions + uv 依赖周更
├─ docs/
│  ├─ PLAN.md               # 本文档
│  ├─ PROGRESS.md           # 里程碑进度，唯一状态来源
│  └─ spike-notes.md        # M0 验证结论
├─ src/runnerdeck/
│  ├─ __main__.py           # 入口：单实例检查、创建 QApplication、托盘
│  ├─ core/                 # 纯 Python，不依赖 Qt
│  │  ├─ models.py          # RunnerRecord、AppSettings、RemoteRunner
│  │  ├─ paths.py           # 各目录位置
│  │  ├─ store.py           # config.json 读写（原子写）
│  │  ├─ secrets.py         # keyring 封装
│  │  ├─ github_client.py   # REST API 封装
│  │  ├─ release.py         # 最新版本、下载、SHA256 校验、缓存
│  │  ├─ installer.py       # 解压 + config.cmd 注册 / 注销
│  │  ├─ process.py         # run.cmd 启停 + 输出解析
│  │  ├─ runner_file.py     # 解析 .runner
│  │  ├─ service.py         # Windows 服务模式（M4）
│  │  ├─ errors.py / redact.py / logs.py / winproc.py   # M1 新增：异常、脱敏、日志、Windows 进程工具
│  │  ├─ status.py          # M1 新增：本地 + 远端状态合并（纯函数）
│  │  ├─ manager.py         # M1 新增：添加/移除/导入/状态的业务流程与回滚，CLI 和 Controller 共用
│  │  └─ cli.py             # 调试用命令行（runnerdeck-cli），M1 验收用
│  ├─ app/                  # Qt 编排层
│  │  ├─ controller.py      # RunnerController(QObject)：对外的唯一入口
│  │  ├─ workers.py         # QRunnable + 信号
│  │  ├─ poller.py          # 定时同步 GitHub 状态
│  │  ├─ autostart.py       # HKCU\...\Run 开机自启
│  │  └─ tray.py
│  └─ ui/
│     ├─ main_window.py     # FluentWindow
│     ├─ pages/             # login / dashboard / add_wizard / runner_detail / settings
│     ├─ components/        # runner_card / status_badge / log_viewer
│     └─ resources/         # 图标
├─ tests/
│  ├─ fixtures/             # 真实 stdout 样本、.runner 样本、API 响应
│  └─ core/
└─ scripts/
   ├─ spike_*.py            # M0 验证脚本
   └─ build.ps1             # PyInstaller 打包
```

## 数据模型与本地存储

`config.json` 只是索引：注册关系以 GitHub 为准，本地以每个 runner 目录里的 `.runner` 为准，应用启动时三者对账（reconcile）。

**存储位置**

| 内容 | 位置 |
| --- | --- |
| 设置 + runner 记录 | `%APPDATA%\RunnerDeck\config.json` |
| PAT | Windows 凭据管理器（keyring，service = `RunnerDeck`，username = GitHub 登录名） |
| runner 安装包缓存 | `%LOCALAPPDATA%\RunnerDeck\cache\` |
| 应用日志 | `%LOCALAPPDATA%\RunnerDeck\logs\` |
| runner 根目录 | 默认 `D:\runners\`，可在设置中修改；尽量短，避免 Windows 260 字符路径限制导致构建失败 |

**RunnerRecord（持久化）**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | str | 本地 UUID |
| `scope` | str | 目前固定 `repo`，为组织级预留 |
| `owner` / `repo` | str | 例如 `nablejohne` / `QFluent` |
| `name` | str | runner 名，默认 `{主机名}-{repo}`，同仓库内唯一 |
| `labels` | list\[str\] | 自定义标签（`self-hosted`、`Windows`、`X64` 由 runner 自动添加） |
| `path` | str | runner 目录绝对路径 |
| `mode` | str | `process` 或 `service` |
| `github_runner_id` | int \| None | 取自 `.runner` 的 `agentId` |
| `auto_start` | bool | 应用启动时是否自动拉起 |
| `created_at` | datetime | 创建时间 |

**AppSettings（持久化）**：`runners_root`、`proxy`、`poll_interval_s`（默认 30）、`default_labels`、`launch_on_login`、`start_minimized`、`close_to_tray`、`theme`。

**运行时状态（不持久化）**：`local_state`（stopped / starting / idle / busy / stopping / error）、`remote_status`（online / offline / unknown / missing）、`current_job`、`pid`、`last_error`、最近 2000 行输出的环形缓冲。

写 `config.json` 一律原子写：先写临时文件，再 `os.replace`。

## 关键流程

所有流程都在后台线程执行，每一步通过信号上报进度和错误；任何一步失败都要回滚已创建的目录和记录。

**添加 runner**

1. 选仓库：列表来自 `/user/repos`，标注公开/私有，已注册的仓库可再加但会提示。
2. 填写名称（默认 `{主机名}-{repo}`）、标签（预填 `default_labels`）、运行模式、是否立即启动。
3. 公开仓库弹出安全提醒：fork PR 可能在本机执行代码，附仓库 Settings → Actions → General 的链接，建议开启 fork PR 审批。
4. 准备安装包：查缓存 → 没有或版本落后则下载 → 校验 SHA256。
5. 解压到 `{runners_root}\{repo}\`，重名时追加后缀。
6. 请求注册 token。
7. 执行 `config.cmd --unattended ...`，捕获全部输出，超时 120 秒；失败则展示输出并清理目录。
8. 读取 `.runner` 拿到 `agentId`，写入记录。
9. 按选择启动。

**启动（进程模式）**

1. `subprocess.Popen(['cmd.exe', '/c', 'run.cmd'], cwd=runner目录)`，`creationflags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`，stdout/stderr 合并成管道，环境变量带上代理。
2. 读取线程逐行写入环形缓冲，并解析状态关键字发出信号。
3. 用 psutil 跟踪整个进程树（runner 自更新时 Listener 会重启，单个 pid 不可靠）。

**停止**

1. 若正在执行 job，弹窗确认：强制停止会让这个 job 失败。
2. 向进程组发送 `CTRL_BREAK_EVENT`，最多等 15 秒。
3. 仍未退出则 `taskkill /PID {pid} /T /F`。
4. 状态置为 stopped。应用退出时对所有进程模式 runner 执行同样流程（服务模式不受影响）。

具体哪种停止方式最干净，必须在 M0 实测确认。

**状态同步**

- 本地：进程是否存活 + stdout 解析出的 idle / busy / 当前 job。
- 远端：Poller 每 `poll_interval_s` 秒按仓库分组请求 runner 列表，用 `github_runner_id` 匹配（名称兜底）。
- 合并后的显示规则：

| 本地 | 远端 | 显示 |
| --- | --- | --- |
| 运行中 | online，busy=false | 空闲 |
| 运行中 | online，busy=true | 运行中（显示 job 名） |
| 运行中 | offline | 连接中；超过 60 秒标为异常 |
| 已停止 | offline | 已停止 |
| 已停止 | online | 被其他进程运行（服务或残留进程），提示接管或结束 |
| 任意 | 列表中不存在 | 已在 GitHub 端被删除，提示重新注册或移除本地记录 |

**移除**

1. 若在运行，先停止。
2. 请求 remove-token，执行 `config.cmd remove --token ...`。
3. 失败或目录已不存在时，改用 `DELETE /actions/runners/{id}`。
4. 删除目录（勾选项：保留 `_work`），删除记录。

**导入已有 runner**

1. 选择目录 → 读取 `.runner` → 得到仓库地址、名称、`agentId`。
2. 用 API 确认该 runner 仍存在，写入记录。
3. 应用启动时扫描 `runners_root`，发现未登记的 runner 目录时提示导入；用 psutil 找出路径在根目录下、但不属于本应用的 `Runner.Listener.exe`，提示接管或结束。

## UI 设计

主窗口用 QFluentWidgets 的 `FluentWindow`，左侧导航四个页面；平时关窗只缩到托盘，托盘是最常用的入口。界面线程绝不做网络或子进程调用。

| 页面 | 内容 | 主要组件 |
| --- | --- | --- |
| 登录（首次启动） | 输入 PAT 并调用 `/user` 验证；附创建 fine-grained token 的链接和所需权限说明 | `PasswordLineEdit`、`HyperlinkButton`、`InfoBar` |
| 概览 | 顶部汇总（运行 / 忙碌 / 离线数量）+ 全部启动、全部停止；下方每个 runner 一张卡片：仓库名、runner 名、状态徽章、当前 job、启停开关、更多菜单（打开目录、查看日志、在 GitHub 打开、移除） | `CardWidget`、`InfoBadge`、`SwitchButton`、`RoundMenu` |
| 添加 runner | 三步向导：选仓库（可搜索）→ 配置名称、标签、模式 → 执行进度（逐步打勾，失败展开输出） | `SearchLineEdit`、`ListWidget`、`ProgressBar`、`MessageBox` |
| runner 详情 | 基本信息、标签、实时输出与 `_diag` 日志切换、操作按钮 | `SegmentedWidget`、`PlainTextEdit` |
| 设置 | 账号（PAT 状态、更换、退出）、runner 根目录、代理、轮询间隔、默认标签、开机自启、关闭到托盘、主题 | `SettingCardGroup` 及各类 `SettingCard` |

**托盘**

- 图标颜色表示汇总状态：灰 = 全部停止，绿 = 有 runner 空闲在线，蓝 = 有 job 正在执行，红 = 有异常。
- 菜单：打开主界面、全部启动、全部停止、每个 runner 的子菜单（启动 / 停止）、退出。
- 可选通知：job 完成或失败时弹托盘提示。

## 风险点与待验证事项

最大的不确定性在 runner 进程的停止和状态解析，这两项必须在写 UI 之前用真实 runner 验证清楚。

| 风险 | 后果 | 应对 |
| --- | --- | --- |
| `run.cmd` 是批处理，收到 Ctrl+Break 可能卡在 “终止批处理操作吗(Y/N)?” | 停止失败、残留进程、GitHub 端仍显示在线 | M0 对比三种方式：Ctrl+Break 发给进程组、直接结束 `Runner.Listener.exe`、`taskkill /T`；记录 GitHub 端多久变 offline |
| stdout 关键字与预期不符 | 状态误判 | M0 保存真实输出样本到 `tests/fixtures`，解析逻辑以样本为准 |
| runner 自更新会重启进程 | 跟踪的 pid 失效 | 跟踪进程树而非单个 pid；以 `.runner` 目录 + 进程路径识别 |
| 公开仓库 + self-hosted | 他人的 fork PR 在本机执行任意代码 | 添加时检测 `private` 并警告，建议开启 fork PR 审批 |
| Windows 路径长度 | 构建工具报路径过长 | 默认短根目录 `D:\runners`，设置页提示 |
| 国内网络 | 下载安装包慢、runner 连不上 | 代理同时作用于 httpx 和 runner（写入 runner 目录的 `.env`） |
| 服务模式需要管理员 | 需 UAC 提权 | 放到 M4，仅对单条命令用 `ShellExecute runas` 提权 |
| 注册 token 约 1 小时过期 | 注册失败 | 每次注册前即时获取，不缓存 |
| PAT 泄露 | 仓库管理权限外泄 | 只存 keyring，日志统一脱敏，引导使用最小权限的 fine-grained token |

## CI 与发布流程

原则：仓库是公开的，**CI 和发布一律跑 GitHub 托管的 runner**（公开仓库免费），绝不让本仓库的 workflow 跑在自托管 runner 上——任何人提一个 fork PR 就能在本机执行代码，这正是本项目要提醒用户规避的风险。RunnerDeck 自身的“吃狗粮”验证放在私有的 `runnerdeck-sandbox` 仓库里做。

**CI（`.github/workflows/ci.yml`）**

触发：push 到 `main`、所有 PR、手动 `workflow_dispatch`；同一分支新提交自动取消旧运行（`concurrency` + `cancel-in-progress`）。

| Job | Runner | 内容 | 引入阶段 |
| --- | --- | --- | --- |
| `lint` | ubuntu-latest | `uv sync --locked` → `ruff check` → `ruff format --check` | M0 |
| `arch-guard` | ubuntu-latest | 用 ruff `TID251 banned-api` 禁止 `core/` import `PyQt5` / `qfluentwidgets`（`src/runnerdeck/core/ruff.toml` 继承根配置单独加规则），并入 `lint` job 即可 | M1 |
| `test` | windows-latest，Python 3.11 | `uv run pytest --cov=runnerdeck.core`；core 覆盖率门槛 80%；集成测试因无 `RUNNERDECK_TEST_PAT` 自动跳过 | M0（fixture 解析测试）/ M1 全量 |
| `ui-smoke` | windows-latest | `QT_QPA_PLATFORM=offscreen` + pytest-qt：主窗口能创建、各页能切换。**注记（M2）：并入 `test`（pytest-qt 离屏）+ `build`（exe `--smoke-test`），不单设 job** | M2 |
| `build` | windows-latest | `scripts/build.ps1` 打 onedir，启动 exe 跑 `--version` 自检，上传 artifact（保留 7 天） | M2 |

约定：

- 全部 workflow 顶层 `permissions: contents: read`，需要写权限的 job 单独声明。
- 第三方 action 固定版本：有主版本 tag 的用主版本（`actions/checkout@v7`），只发完整版本的固定到完整版本（`astral-sh/setup-uv@v10.3.1`，它没有 `v10` 这种浮动 tag）；由 dependabot 每周提升级 PR。
- 不在 CI 里放 PAT secret，集成测试只在本机跑；fork PR 本来也拿不到 secret。
- 仓库开启 Secret scanning + Push protection（公开仓库免费），兜底防 PAT 误提交。
- `main` 开分支保护：要求 `lint`、`test` 通过才能合并（个人项目可允许管理员直推，但 CI 红了必须先修）。

**版本与发布（`.github/workflows/release.yml`）**

- 版本号：SemVer，唯一来源是 `pyproject.toml` 的 `version`；运行时用 `importlib.metadata.version("runnerdeck")` 读取，设置页“关于”和 `--version` 都显示它。
- 0.x 阶段：M2 结束发 `v0.1.0`（预发布），M3 结束发 `v0.2.0`，M4 结束发 `v1.0.0`。tag 带 `-alpha` / `-beta` / `-rc` 后缀时自动标记为 pre-release。
- 发布步骤（本机）：
  1. 改 `pyproject.toml` 版本号，`uv lock`；把 `CHANGELOG.md` 的 `Unreleased` 段落改成新版本号和日期。
  2. 提交 `chore(release): vX.Y.Z`，推到 `main`，等 CI 绿。
  3. `git tag vX.Y.Z && git push origin vX.Y.Z`。
- release.yml（`on: push: tags: ['v*']`，windows-latest）：
  1. 校验 tag 与 `pyproject.toml` 版本一致，不一致直接失败。
  2. 重跑 lint + test（不信任“之前绿过”）。
  3. PyInstaller onedir → `RunnerDeck-X.Y.Z-win-x64.zip`；M4 起再用 Inno Setup（windows-latest 镜像自带 `iscc`）出 `RunnerDeck-X.Y.Z-setup.exe`。
  4. 生成 `SHA256SUMS.txt`；用 `actions/attest-build-provenance` 生成构建来源证明（免费、零维护，用户可 `gh attestation verify` 校验）。
  5. 从 `CHANGELOG.md` 截取本版本段落作为 release 正文，`gh release create` 上传全部产物；该 job 单独声明 `permissions: contents: write, id-token: write, attestations: write`。
- 代码签名：MVP 不做，README 说明 SmartScreen 提示“未知发布者”属正常并给出 SHA256 校验方法；以后有需要再接 SignPath（开源项目免费）。
- 应用内更新检查（M4 可选）：启动时查 `GET /repos/nablejohne/RunnerDeck/releases/latest`，有新版只在设置页提示并给下载链接，不做自动替换。

## 开发里程碑

分五步推进，每步都有可验证的产出；先把 core 做成命令行可用，再套 UI，这样 UI 出问题时能快速判断是界面还是底层。建议准备一个私有测试仓库（如 `runnerdeck-sandbox`），里面放一个 `runs-on: self-hosted` 的简单 workflow。

**M0 技术验证**

- [ ] `scripts/spike_api.py`：用 PAT 调通 `/user`、仓库列表、注册 token、runner 列表
- [ ] `scripts/spike_runner.py`：下载、解压、无人值守注册到测试仓库
- [ ] 对比三种停止方式，记录 GitHub 端变 offline 的耗时
- [ ] 保存空闲、执行 job、job 结束、自更新时的 stdout 样本
- [ ] 工程地基：`pyproject.toml` + `uv.lock`、ruff / pytest 配置、`CHANGELOG.md`、`ci.yml`（`lint` + `test` 两个 job）、`dependabot.yml`

* 验收：结论写入 `docs/spike-notes.md`，样本放入 `tests/fixtures/`；GitHub 上 CI 首次跑绿

**M1 core 层**

- [ ] models、store、secrets、paths
- [ ] github\_client（含分页、错误类型、脱敏日志）
- [ ] release（缓存 + SHA256 校验）、installer、runner\_file
- [ ] process（启停、进程树跟踪、输出解析）
- [ ] `cli.py`：`login` / `repos` / `add` / `start` / `stop` / `status` / `remove` / `import`
- [ ] CI：加入架构守卫（`core/` 禁止 import Qt）和 core 覆盖率 80% 门槛

* 验收：只用 CLI 完成测试仓库的添加 → 跑通一次 workflow → 移除；`pytest` 全绿，CI 绿

**M2 UI 最小可用**

- [ ] RunnerController + workers
- [ ] 登录页、概览页、添加向导、启停、移除、基础设置
- [ ] `scripts/build.ps1`（PyInstaller onedir）+ `--version` 参数
- [ ] CI：加入 `ui-smoke` 和 `build` job；新增 `release.yml`（先只出 zip）

* 验收：从零给两个仓库各添加一个 runner，都能跑通 workflow，界面全程不卡顿；推 `v0.1.0` tag 自动产出 GitHub Release，下载的 zip 解压即可运行

**M3 日常可用**

- [ ] 托盘、关闭到托盘、开机自启（HKCU Run）、启动时自动拉起 runner
- [ ] Poller 与状态合并规则、详情页日志查看
- [ ] 导入已有目录、发现残留进程
- [ ] job 完成 / 失败通知

* 验收：开机后无需操作，runner 自动在线；把现有 `D:\tmp\actions-runner` 导入成功；发布 `v0.2.0`

**M4 增强与发布**

- [ ] 服务模式
- [ ] 扫描 workflow 中的 `runs-on: self-hosted`、最近运行记录
- [ ] 代理同时作用于 runner
- [ ] release.yml 增加 Inno Setup 安装包、`SHA256SUMS.txt`、构建来源证明
- [ ] 可选：设置页检查新版本
- [ ] 发布流程跑在 GitHub 托管 runner 上（见“CI 与发布流程”，不用自托管 runner）

* 验收：推 `v1.0.0` tag，Release 页面出现 zip + setup.exe + SHA256SUMS，安装包在干净的 Windows 上可装可卸

## 用 Claude Code 开发

把本文档导出为 Markdown 放到 `docs/PLAN.md`，在仓库根目录放下面的 `CLAUDE.md`，然后一次只推进一个里程碑。

1. 新建仓库并初始化：让 Claude Code 按“架构与目录结构”生成骨架、`pyproject.toml`、ruff 与 pytest 配置。
2. 每个里程碑先进入 plan mode，让它读 `docs/PLAN.md` 出实现计划，确认后再写代码。
3. M0 需要你在本机配合运行 spike 脚本，把结论和样本交回给它。
4. 集成测试用的 PAT 通过环境变量提供，不要贴进对话或代码。

可以这样开场：

```text
阅读 docs/PLAN.md 和 CLAUDE.md。我们现在做 M0：写 scripts/spike_api.py 和 scripts/spike_runner.py，
用环境变量 RUNNERDECK_TEST_PAT 和 RUNNERDECK_TEST_REPO 指定测试仓库。先给出计划，确认后再实现。
```

**CLAUDE.md 模板**

```markdown
# RunnerDeck

Windows 托盘应用：管理多个仓库级 GitHub Actions self-hosted runner。完整方案见 docs/PLAN.md，runner 实测结论见 docs/spike-notes.md。

## 技术栈
- Python 3.11, PyQt5 5.15, PyQt-Fluent-Widgets (import qfluentwidgets)
- httpx, pydantic v2, keyring, psutil；M4 起 pywin32
- uv 管理依赖，ruff 格式化与检查，pytest + respx 测试

## 常用命令
- 安装依赖：uv sync
- 运行：uv run python -m runnerdeck
- 调试 CLI：uv run runnerdeck-cli --help（或 uv run python -m runnerdeck.core.cli --help）
- 测试：uv run pytest
- 检查：uv run ruff check . ; uv run ruff format --check .
- 打包：./scripts/build.ps1
- 发布：改 pyproject 版本 + CHANGELOG → 推 main → `git tag vX.Y.Z && git push origin vX.Y.Z`（详见 docs/PLAN.md“CI 与发布流程”）

## 架构规则
- 依赖方向只能是 ui -> app -> core；core 不得 import PyQt5 或 qfluentwidgets
- UI 线程不得做网络或子进程调用；耗时操作放 app/workers.py，结果通过信号返回
- GitHub 调用只能经过 core/github_client.py
- 调用 config.cmd / run.cmd 的代码只能在 core/installer.py 和 core/process.py
- config.json 一律原子写（临时文件 + os.replace）

## 安全
- PAT 只存 keyring，绝不写入文件、日志、异常信息或测试快照；日志中的 token 一律脱敏
- 不读写 runner 目录里的 .credentials* 文件
- 集成测试读取 RUNNERDECK_TEST_PAT 和 RUNNERDECK_TEST_REPO，未设置时自动跳过

## 约定
- 界面文案用中文；代码、注释、提交信息用英文
- core 层的新功能必须附单测
- CI 和发布只用 GitHub 托管 runner，本仓库 workflow 不得使用 `runs-on: self-hosted`
- 每个里程碑收尾更新 docs/PROGRESS.md
- 对 runner 行为不确定时，先查 docs/spike-notes.md；没有记录就先写 spike 验证，不要猜
```
