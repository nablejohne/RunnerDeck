# RunnerDeck 进度

项目状态的唯一来源。接续工作先读本文件，再读 docs/PLAN.md。

## 路线

- [x] M0 技术验证 + 工程地基（spike 脚本、stdout 样本、停止方式结论、pyproject/ruff/pytest、ci.yml 首次跑绿）
- [x] M1 core 层 + CLI（架构守卫、覆盖率门槛）
- [x] M2 UI 最小可用 + 打包 + release.yml → v0.1.0（2026-10-24 发布，预发布）
- [x] M3 日常可用（托盘、自启、Poller、导入）：验收全部通过；不单独发 v0.2.0，随 v1.0.0 一起发布
- [ ] M4 增强与发布（服务模式、Inno Setup、来源证明）→ v1.0.0（进行中）

## M4：进行中（2026-10-24 起）

契约 `docs/m4-design.md`（§13 记 spike 与实现后的修订）。用户决策：服务模式完整支持（UAC 只提权单条命令；安装时给当前用户授启停权限；服务账号只用 NETWORK SERVICE）；安装包按用户安装到 `%LOCALAPPDATA%\Programs\RunnerDeck`；顺序实现 + 最后 1 个独立 reviewer；M3 不单独发版。

- [x] 计划与契约（用户已确认）
- [x] 0 真机 spike S1–S11（`ssh XM`，2026-10-24）→ spike-notes §12：服务装完即启动、SCM 停止是优雅的（空闲 1 s / job 中 5.4 s Canceled）、授权后非管理员可启停、stdout 原样进 Runner 日志的 `WRITE LINE:`、`.env` 代理生效、转服务 = 注销后重装；**新坑**：Listener 要能列出每一级上级目录，根目录加固须留 `Authenticated Users:(RX)`
- [x] 1 依赖与小项：pywin32 / PyYAML、互斥量、runner 版本、根目录加固、代理写进 `.env`（954a263、f3f1fcf，CI 绿）
- [x] 2 服务 core（service / elevate / elevated、installer、manager、CLI）：XM 提权 SSH 下 CLI 全流程通过（添加→job→停/启→转服务→移除，无残留），真机 pytest 2486 passed；修订见 m4-design §13
- [x] 3 服务 app / ui：向导选项、卡片 / 详情 / 托盘、转服务 / 授权、Runner 日志解析的状态与 job 通知（桌面验收放在第 8 步）
- [x] 4 Workflow 扫描、最近运行、检查更新（XM 上用真实 PAT 验证了接口）
- [x] 5 取消注销后恢复（`WM_ENDSESSION(FALSE)` 原生事件过滤器 + 等停下后重启；桌面验证放在第 8 步）
- [ ] 6 打包与发布：Inno Setup、CI 安装冒烟、SHA256SUMS、来源证明
- [ ] 7 独立评审（1 个 reviewer）
- [ ] 8 CI 绿 → 真机 pytest → 桌面验收 → 发布 v1.0.0 → 回流评估

## M3：已完成（2026-10-24，随 v1.0.0 发布）

契约 `docs/m3-design.md`（§10 记实现后的修订）。实现方式：地基（本会话）→ 4 车道并行工作流 → 整合 → 一个独立 reviewer 评审（用户因成本取消了多 agent 评审工作流）。

- [x] 地基：`models`（`notify_jobs` + 运行时数据类）、`status` 合并规则补两条（优雅停止后 90 s 宽限、远端快照早于本地状态）+ `bucket` / `tray_level` / `is_job_failure`、FakeController、架构守卫（ui 禁止 autostart/stop_marks/diag/winreg/psutil）
- [x] 车道 A core：`stop_marks`（每 runner 一个文件，GUI/CLI 共用，补 M1 遗留的强杀信息缺口）、`diag`、`autostart`（HKCU Run，可注入注册表）、`RunnerProcess.signal_stop`、manager 一次性进程扫描 / discover / kill_external / kill_stray / forget / auto_start、CLI `forget`
- [x] 车道 B app：controller 实时合并 + 5 s 重算 + `display_changed`、接管 / 结束进程、诊断日志、自启开关；`StatusPoller`（静默失败、全部失败降频 5 分钟、kick 去抖）
- [x] 车道 C ui：托盘（颜色、菜单、通知）、关窗到托盘 / 退出、详情页（实时输出 + 诊断日志，取代 LogDialog）、接管 / 结束、导入、发现横幅、设置页新项、主题
- [x] 车道 D：单实例激活、`--autostart`、注销处理、spec、README / CHANGELOG / CLAUDE.md
- [x] 整合：全量 2215 passed（macOS 离屏）、smoke 退出 0、FakeController 与真实 controller 一致性测试、真实 controller + manager 接线脚本检查
- [x] 独立评审（1 个 reviewer）9 条 → 修 8 条（卡片在确认框期间被重建删除、残留进程误判会杀自己的 runner、状态窗口按快照时间算、ERROR 让位 EXTERNAL、无窗口时的 MessageBox 崩溃、设置过期副本覆盖、登录框叠模态、停止记录读后删）；取消注销后不自动恢复留 M4；全量 2300 passed、smoke 0
- [x] CI（windows-latest，0f8df5e）绿：lint、test 2329 passed（core 覆盖率 98%）、build 打包并冒烟三个 exe。前两轮失败都在测试侧：同线程命名管道写不出（改成真实第二进程发送）、离屏下 qframelesswindow 的 GetWindowPlacement 报 1400（`ui/compat.tolerate_offscreen_window_handles`）、import/forget 测试与随后的 discover() 竞态
- [x] XM：真机 pytest 2328 passed；`_diag` 运行中可读（列表元数据改用 `os.stat`，否则正在写的日志显示 0 B）；CLI 强杀后 `status` 显示“GitHub 会话未过期”并倒数（spike-notes §11）
- [x] **桌面验收（2026-10-24，XM 桌面，CI 产物 2a38faa `RunnerDeck-0.1.0-win-x64.zip`，sha256 142114e9…54b3c7，解压在 `D:\tmp\RunnerDeck-m3`）**：七项全部通过
  1. 托盘：颜色随状态变（灰 / 绿 / 蓝）、菜单启停、左键打开、关窗到托盘、托盘“退出”优雅停止；再次双击 exe 激活已有窗口
  2. 开机自启：打开开关、两个 sandbox runner 勾“随 RunnerDeck 启动”→ 重启后不做操作，托盘出现、两个 runner 自动在线（API 确认 online、idle）
  3. 导入 `D:\tmp\actions-runner`（注册在私有仓库 `nablejohne/QFluent`，id 21；PAT 先补上 QFluent 的仓库权限）→ 由 RunnerDeck 启动、在线 → 停止 → 取消登记；目录和 `.runner` 原样，GitHub 上 id 21 仍注册（API 确认）。QFluent 没有跑在这台 runner 上的 workflow，所以这一项没跑 job，job 路径由第 4 项的 sandbox 覆盖
  4. 通知（sandbox `spike.yml`，从开发机 `gh workflow run` 触发）：默认“仅失败”下成功 job（run 40066952230）不弹、失败 job（run 40067026244，`fail=true`）弹“失败”；改成“全部”后成功 job（run 40067112668）弹“成功”；执行中托盘变蓝、卡片显示“运行中：spike”
  5. 详情页：实时输出、诊断日志（大小非 0）、刷新、打开 `_diag` 目录、返回
  6. 接管：CLI 另开终端 `start` → 界面显示“被其他进程运行”+“接管”→ 接管后先“等待旧会话过期”再恢复空闲
  7. 注销：runner 运行中注销不被阻挡，重新登录后由开机自启恢复（用户反馈无问题；取消注销后不自动恢复仍是已知限制，留 M4）
- 发布：用户决定不单独发 v0.2.0，M3 的改动随 v1.0.0 一起发（CHANGELOG `[Unreleased]` 并入 1.0.0）

## M2：已完成（2026-10-24，v0.1.0 预发布）

接口与实现后的修订见 `docs/m2-design.md`（§10 以代码为准）。

- [x] core 地基：`console_helper`（GUI 无控制台时转发 Ctrl+Break）、`account`、`runner_env`（PowerShell 执行策略选项）
- [x] `app/`：`workers`（QThreadPool 任务）、`controller`（UI 的唯一入口，信号回传）
- [x] `ui/`：主窗口（FluentWindow）、登录对话框、概览页 + runner 卡片、添加向导、设置页、日志对话框；关窗先优雅停止全部 runner
- [x] 架构测试：ui 只经 controller 访问 core，app 不 import ui，console_helper 只用标准库
- [x] 打包：`scripts/build.ps1`（PyInstaller onedir，三个 exe + zip）；`--smoke-test`
- [x] CI：Windows 离屏 Qt 测试 + `build` job（打包并冒烟三个 exe）；`release.yml`（`v*` tag 发布 zip）
- [x] 修复：CI / release.yml 读版本号用错 TOML 键（`['project']['version']`），build job 首次因此失败
- [x] CI（windows-latest，3c30a81）：1837 passed、覆盖率 97.9%；build job 打包并冒烟三个 exe 通过，zip 作为 artifact 上传
- [x] 真机运行 CI 打包产物：三个 exe 在 XM 上可运行（GUI 验收即用此包）；XM 上本地 `build.ps1` 未复跑（CI windows-latest 已覆盖）
- [x] **真机 GUI 验收（2026-10-24，XM 桌面，CI 产物 zip）**：登录（Ctrl+V）→ 向导给 `runnerdeck-sandbox`、`runnerdeck-sandbox2`（新建的私有仓库）各添加一个 runner（下载、校验、ACL、`.env` 执行策略、注册、启动）→ 两个 workflow 并行成功，PowerShell 步骤 `policy=RemoteSigned` → 界面全程不卡 → 关窗“停止并退出”：两个 runner 都是 helper 转发 Ctrl+Break 的优雅停止（并行，约 1.2 s），无残留进程，GitHub 上均 offline
  - 插曲：两个 runner 注册时没有 `runnerdeck-spike` 标签（用户确认向导标签栏没填，属正常行为），测试 job 排队；用 API 补标签后，已排队的 job 不会重新匹配，取消重派才跑通
- [x] 发布 `v0.1.0`（预发布）：https://github.com/nablejohne/RunnerDeck/releases/tag/v0.1.0 ，资产 `RunnerDeck-0.1.0-win-x64.zip`（67.8 MB，sha256 df3507fa…c0c01ae6）；release 工作流重跑 lint/1837 测试/打包/冒烟全过；`release.yml` 改为 `v0.*` 一律标记预发布

## M1：已验收（2026-10-23，Windows 真机，通过 SSH）

M0 第 1 轮 spike 已完成；**第 2 轮推迟**（用户决定先做 M1，第 2 轮项目不阻塞 M1，见下方“M0 遗留”）。

M1 已完成：
- [x] 地基：`errors` `models` `paths` `redact` `logs` `winproc`，接口契约 `docs/m1-design.md`（§13 记录实现后的修订）
- [x] `store` `secrets` `github_client` `release` `runner_file` `installer` `process` `status` `manager` `cli`（`runnerdeck-cli` 入口）
- [x] 架构守卫：`core/ruff.toml`（TID251 禁止 Qt/app/ui）+ `tests/core/test_architecture.py`（只有 installer/process 调 cmd.exe，只有 github_client 用 httpx）
- [x] CI：Windows test job 加 core 覆盖率 ≥ 80% 门槛（实际约 99%）
- [x] 对抗评审：6 个维度找问题 + 每条独立反驳，确认 16 条（归并为 7 个问题）全部处理，C14 推迟到 M3
- [x] **M1 验收（Windows 真机）**：`add`（下载 2.338.1 + SHA256 + ACL）→ `start` → workflow 成功 → Ctrl+C 优雅停止 → `status` → `remove`，GitHub 无残留；job 执行中 Ctrl+C 也能优雅停止（job Canceled）；真机 `pytest` 1606 passed，集成测试 6 passed
- [x] 桌面会话验证 `login`（传统控制台里 Ctrl+V 粘贴）/ `whoami`（来源：系统凭据管理器）/ `logout`（已删除）；SSH 会话无法访问凭据管理器，见 spike-notes §10

验收命令（PowerShell，仓库根目录，先 `git pull` 和 `uv sync`）：

```powershell
uv run runnerdeck-cli login                        # 粘贴 fine-grained PAT（Administration RW，选 runnerdeck-sandbox）
uv run runnerdeck-cli add nablejohne/runnerdeck-sandbox --labels runnerdeck-spike
uv run runnerdeck-cli start --all                  # 另开一个终端：gh workflow run spike.yml -R nablejohne/runnerdeck-sandbox -f seconds=20
#   看到 job 完成后按一次 Ctrl+C，应显示优雅停止
uv run runnerdeck-cli status
uv run runnerdeck-cli remove nablejohne/runnerdeck-sandbox -y
uv run pytest
```

验证：`uv run ruff check . && uv run ruff format --check . && uv run pytest`（本地 macOS 1600+ passed）；GitHub CI（ubuntu lint + windows test）绿。

### M0 第 2 轮（已完成，见 spike-notes §10）

除自更新（无法按需触发）外全部完成：helper 转发 Ctrl+Break 可行（M2 托盘停止方案）、job 中停止需约 15 s（已改为 90 s 超时）、`.env` 执行策略修复有效、`--replace` / 已删除 runner 的输出、DELETE 幂等、`.runner` 与 API 样本入库。

## 已完成

### 2026-10-24 M2 收尾与 v0.1.0

- 修 CI：冒烟测试和 release.yml 读版本号用错 TOML 键（`['project']['version']`）
- 真机 GUI 验收通过（两个仓库、并行 workflow、关窗优雅停止无残留）；新建私有测试仓库 `runnerdeck-sandbox2`
- 发布 v0.1.0 预发布版


### 2026-10-23 M1 真机验收 + M0 第 2 轮

- 真机发现并修复：runner 在 GitHub 被删除后自己清配置并以 0 退出（改判 ERROR）；job 中停止 15 s 不够导致强杀和僵尸 job（改为 90 s）；DELETE 遇僵尸 job 返回 422（给 force-cancel 提示）；SSH 会话无法用凭据管理器（提示 RUNNERDECK_PAT）；状态文案
- 用户反馈 login 粘贴失败：传统控制台里 Ctrl+V 只给 getpass 一个 U+0016；新增 `secret_input.read_secret`（Ctrl+V 读剪贴板、支持管道），桌面复测通过
- 新增真实样本：`deregistered_on_github.txt`、`config_replaced.txt`、`remove_already_gone.txt`、`runner/runner_file.json`、`api/*.json`，`tests/core/test_real_samples.py`


### 2026-10-23 M1 core 层 + CLI（代码）

- 6 个模块并行实现（各自带测试）→ manager → cli；整合时修了 manager 读取成功标记的类型错误
- Windows CI 首次跑真实进程测试：假 runner 启动/Ctrl+Break 优雅停止/强杀/崩溃识别、真实 cmd.exe 调用 config.cmd 全部通过；CI 进程有控制台
- 评审确认并修复：Listener 崩溃被当成正常停止（run.cmd 总是 exit 0）、PAT 经环境变量泄漏给 workflow job、404“无权限”被当成“已删除”、config.cmd 不走代理、只信任 certifi、`start` 的 Ctrl+C 处理、runner 目录 ACL 过宽；附带：cmd.exe 绝对路径、解压中断清理、CLI 独立日志文件、`remove --local-only`


### 2026-10-22 项目初始化

- 按 PLAN.md 建好目录（空目录用 `.gitkeep` 占位）
- PLAN.md 补充“CI 与发布流程”一节，并把 CI/发布任务分摊到 M0–M4
- 创建公开仓库 https://github.com/nablejohne/RunnerDeck，许可证 GPLv3

## 决策与坑

- GitHub 只在 job 入队时按标签匹配 runner；之后再给 runner 加标签，已排队的 job 不会被分配，要取消重派
- 发布：`v0.*` 自动标记预发布（SemVer 0.x = 初始开发）；版本号只读 `[project].version`

- M2 托盘应用的停止方案：helper 进程（CREATE_NO_WINDOW，自带隐藏控制台）转发 Ctrl+Break，真机 0.9 s 优雅退出
- 执行 job 时停止要等 runner 取消 job（约 15 s 起），超时 90 s；强杀会留下僵尸 job（runner busy、不能删除，只有 force-cancel 能解）
- GitHub `DELETE runner` 对不存在的 id 也返回 204；runner 被 GitHub 删除时输出 “The runner no longer exists on the server” 并以 0 退出

- M1 新增 core 模块 `errors/redact/logs/winproc/status/manager`（理由见 m1-design §0）；Controller（M2）只负责把 manager 的调用放进工作线程
- 真实 run.cmd 在 Listener 崩溃后仍 `exit /b 0`，异常只能从 run-helper 打印的 Listener 退出码判断；测试用的假 run.cmd 必须照搬这一点
- GitHub 对“runner 不存在”和“PAT 看不到仓库”都返回 404，只有 list_runners 成功后 404 才算“已删除”
- 传给 config.cmd / run.cmd 的环境一律去掉 PAT 类变量（`winproc.child_environment`）
- TLS 用 truststore 走系统证书库；CLI 和托盘应用写不同的日志文件
- `astral-sh/setup-uv` 没有浮动主版本 tag；Windows 上 TextIOWrapper 默认写 `\r\n`

- 只有和 runner 共享控制台的 Ctrl+Break 能干净停止（11 秒 offline、可立即重启）；`CREATE_NO_WINDOW` 下静默无效；强杀会让旧会话残留约 3 分钟
- `stdin=DEVNULL` 是硬性要求，否则 Ctrl+Break 会卡在“终止批处理操作吗(Y/N)?”
- 个人电脑默认执行策略 Restricted，`shell: powershell` 步骤全部失败；候选修复是 runner `.env` 写 `PSExecutionPolicyPreference`
- `config.cmd remove` 会删掉 `.runner`，必须先读出 `agentId`

- M0 spike 辅助函数放在 `scripts/spike_common.py`，pytest 用 `pythonpath = ["scripts"]` 导入；M1 由 core/ 重写后删除这段配置
- 下载 runner zip 用不带 Authorization 的独立 httpx client，避免把 PAT 发给 CDN
- fixture 只保留测试仓库的条目和最少字段，其他私有仓库名不进公开仓库
- 已发现：`registration-token` 的 `expires_at` 带 `+08:00` 偏移；release asset 自带 `digest` 字段可直接做 SHA256 校验
- `astral-sh/setup-uv` 没有浮动主版本 tag，必须写完整版本（首次 CI 因 `@v10` 失败）
- spike 的 `workflow_dispatch` 需要 PAT 有 Actions: Read and write，产品本身只要 Read

- 公开仓库的 CI / 发布只跑 GitHub 托管 runner，不用自托管 runner（fork PR 可在本机执行代码）
