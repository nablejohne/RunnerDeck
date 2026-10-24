# RunnerDeck 进度

项目状态的唯一来源。接续工作先读本文件，再读 docs/PLAN.md。

## 路线

- [x] M0 技术验证 + 工程地基（spike 脚本、stdout 样本、停止方式结论、pyproject/ruff/pytest、ci.yml 首次跑绿）
- [x] M1 core 层 + CLI（架构守卫、覆盖率门槛）
- [ ] M2 UI 最小可用 + 打包 + release.yml → v0.1.0
- [ ] M3 日常可用（托盘、自启、Poller、导入）→ v0.2.0
- [ ] M4 增强与发布（服务模式、Inno Setup、来源证明）→ v1.0.0

## 当前：M2（代码完成，等真机 GUI 验收和发布 v0.1.0）

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
  - 插曲：两个 runner 注册时没有 `runnerdeck-spike` 标签（向导标签栏为空），测试 job 排队；用 API 补标签后，已排队的 job 不会重新匹配，取消重派才跑通。标签栏是否被填过待用户确认
- [ ] 发布 `v0.1.0`（预发布）：CHANGELOG `[Unreleased]` 改成 `[0.1.0] - 日期`、pyproject 版本、推 tag，Release 页面出现 zip

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
